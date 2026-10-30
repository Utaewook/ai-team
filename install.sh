#!/bin/bash
# AI 팀 설치 — 저장소 폴더에서 root 로 실행:  sudo bash install.sh
# 여러 번 실행해도 안전합니다. 이미 있는 설정(team-config.yaml, jira.env, 봇 정의, 브리프)은 덮어쓰지 않습니다.
#
#  1) 패키지: python3-yaml, tmux, git, jq, 빌드 도구, Node 22, uv, GitHub CLI, Docker(rootless 전용)
#  2) 프로그램: deploy/manifest.tsv 의 core 목록 (디스패처·ai-bot·도구·유닛)
#  3) 설정: /etc/ai-team/ (예시에서 시작), 봇 정의 템플릿 → /etc/ai-team/bots.d/
#  4) systemd: 디스패처·아침 요약 유닛(아직 켜지 않음), ai-bot init(봇 유닛·공용 claude/bun·감시 타이머)

KIT="$(cd "$(dirname "$0")" && pwd)"
[ "$(id -u)" = 0 ] || { echo "root 로 실행하세요: sudo bash $0"; exit 1; }
OK=""; FAIL=""
step() { echo "▶ $1"; if eval "$2"; then OK="$OK\n  ✅ $1"; else FAIL="$FAIL\n  ❌ $1"; fi; }
export DEBIAN_FRONTEND=noninteractive

# ── 1) 패키지 ──
step "기본 패키지" 'apt-get update -qq && apt-get install -y -qq python3-yaml tmux git curl jq unzip ca-certificates gnupg \
  build-essential python3-venv uidmap dbus-user-session >/dev/null'
step "Node.js 22" 'node -v 2>/dev/null | grep -q "^v2[2-9]" || { curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null && apt-get install -y -qq nodejs >/dev/null; }'
step "uv" 'command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh >/dev/null'
step "GitHub CLI" 'command -v gh >/dev/null || { install -d -m 755 /etc/apt/keyrings &&
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg &&
  chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg &&
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list &&
  apt-get update -qq && apt-get install -y -qq gh >/dev/null; }'
step "Docker 패키지" 'command -v dockerd-rootless-setuptool.sh >/dev/null || { install -d -m 755 /etc/apt/keyrings &&
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc && chmod a+r /etc/apt/keyrings/docker.asc &&
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" > /etc/apt/sources.list.d/docker.list &&
  apt-get update -qq && apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin docker-ce-rootless-extras >/dev/null; }'
# root 권한 Docker 데몬은 쓰지 않음 (docker 그룹 = 사실상 root). 봇은 자기 사용자의 rootless Docker 를 씀.
step "root Docker 데몬 끄기" 'systemctl disable --now docker.service docker.socket >/dev/null 2>&1; systemctl mask docker.service docker.socket >/dev/null 2>&1;
  rm -f /var/run/docker.sock; for u in $(getent group docker | cut -d: -f4 | tr , " "); do gpasswd -d "$u" docker >/dev/null; done; true'
# Ubuntu 24.04+ 는 비특권 user namespace 를 AppArmor 로 제한 → rootlesskit 에만 허용
step "AppArmor (rootlesskit)" '[ ! -d /etc/apparmor.d ] || [ -f /etc/apparmor.d/usr.bin.rootlesskit ] || [ ! -x /usr/bin/rootlesskit ] ||
  { printf "%s\n" "abi <abi/4.0>," "include <tunables/global>" "" "/usr/bin/rootlesskit flags=(unconfined) {" "  userns," "" "  include if exists <local/usr.bin.rootlesskit>" "}" \
    > /etc/apparmor.d/usr.bin.rootlesskit && systemctl restart apparmor.service; }'

# ── 2) 프로그램 ──
# 설치할 파일 목록은 deploy/manifest.tsv 하나로 관리 (자동 배포 ai-team-deploy 도 같은 목록을 씀)
step "프로그램 (deploy/manifest.tsv 의 core)" 'install -d -m 755 /opt/ai-team /usr/local/sbin /usr/local/lib/ai-team &&
  grep -v "^#" "$KIT/deploy/manifest.tsv" | while IFS=$(printf "\t") read -r g src dst mode; do
    [ "$g" = core ] || continue; install -D -m "$mode" "$KIT/$src" "$dst" || exit 1; done'

# ── 3) 설정 (없을 때만 예시에서 생성) ──
step "설정 폴더" 'install -d -m 700 /etc/ai-team /etc/ai-team/bots.d && install -d -m 755 /etc/ai-team-projects && install -d -m 755 /var/lib/ai-team &&
  { [ -f /etc/ai-team/team-config.yaml ] || install -m 600 "$KIT/config/team-config.example.yaml" /etc/ai-team/team-config.yaml; } &&
  { [ -f /etc/ai-team/jira.env ] || install -m 600 "$KIT/config/jira.env.example" /etc/ai-team/jira.env; } &&
  { [ -f /etc/ai-team-projects/OPS.md ] || install -m 644 "$KIT/context/OPS.md" /etc/ai-team-projects/OPS.md; } &&
  install -m 644 "$KIT/context/PRODUCT.md.example" /etc/ai-team-projects/PRODUCT.md.example'
step "봇 정의 템플릿" 'for d in "$KIT"/bots/*/; do n=$(basename "$d");
    if [ -e "/etc/ai-team/bots.d/$n" ]; then echo "   = $n: 이미 있음, 그대로 둠"; continue; fi
    install -d -m 700 "/etc/ai-team/bots.d/$n" && install -m 600 "$d"* "/etc/ai-team/bots.d/$n/" && echo "   + $n";
  done; true'

# ── 4) systemd ──
step "프로젝트 목록 (INDEX.md)" '/usr/local/sbin/ai-team-update-instructions --index-only'
step "디스패처 유닛 (아직 켜지 않음)" 'systemctl daemon-reload'
step "ai-bot init (봇 유닛·공용 claude/bun·감시)" '/usr/local/sbin/ai-bot init'

echo; echo "== 설치 결과"; printf "$OK"; printf "$FAIL"; echo
echo "== 버전"
node -v 2>/dev/null; uv --version 2>/dev/null; gh --version 2>/dev/null | head -1; /usr/local/bin/claude --version 2>/dev/null; /usr/local/bin/bun --version 2>/dev/null | sed 's/^/bun /'
echo
echo "다음: 문서의 '5. Jira' → jira.env 채우기 → sudo python3 /opt/ai-team/jira-setup.py --approver-email <내 이메일>"
[ -z "$FAIL" ]
