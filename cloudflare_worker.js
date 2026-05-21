/**
 * Cloudflare Worker — Telegram → GitHub Actions 觸發器
 *
 * 流程:
 *   TG 使用者打 /check → Telegram 推 webhook → 這個 Worker →
 *   立刻回 "⏳ 收到指令" → 觸發 GitHub Actions 跑 check_stocks.py →
 *   結果由 check_stocks.py 直接推回 TG
 *
 * 環境變數(在 Cloudflare 後台「Settings → Variables」設定):
 *   TELEGRAM_TOKEN      — 你的 Telegram Bot Token(Secret)
 *   TELEGRAM_CHAT_ID    — 你的 TG chat_id(Plain text)
 *   GITHUB_TOKEN        — GitHub Personal Access Token,有 Actions:Write 權限(Secret)
 *   GITHUB_REPO         — 例如 "GGININDERR/stock-alert"(Plain text)
 *   WEBHOOK_SECRET      — 隨便一串長字串,防止外人亂打 webhook(Secret)
 */

const HELP_TEXT = `🤖 <b>Stark 停損機器人指令</b>

/check 或 /check_all — 檢查台股 + 美股
/check_tw — 只檢查台股
/check_us — 只檢查美股
/help — 顯示這則說明

每個交易日收盤後也會自動檢查。`;

const COMMANDS = {
  '/check': 'ALL',
  '/check_all': 'ALL',
  '/check_tw': 'TW',
  '/check_us': 'US',
};

export default {
  async fetch(request, env) {
    // 健康檢查(瀏覽器打開 worker URL 會看到 OK)
    if (request.method === 'GET') {
      return new Response('Stark TG Bot Worker is running ✅', { status: 200 });
    }
    if (request.method !== 'POST') {
      return new Response('Method not allowed', { status: 405 });
    }

    // 驗證 webhook secret(Telegram 設定 webhook 時帶上)
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

    // 只接受預設 chat_id 的訊息(防止外人用我的 bot)
    if (String(msg.chat?.id) !== String(env.TELEGRAM_CHAT_ID)) {
      return new Response('ignored: wrong chat');
    }

    const text = (msg.text || '').trim().toLowerCase().split('@')[0];

    if (text === '/help' || text === '/start') {
      await sendMessage(env, HELP_TEXT);
      return new Response('ok');
    }

    const market = COMMANDS[text];
    if (!market) {
      return new Response('ok'); // 不是指令就忽略
    }

    // 立刻回 ack
    await sendMessage(env, `⏳ 收到指令,觸發 <b>${market}</b> 檢查中...`);

    // 觸發 GitHub Actions
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
          inputs: { market },
        }),
      }
    );

    if (!trigger.ok) {
      const errText = await trigger.text();
      await sendMessage(env, `❌ 觸發失敗 (${trigger.status}): ${errText.slice(0, 200)}`);
    }

    return new Response('ok');
  },
};

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
