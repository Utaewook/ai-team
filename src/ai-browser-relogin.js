// ai-browser-relogin — 상시 브라우저에서 소셜 로그인 버튼으로 다시 로그인 (비밀번호 입력 없음)
// 사용: ai-browser-relogin <사이트> [--check]      사이트 정의: /usr/local/lib/ai-team/relogin-sites.json
// 결과: 첫 줄 OK|LOGGED_IN|FAIL <이유>. 실패하면 화면 캡처를 notes/browser/ 에 남긴다.
// 비밀번호·인증·약관 동의 화면이 나오면 아무것도 누르지 않고 FAIL 로 끝낸다.
const fs = require('fs');
const path = require('path');
const os = require('os');

const LIB = process.env.AI_PW_CORE || '/usr/local/lib/ai-team/playwright-mcp/node_modules/playwright-core';
const SITES = process.env.AI_RELOGIN_SITES || '/usr/local/lib/ai-team/relogin-sites.json';
const { chromium } = require(LIB);

function botDir() {
  const base = path.join(os.homedir(), 'bots');
  for (const d of fs.readdirSync(base)) {
    if (fs.existsSync(path.join(base, d, 'playwright.json'))) return path.join(base, d);
  }
  throw new Error('playwright.json 이 없습니다 (브라우저 봇이 아님)');
}

async function loggedIn(page, site) {
  await page.goto(site.check_url, { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForTimeout(1500);
  const body = await page.locator('body').innerText({ timeout: 10000 }).catch(() => '');
  return new RegExp(site.logged_in).test(body);
}

async function main() {
  const [name, flag] = process.argv.slice(2);
  const sites = JSON.parse(fs.readFileSync(SITES, 'utf8'));
  const site = sites[name];
  if (!site) { console.log(`FAIL 모르는 사이트: ${name} (가능: ${Object.keys(sites).join(', ')})`); return 2; }
  const dir = botDir();
  const conf = JSON.parse(fs.readFileSync(path.join(dir, 'playwright.json'), 'utf8'));
  const outDir = conf.outputDir || path.join(dir, 'notes', 'browser');
  const browser = await chromium.connectOverCDP(conf.browser.cdpEndpoint, { timeout: 20000 });
  const ctx = browser.contexts()[0];
  const page = await ctx.newPage();
  let popup = null;
  const trace = [];
  const step = (msg) => trace.push(`${new Date().toISOString().slice(11, 19)} ${msg}`);
  const fail = async (why) => {
    fs.writeFileSync(path.join(outDir, `relogin-${name}.log`), trace.join('\n') + `\nFAIL ${why}\n`);
    const shot = path.join(outDir, `relogin-${name}-${Date.now()}.png`);
    await (popup && !popup.isClosed() ? popup : page).screenshot({ path: shot }).catch(() => {});
    console.log(`FAIL ${why}\n화면: ${shot}`);
    return 1;
  };
  try {
    if (await loggedIn(page, site)) { console.log('LOGGED_IN 이미 로그인되어 있음'); return 0; }
    if (flag === '--check') { console.log('FAIL 로그아웃 상태'); return 1; }
    await page.goto(site.login_url, { waitUntil: 'domcontentloaded', timeout: 30000 });
    await page.waitForTimeout(1500);
    const btn = page.locator(site.button_selector || 'a, button').filter({ hasText: new RegExp(site.button_text) })
      .or(page.locator(site.button_fallback || '[class*="kakao"], [id*="kakao"]')).first();
    if (!(await btn.count())) return await fail(`'${site.button_text}' 로그인 버튼을 찾지 못함`);
    await btn.scrollIntoViewIfNeeded().catch(() => {});
    const popupWait = ctx.waitForEvent('page', { timeout: 10000 }).catch(() => null);
    await btn.click({ force: true, timeout: 10000 });
    popup = await popupWait;
    step(`클릭 후: ${popup ? '팝업 ' + popup.url() : '같은 창 ' + page.url()}`);
    const flow = popup || page;
    // 소셜 화면: '계속하기'·계정 선택만 누른다. 입력·동의가 필요한 화면이면 멈춘다.
    const deadline = Date.now() + 45000;
    while (Date.now() < deadline) {
      try {
        if (popup && popup.isClosed()) break;
        await flow.waitForLoadState('domcontentloaded', { timeout: 10000 }).catch(() => {});
        const url = flow.url();
        step(`화면: ${url.slice(0, 120)}`);
        if (!popup && new URL(url).hostname.endsWith(site.domain)) break;          // 같은 창 방식: 사이트로 돌아옴
        const text = await flow.locator('body').innerText({ timeout: 5000 }).catch(() => '');
        if (await flow.locator('input[type="password"]').count() || /인증번호|본인\s*인증|보안\s*문자|captcha/i.test(text))
          return await fail('소셜 로그인이 비밀번호·인증을 요구함 — 사람이 로그인해야 함');
        if (/약관|동의하고|회원\s*가입/.test(text) && !/계속하기/.test(text))
          return await fail('약관 동의·회원가입 화면이 나옴 — 사람이 확인해야 함');
        const go = flow.locator('button, a, input[type="submit"]').filter({ hasText: /계속하기/ }).first();
        if (new RegExp(site.social_host || 'kakao\\.com').test(url) && await go.count()) {
          await go.click({ timeout: 5000 }).catch(() => {});
          await flow.waitForTimeout(2000).catch(() => {});
          continue;
        }
        await flow.waitForTimeout(1500).catch(() => {});
      } catch (err) {
        step(`예외: ${String(err.message).split('\n')[0]} (팝업 닫힘=${popup ? popup.isClosed() : '-'}, 창 닫힘=${page.isClosed()})`);
        if (popup && popup.isClosed()) break;   // 소셜 팝업이 스스로 닫힘 = 보통 로그인 완료
        if (page.isClosed()) throw err;
        await page.waitForTimeout(1000);
      }
    }
    await page.waitForTimeout(2000);
    if (await loggedIn(page, site)) { console.log('OK 다시 로그인함'); return 0; }
    return await fail('버튼 흐름은 끝났지만 여전히 로그아웃 상태');
  } catch (err) {
    return await fail(`오류: ${String(err.message || err).split('\n')[0]}`);
  } finally {
    if (popup && !popup.isClosed()) await popup.close().catch(() => {});
    await page.close().catch(() => {});
    await browser.close().catch(() => {});   // 연결만 끊음 (상시 브라우저는 그대로)
  }
}

main().then((code) => process.exit(code), (err) => { console.log(`FAIL ${err.message}`); process.exit(1); });
