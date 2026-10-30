#!/bin/bash
# 자동 배포 첫 설치 (서버에서 한 번만):
#   sudo git clone https://github.com/<계정>/ai-team /var/lib/ai-team/src/code
#   sudo bash /var/lib/ai-team/src/code/deploy/bootstrap.sh <계정>/ai-team-config
#
# 1) /etc/ai-team/deploy.yaml 작성      2) 설정 저장소용 배포 키(쓰기) 만들기 → GitHub 에 등록 안내
# 3) ai-team-deploy·유닛 설치            4) 배포 계획(plan) 출력 — 실제 배포는 사람이 확인 후 실행
set -u
[ "$(id -u)" = 0 ] || { echo "root 로 실행하세요: sudo bash $0 <계정>/ai-team-config"; exit 1; }
CONFIG_SLUG="${1:-}"
CODE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CODE_URL="$(git -C "$CODE_DIR" remote get-url origin)"
KEY=/root/.ssh/ai-team-config
CONF=/etc/ai-team/deploy.yaml

command -v git >/dev/null || apt-get install -y -qq git >/dev/null

# 1) 설정
install -d -m 700 /etc/ai-team /var/lib/ai-team/deploy
if [ ! -f "$CONF" ]; then
  {
    echo "# ai-team-deploy 설정 (비밀값 없음)"
    echo "code_repo: $CODE_URL"
    [ -n "$CONFIG_SLUG" ] && echo "config_repo: git@github.com:$CONFIG_SLUG.git" && echo "config_key: $KEY"
    echo "branch: main"
    echo "require_ci: true      # 코드 저장소 main 의 GitHub 검사가 통과한 커밋만 배포"
    echo "restart_bots: true    # 배포 후 Telegram 봇 재시작 (새 지침·설정 반영)"
    echo "src_dir: /var/lib/ai-team/src"
  } > "$CONF"
  chmod 600 "$CONF"
  echo "✅ $CONF 작성"
else
  echo "= $CONF 이미 있음"
fi

# 2) 설정 저장소 배포 키 (root 전용, 이 저장소 하나에만 쓰기 가능)
if [ -n "$CONFIG_SLUG" ]; then
  install -d -m 700 /root/.ssh
  [ -f "$KEY" ] || ssh-keygen -q -t ed25519 -N "" -C "ai-team-deploy@$(hostname)" -f "$KEY"
  if GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" \
       git ls-remote "git@github.com:$CONFIG_SLUG.git" >/dev/null 2>&1; then
    echo "✅ 설정 저장소 접속 확인 ($CONFIG_SLUG)"
  else
    echo
    echo "🔑 아래 공개키를 설정 저장소의 배포 키로 등록하세요 (쓰기 허용):"
    echo "   https://github.com/$CONFIG_SLUG/settings/keys/new"
    echo "   Title: ai-team-deploy (VM)   ·   Key: 아래 한 줄 전체   ·   ☑ Allow write access   →  Add key"
    echo
    cat "$KEY.pub"
    echo
    read -r -p "등록했으면 Enter: " _
    GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" \
      git ls-remote "git@github.com:$CONFIG_SLUG.git" >/dev/null 2>&1 \
      && echo "✅ 설정 저장소 접속 확인" || { echo "❌ 접속 실패 — 키 등록과 'Allow write access' 를 확인한 뒤 이 스크립트를 다시 실행하세요"; exit 1; }
  fi
fi

# 3) 배포 도구·유닛 (나머지 프로그램은 첫 배포 때 manifest 대로 설치됨)
install -m 755 "$CODE_DIR/deploy/ai-team-deploy" /usr/local/sbin/ai-team-deploy
install -m 644 "$CODE_DIR/systemd/ai-team-deploy.service" "$CODE_DIR/systemd/ai-team-deploy.timer" /etc/systemd/system/
systemctl daemon-reload
echo "✅ ai-team-deploy 설치 (자동 배포는 아직 꺼짐)"

# 4) 계획
echo; echo "── 배포 계획 (아직 아무것도 바꾸지 않음) ──"
/usr/local/sbin/ai-team-deploy plan
echo
echo "확인 후:  sudo ai-team-deploy run --force   (첫 배포)   →   sudo ai-team-deploy enable   (자동 배포 켜기)"
