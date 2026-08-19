/**
 * Cloudflare Worker — Telegram → GitHub Actions 觸發器
 *
 * 流程:
 *   TG 使用者打指令 → Telegram 推 webhook → 這個 Worker →
 *   立刻回 "⏳ 收到指令" → 觸發 GitHub Actions 跑 check_stocks.py →
 *   結果由 check_stocks.py 直接推回 TG
 *
 * 環境變數(Cloudflare 後台「Settings → Variables」設定):
 *   TELEGRAM_TOKEN      Bot Token(Secret)
 *   TELEGRAM_CHAT_ID    你的 TG chat_id(Plain text)
 *   GITHUB_TOKEN        GitHub PAT,Actions:Write 權限(Secret)
 *   GITHUB_REPO         "GGININDERR/stock-alert"(Plain text)
 *   WEBHOOK_SECRET      Telegram webhook secret_token(Secret)
 */

// 版本:每次改 Worker 就 +1,方便確認部署到底有沒有生效
const VERSION = '2026-08-17 補上 /pos,webhook 恢復為主要路徑';

const HELP_TEXT = `🤖 <b>Stark 停損機器人</b>  <i>(v3 即時版)</i>

<b>查詢類</b>
/check  或 /check_all — 檢查台股 + 美股(停損 + 停利)
/check_tw — 只檢查台股
/check_us — 只檢查美股
/list — 持股清單 + 總損益
/price &lt;TW|US&gt; &lt;代碼&gt; — 查現價 + 均線
  例:<code>/price TW 2330</code>

<b>持股管理(需 Sheet 寫入權限)</b>
/add &lt;TW|US&gt; &lt;代碼&gt; &lt;成本&gt; &lt;股數&gt; [名稱]
  例:<code>/add TW 2330 950 1000 台積電</code>
/del &lt;TW|US&gt; &lt;代碼&gt; — 移除持股
  例:<code>/del TW 2330</code>
/sold &lt;TW|US&gt; &lt;代碼&gt; &lt;股數&gt; — 賣出 N 股
  例:<code>/sold TW 2330 500</code>
/clear CONFIRM — 一次清空全部持股(不可復原)

<b>幣圈掃描(OKX USDT 永續)</b>
/scan — 立刻掃一次(預設 breakout:剛啟動)
/scan early — 壓縮後突破,還沒噴的位置
/scan chase — 追高順勢,供觀察回踩
/scan classic — 最寬鬆的一組
/pos — 目前幣圈持倉:停損價、離停損多遠、是否逼近停損
/stats — 訊號追蹤統計(這些條件到底準不準)

<b>自動排程</b>
台股 13:35、美股 21:05 收盤後自動檢查持股。
幣圈每小時掃 breakout、每 4 小時掃 early,掃到才推播;
每天 09:15 推一次訊號追蹤統計。`;

// /scan 後面可接的模式
const SCAN_MODES = {
  '': 'breakout',
  'breakout': 'breakout',
  'early': 'early',
  'chase': 'chase',
  'classic': 'classic',
};

const SCAN_LABEL = {
  breakout: '剛啟動掃描',
  early: '壓縮後突破掃描',
  chase: '追高順勢掃描',
  classic: '寬鬆版掃描',
};

// 指令對應:[command, default_market, parser]
// parser 從 cmdText 餘下 tokens 拆出 (market, args[])
const COMMAND_TABLE = {
  '/check':     { cmd: 'check', market: 'ALL' },
  '/check_all': { cmd: 'check', market: 'ALL' },
  '/check_tw':  { cmd: 'check', market: 'TW' },
  '/check_us':  { cmd: 'check', market: 'US' },
  '/list':      { cmd: 'list',  market: 'ALL' },
  '/portfolio': { cmd: 'list',  market: 'ALL' },
  '/whoami':    { cmd: 'whoami', market: 'ALL' },
};

// 多參數指令(需從訊息餘下文字拆出 market + args)
const MULTI_ARG_COMMANDS = new Set([
  '/price', '/add', '/del', '/delete', '/sold',
]);

