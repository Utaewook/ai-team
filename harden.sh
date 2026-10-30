#!/bin/bash
# VM 기본 보안 — root 로 실행:  sudo bash harden.sh
# 먼저 ubuntu 비밀번호를 정해 두세요:  sudo passwd ubuntu   (이 스크립트가 비밀번호 없는 sudo 를 없앱니다)
#
#  1) ubuntu 의 비밀번호 없는 sudo 제거 (봇이나 탈취된 프로세스가 바로 root 가 되는 경로 차단)
#  2) SSH: 키 로그인만, root 로그인 금지, ubuntu 만 접속 허용
#  3) 쓰지 않는 rpcbind(111 포트) 끄기
#  4) 보안 업데이트 자동 설치
#  5) ubuntu 를 lxd·docker 그룹에서 제외 (두 그룹 모두 사실상 root)

[ "$(id -u)" = 0 ] || { echo "root 로 실행하세요: sudo bash $0"; exit 1; }
ADMIN="${ADMIN_USER:-ubuntu}"

# 0) 안전 확인: 비밀번호와 SSH 키가 있어야 진행
if [ "$(passwd -S "$ADMIN" | awk '{print $2}')" != "P" ]; then
  echo "❌ $ADMIN 비밀번호가 없습니다. 먼저: sudo passwd $ADMIN"; exit 1; fi
if [ ! -s "/home/$ADMIN/.ssh/authorized_keys" ]; then
  echo "❌ /home/$ADMIN/.ssh/authorized_keys 가 비어 있습니다. SSH 키 로그인이 안 되는 상태에서는 진행하지 않습니다."; exit 1; fi

# 1) 비밀번호 없는 sudo 제거
for f in /etc/sudoers.d/*; do
  [ -f "$f" ] || continue
  if grep -qE "^\s*$ADMIN\s.*NOPASSWD" "$f"; then
    cp "$f" "/root/$(basename "$f").bak-$(date +%Y%m%d)"
    sed -E "s/^(\s*$ADMIN\s.*)NOPASSWD:\s*/\1/" "$f" > "$f.new"
    if visudo -cf "$f.new" >/dev/null; then mv "$f.new" "$f" && chmod 440 "$f" && echo "✅ $f: NOPASSWD 제거"
    else rm -f "$f.new"; echo "⚠️ $f 수정 검사 실패 — 그대로 둠"; fi
  fi
done
grep -rqE "^\s*$ADMIN\s.*ALL" /etc/sudoers /etc/sudoers.d/ 2>/dev/null || id -nG "$ADMIN" | grep -qw sudo \
  || echo "⚠️ $ADMIN 의 sudo 권한 줄을 찾지 못했습니다. 새 창에서 'sudo -k; sudo true' 로 확인하세요."

# 2) SSH
cat > /etc/ssh/sshd_config.d/90-ai-team.conf <<EOF
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
AllowUsers $ADMIN
EOF
if sshd -t; then systemctl reload ssh && echo "✅ SSH: 키 전용 · root 금지 · $ADMIN 만"
else rm -f /etc/ssh/sshd_config.d/90-ai-team.conf; echo "⚠️ sshd 설정 검사 실패 — 원래대로 둠"; fi

# 3) rpcbind
if systemctl list-unit-files rpcbind.service >/dev/null 2>&1; then
  systemctl disable --now rpcbind.service rpcbind.socket >/dev/null 2>&1; systemctl mask rpcbind.service rpcbind.socket >/dev/null 2>&1
  echo "✅ rpcbind 끔"; fi

# 4) 자동 보안 업데이트
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq unattended-upgrades >/dev/null
printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' > /etc/apt/apt.conf.d/20auto-upgrades
echo "✅ 자동 보안 업데이트"

# 5) 위험한 그룹 제외
for g in lxd docker; do
  id -nG "$ADMIN" | grep -qw "$g" && gpasswd -d "$ADMIN" "$g" >/dev/null && echo "✅ $ADMIN 를 $g 그룹에서 제외 (재로그인 후 적용)"
done

echo
echo "== 확인: 지금 창을 닫지 말고, 새 터미널에서 SSH 로 접속되는지 + 'sudo -k; sudo true' 가 비밀번호를 묻는지 확인하세요."
ss -tlnp | awk 'NR>1{print "  열린 포트:", $4}' | sort -u
