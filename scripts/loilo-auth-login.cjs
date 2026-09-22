"use strict";

// One-time/manual authentication helper. This is not used by the scheduled
// collector. The user completes the normal LoiLoNote sign-in in a temporary
// Chrome context; the helper stores only connect.sid in Windows Credential
// Manager and then destroys the temporary browser context.

const { spawn } = require("node:child_process");
const { chromium } = require("playwright");

async function storeSecret(value) {
  await new Promise((resolve, reject) => {
    const child = spawn(
      "py",
      ["-3", "-m", "loilo_briefing", "auth", "store-session", "--stdin"],
      { cwd: require("node:path").resolve(__dirname, ".."), stdio: ["pipe", "inherit", "inherit"] },
    );
    child.stdin.end(value + "\n");
    child.on("error", reject);
    child.on("exit", (code) => (code === 0 ? resolve() : reject(new Error(`credential store exited ${code}`))));
  });
}

async function main() {
  let browser;
  let context;
  try {
    browser = await chromium.launch({ headless: false, channel: "chrome" });
    context = await browser.newContext();
    const page = await context.newPage();
    await page.goto("https://loilonote.app/_/", { waitUntil: "domcontentloaded" });
    process.stdout.write("LoiLoNoteの正規画面でログインしてください。認証確認後、この一時ウィンドウは自動的に閉じます。\n");
    const deadline = Date.now() + 10 * 60 * 1000;
    let session;
    while (Date.now() < deadline) {
      const response = await context.request.get("https://loilonote.app/_/", { timeout: 15000 });
      const text = await response.text();
      if (response.status() === 200 && text.includes('id="initial-state"')) {
        const cookies = await context.cookies("https://loilonote.app/");
        session = cookies.find((cookie) => cookie.name === "connect.sid")?.value;
        if (session) break;
      }
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }
    if (!session) throw new Error("LoiLoNote authentication required");
    await storeSecret(session);
  } finally {
    if (context) await context.close().catch(() => {});
    if (browser) await browser.close().catch(() => {});
  }
}

main().catch((error) => {
  process.stderr.write(`${error.message}\n`);
  process.exitCode = 1;
});