export default {
  async fetch(request, env) {
    // 健康檢查(瀏覽器打開會看到 OK)
    if (request.method === 'GET') {
      // 版本字串:用瀏覽器打開 Worker 網址就知道部署的是哪一版,
      // 不必靠打指令去猜。改動 Worker 時記得一起更新。
      return new Response(
        `Stark TG Bot Worker is running ✅\nversion: ${VERSION}`,
        { status: 200 }
      );
    }
    if (request.method !== 'POST') {
      return new Response('Method not allowed', { status: 405 });
    }

    // 驗證 webhook secret
    const secret = request.headers.get('X-Telegram-Bot-Api-Secret-Token');
    if (secret !== env.WEBHOOK_SECRET) {
      return new Response('Unauthorized', { status: 401 });
    }

    let update;
    try {
      update = await request.json();
    } catch (e) {
      return new Response('Bad JSON', { status: 400 });
    }

    const msg = update.message || update.edited_message;
    if (!msg) return new Response('ok');

    // 只接受預設 chat_id
    if (String(msg.chat?.id) !== String(env.TELEGRAM_CHAT_ID)) {
      return new Response('ignored: wrong chat');
    }

    const text = (msg.text || '').trim();
    if (!text) return new Response('ok');

    // 拆解第一個 token = 指令(去掉 @botname 後綴)
    const tokens = text.split(/\s+/);
    const firstToken = tokens[0].toLowerCase().split('@')[0];

    // /help /start
    if (firstToken === '/help' || firstToken === '/start') {
      await sendMessage(env, HELP_TEXT);
      return new Response('ok');
    }

    // /scan [模式] — 幣圈掃描
    if (firstToken === '/scan') {
      const key = (tokens[1] || '').toLowerCase();
      const mode = SCAN_MODES[key];
      if (!mode) {
        await sendMessage(
          env,
          `❌ 沒有「${tokens[1]}」這個模式。可用:` +
          `<code>breakout</code>(預設) <code>early</code> ` +
          `<code>chase</code> <code>classic</code>`
        );
        return new Response('ok');
      }
      await dispatch(env, {
        workflow: 'scan_bull.yml',
        inputs: { mode, args: '' },
        ackLabel: SCAN_LABEL[mode],
        note: '沒掃到標的就不會再有訊息,約 30 秒',
      });
      return new Response('ok');
    }

    // /pos — 幣圈持倉狀態(走 scan_bull.yml 的 positions 分支)
    if (firstToken === '/pos' || firstToken === '/positions' || firstToken === '/holding') {
      await dispatch(env, {
        workflow: 'scan_bull.yml',
        inputs: { mode: 'positions', args: '' },
        ackLabel: '幣圈持倉狀態',
      });
      return new Response('ok');
    }

    // /stats — 訊號追蹤統計
    if (firstToken === '/stats') {
      await dispatch(env, {
        workflow: 'scan_bull.yml',
        inputs: { mode: 'report', args: '' },
        ackLabel: '訊號追蹤統計',
      });
      return new Response('ok');
    }

    // 簡單指令(check / list 系列)
    if (COMMAND_TABLE[firstToken]) {
      const { cmd, market } = COMMAND_TABLE[firstToken];
      await ackAndDispatch(env, { command: cmd, market, args: '', ackLabel: prettyLabel(firstToken) });
      return new Response('ok');
    }

    // /clear — 清空全部持股,沒有市場參數,且一定要 CONFIRM
    if (firstToken === '/clear') {
      if ((tokens[1] || '').toUpperCase() !== 'CONFIRM') {
        await sendMessage(
          env,
          '⚠️ <b>這會刪掉全部持股</b>,無法復原。\n' +
          '確定的話請打:<code>/clear CONFIRM</code>'
        );
        return new Response('ok');
      }
      await ackAndDispatch(env, {
        command: 'clear', market: 'ALL', args: 'CONFIRM', ackLabel: '清空全部持股',
      });
      return new Response('ok');
    }

    // 多參數指令
    if (MULTI_ARG_COMMANDS.has(firstToken)) {
      const rest = tokens.slice(1);
      if (rest.length < 2) {
        await sendMessage(env, `❌ <code>${firstToken}</code> 參數不足,打 /help 看用法`);
        return new Response('ok');
      }
      const market = rest[0].toUpperCase();
      if (!['TW', 'US'].includes(market)) {
        await sendMessage(env, `❌ 第一個參數要是 TW 或 US,你傳的是「${rest[0]}」`);
        return new Response('ok');
      }
      const args = rest.slice(1).join(' ');
      const cmd = firstToken.slice(1); // 去掉開頭的 /
      await ackAndDispatch(env, {
        command: cmd,
        market,
        args,
        ackLabel: `${firstToken} ${market} ${rest.slice(1).join(' ')}`,
      });
      return new Response('ok');
    }

    // 其他訊息忽略
    return new Response('ok');
  },
};

function prettyLabel(token) {
  return {
    '/check':     '台股 + 美股',
    '/check_all': '台股 + 美股',
    '/check_tw':  '台股',
    '/check_us':  '美股',
    '/list':      '持股清單',
    '/portfolio': '持股清單',
    '/whoami':    '查 Service Account email',
  }[token] || token;
}

// 股票指令:固定觸發 stock_check.yml
async function ackAndDispatch(env, { command, market, args, ackLabel }) {
  return dispatch(env, {
    workflow: 'stock_check.yml',
    inputs: { command, market, args },
    ackLabel,
  });
}

// 通用觸發:指定 workflow 檔名與 inputs
async function dispatch(env, { workflow, inputs, ackLabel, note }) {
  const tail = note ? `\n<i>${note}</i>` : '';
  await sendMessage(env, `⏳ 收到指令,執行 <b>${ackLabel}</b> 中...${tail}`);

  const trigger = await fetch(
    `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/${workflow}/dispatches`,
    {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${env.GITHUB_TOKEN}`,
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'cf-worker-tg-bot',
      },
      body: JSON.stringify({ ref: 'main', inputs }),
    }
  );

  if (!trigger.ok) {
    const errText = await trigger.text();
    await sendMessage(env, `❌ 觸發失敗 (${trigger.status}): ${errText.slice(0, 200)}`);
  }
}

async function sendMessage(env, text) {
  return fetch(`https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/sendMessage`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      chat_id: env.TELEGRAM_CHAT_ID,
      text,
      parse_mode: 'HTML',
    }),
  });
}
