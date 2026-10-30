#!/bin/bash
# 브라우저 봇(선택) 설치 — sudo bash install-browser.sh   (install.sh 다음에)
# 가상 화면·VNC·한글 글꼴 + Playwright MCP + 맞는 Chromium + 클릭 안전장치(ai-browser-guard)
KIT="$(cd "$(dirname "$0")" && pwd)"; L=/usr/local/lib/ai-team; B=$L/ms-playwright
[ "$(id -u)" = 0 ] || { echo "root 로 실행하세요"; exit 1; }
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq xvfb x11vnc novnc websockify xauth fonts-noto-cjk >/dev/null || { echo "❌ 패키지"; exit 1; }
install -d -m 755 $L/playwright-mcp
npm install --silent --prefix $L/playwright-mcp @playwright/mcp@0.0.83 >/dev/null 2>&1 || { echo "❌ Playwright MCP"; exit 1; }
PLAYWRIGHT_BROWSERS_PATH=$B node $L/playwright-mcp/node_modules/playwright-core/cli.js install-deps chromium >/dev/null 2>&1
PLAYWRIGHT_BROWSERS_PATH=$B node $L/playwright-mcp/node_modules/playwright-core/cli.js install chromium >/dev/null || { echo "❌ Chromium"; exit 1; }
chmod -R a+rX $L/playwright-mcp $B
grep -v "^#" "$KIT/deploy/manifest.tsv" | while IFS=$(printf "\t") read -r g src dst mode; do
  [ "$g" = browser ] && install -D -m "$mode" "$KIT/$src" "$dst"; done
/usr/local/sbin/ai-bot init --skip-binaries >/dev/null
if [ ! -e /etc/ai-team/bots.d/concierge ]; then
  install -d -m 700 /etc/ai-team/bots.d/concierge && install -m 600 "$KIT"/bots-optional/concierge/* /etc/ai-team/bots.d/concierge/
  echo "+ concierge 정의 (bot.yaml 의 browser.allow 에 쓸 사이트 도메인을 넣으세요)"
fi
echo "✅ 브라우저 봇 준비. 다음: sudo ai-bot add concierge → sudo ai-bot browser-login concierge <로그인 URL…>"
