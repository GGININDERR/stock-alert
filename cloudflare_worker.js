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

const HELP_TEXT = `🤖 <b>Stark 停損機器人</b>

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

每個交易日收盤後也會自動檢查推播。`;

// 指令對應:[command, default_market, parser]
// parser 從 cmdText 餘下 tokens 拆出 (market, args[])
const COMMAND_TABLE = {
  '/check':     { cmd: 'check', market: 'ALL' },
  '/check_all': { cmd: 'check', market: 'ALL' },
  '/check_tw':  { cmd: 'check', market: 'TW' },
  '/check_us':  { cmd: 'check', market: 'US' },
  '/list':      { cmd: 'list',  market: 'ALL' },
  '/portfolio': { cmd: 'list',  market: 'ALL' },
};

// 多參數指令(需從訊息餘下文字拆出 market + args)
const MULTI_ARG_COMMANDS = new Set([
  '/price', '/add', '/del', '/delete', '/sold',
]);

export default {
  async fetch(request, env) {
    // 健康檢查(瀏覽器打開會看到 OK)
    if (request.method === 'GET') {
      return new Response('Stark TG Bot Worker is running ✅', { status: 200 });
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

    // 簡單指令(check / list 系列)
    if (COMMAND_TABLE[firstToken]) {
      const { cmd, market } = COMMAND_TABLE[firstToken];
      await ackAndDispatch(env, { command: cmd, market, args: '', ackLabel: prettyLabel(firstToken) });
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
  }[token] || token;
}

async function ackAndDispatch(env, { command, market, args, ackLabel }) {
  await sendMessage(env, `⏳ 收到指令,執行 <b>${ackLabel}</b> 中...`);

  const trigger = await fetch(
    `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/stock_check.yml/dispatches`,
    {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${env.GITHUB_TOKEN}`,
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'cf-worker-tg-bot',
      },
      body: JSON.stringify({
        ref: 'main',
        inputs: { command, market, args },
      }),
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
