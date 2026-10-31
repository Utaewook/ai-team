# 개인 AI 팀 구축 가이드 (OCI Always Free + Claude Code + Telegram + Jira)

> 최종 갱신: 2026-10-29 · 기준 환경: OCI Always Free Ampere VM (Ubuntu 24.04, ARM) · Claude Code 2.1.x
> 이 저장소만 있으면 빈 계정에서 시작해 같은 구성을 다시 만들 수 있다.
> 비밀 값(토큰·비밀번호)은 이 문서에 적지 않는다.

Telegram 으로 말을 걸면 Claude Code 봇 3개(**leader · dev · research**)가 일을 나눠 받는다.
작업은 Jira 에 기록되고, 디스패처가 정해진 시간대(주로 야간)에 실행한 뒤 결과를 Jira 코멘트와 Telegram 으로 보고한다.
봇은 각자 다른 리눅스 사용자로 돌아서 서로의 파일·토큰을 볼 수 없다.

---

## 목차
1. [전체 구조](#1-전체-구조)
2. [준비물과 비용](#2-준비물과-비용)
3. [OCI VM 만들기](#3-oci-vm-만들기)
4. [첫 접속, 설치, 기본 보안](#4-첫-접속-설치-기본-보안)
5. [Jira 준비](#5-jira-준비)
6. [Telegram·Claude·GitHub 토큰 준비](#6-telegramclaudegithub-토큰-준비)
7. [봇 만들기](#7-봇-만들기)
8. [디스패처 켜기](#8-디스패처-켜기)
9. [테스트 시나리오](#9-테스트-시나리오)
10. [사용법](#10-사용법)
11. [운영 명령어](#11-운영-명령어)
12. [보안 모델](#12-보안-모델)
13. [트러블슈팅과 배운 것](#13-트러블슈팅과-배운-것)
14. [자동 배포 (GitHub → VM)](#14-자동-배포-github--vm)
15. [정기 점검](#15-정기-점검)
16. [부록: 저장소 구성, 설계 결정](#16-부록)

---

## 1. 전체 구조

```
나 ──Telegram──▶ leader 봇 (bot-leader · Claude Code + Telegram 플러그인, tmux, 상시 실행)
   │               │ 팀원에게 맡길 일은 요청 파일로만 남김 (Jira 토큰 없음)
   │               ▼
   │      /home/bot-leader/bots/leader/outbox/*.json
   │               │
   │               ▼  매분 (systemd timer, root)
   │      디스패처 /opt/ai-team/dispatcher.py
   │        ├─ 요청 검증 → Jira 이슈 생성 / 승인 / 재예약 / 관리 요청
   │        ├─ Jira 조회 → 시간 창·Run At·예산 확인 → 프로젝트별 공정 분배
   │        └─ 러너 실행 ──▶ claude -p  (bot-<봇> 사용자 · 그 봇의 Claude 토큰 · 봇/레포 폴더)
   │                          └─ 결과(서식 있는 코멘트)·사용량·세션 → Jira, 알림 → Telegram
   ├─Telegram──▶ dev 봇 (bot-dev · 레포·rootless Docker·GitHub 토큰)
   └─Telegram──▶ research 봇 (bot-research · 웹 조사만, 권한 최소)

Jira = 단일 진실 원천 (상태·핸드오프·사용량)   07:30 아침 요약 → Telegram
```

**핵심 원칙**
- 언제 실행할지는 코드(디스패처)가, 무엇을 할지는 Claude 가 정한다.
- 봇 하나 = 리눅스 사용자 하나(`bot-<이름>`). 사람은 `ubuntu` 만 쓴다.
- Jira 토큰은 root 의 디스패처만 가진다. 봇끼리 직접 위임하지 않고 모든 위임은 Jira 를 거친다.
- 봇은 자기 지침·권한 파일을 고칠 수 없다(root 소유). 관리자 권한이 필요한 일은 내가 Jira 에서 승인해야 실행된다.
- Claude 구독을 나와 같이 쓰므로 봇 작업은 내가 덜 쓰는 야간(01:00~07:00)에 몰아서 실행한다.

| 구성 | 위치 |
|---|---|
| 봇 관리 도구 | `/usr/local/sbin/ai-bot` (`sudo ai-bot …`) |
| 봇 정의 (원본) | `/etc/ai-team/bots.d/<봇>/{bot.yaml,CLAUDE.md,settings.json}` — root 700 |
| 봇 서비스 | `ai-bot@<봇>.service` (시스템 유닛, `User=bot-<봇>`, tmux 소켓 `bot-<봇>`) |
| 봇 작업 폴더 | `/home/bot-<봇>/bots/<봇>/` — root 소유·봇 읽기 전용. 봇이 쓰는 하위 폴더(`outbox`, `notes`)만 봇 소유 |
| 봇 토큰 | Claude `~/.config/ai-bot/claude.env` · Telegram `~/.claude/channels/telegram-<봇>/.env` · GitHub `~/.git-credentials` (모두 그 봇 홈, 600) |
| 레포 | `/home/bot-dev/projects/<레포>` |
| 볼트(선택) | `/home/bot-leader/vault` + `ai-vault-sync@leader.timer` |
| 공용 실행 파일 | `/usr/local/bin/{claude,bun}` → `/usr/local/lib/ai-team/` (root 소유, 자동 업데이트 끔) |
| 디스패처 | `/opt/ai-team/dispatcher.py`, 설정 `/etc/ai-team/team-config.yaml`, Jira 토큰 `/etc/ai-team/jira.env` |
| 프로젝트 브리프 | `/etc/ai-team-projects/<Product>.md` (root 644) |
| 상태 | `/var/lib/ai-team/` (사용량 장부 `usage.jsonl`, 재예약 메모, 감시 상태) |
| 타이머 | `ai-team-dispatch.timer`(매분) · `ai-team-summary.timer`(07:30) · `ai-bot-watchdog.timer`(2분) |

---

## 2. 준비물과 비용

| 준비물 | 용도 | 비용 |
|---|---|---|
| Oracle Cloud 계정 | VM (Always Free) | 0원 (카드 인증 필요) |
| Claude Pro 또는 Max 구독 | 봇이 쓰는 Claude Code | 기존 구독 그대로 (사용량을 나와 공유) |
| Telegram 계정 | 봇과 대화 | 0원 |
| Atlassian(Jira Cloud Free) | 작업 추적 | 0원 (10명까지) |
| GitHub 계정 | 코드 레포, (선택) Obsidian 볼트 | 0원 |
| PC 의 SSH 클라이언트 | VM 접속 | — |

이메일 하나로 Jira 봇 계정을 따로 만들 때는 Gmail 의 `+` 별칭을 쓰면 편하다 (예: `me+jirabot@gmail.com`).

---

## 3. OCI VM 만들기

> 콘솔 화면 이름은 바뀔 수 있다. 핵심 값만 맞추면 된다.

1. **가입**: oracle.com/cloud/free → 가입. **홈 리전은 나중에 바꿀 수 없다.** 가까운 리전(예: 서울, 춘천)을 고른다.
2. **SSH 키 만들기 (PC 에서)**
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/oci_devbox -C "dev-box"
   ```
   `~/.ssh/oci_devbox.pub` 내용을 VM 생성 때 붙여넣는다. 개인 키(`oci_devbox`)는 PC 밖으로 보내지 않는다.
3. **인스턴스 생성**: 메뉴 → Compute → Instances → Create instance
   - 이름: `dev-box`
   - Image: **Canonical Ubuntu 24.04** (aarch64)
   - Shape: **Ampere `VM.Standard.A1.Flex`** · OCPU **2** · 메모리 **12GB** (Always Free 한도는 합계 4 OCPU / 24GB)
   - Networking: 새 VCN + **public subnet**, **공인 IPv4 할당**
   - SSH keys: 위의 공개 키 붙여넣기
   - Boot volume: 기본 약 47GB → 100GB 정도로 늘려도 무료 범위(블록 스토리지 합계 200GB)
   - `Out of host capacity` 가 나오면 다른 가용 도메인(AD)을 고르거나 시간을 두고 다시 시도
4. **네트워크**: VCN → Security List 의 인바운드는 **TCP 22 만** 남긴다(기본값). 가능하면 22 를 내 IP 대역으로 제한.
   Ubuntu 이미지 안의 iptables 도 기본으로 22 외에는 막혀 있다. 웹 서비스를 열 계획이 없으면 그대로 둔다.
5. **(권장) Pay As You Go 로 업그레이드**: Always Free 한도 안에서는 여전히 0원이고, 오래 한가한 무료 인스턴스가
   회수되는 정책을 피할 수 있다. 업그레이드했다면 **Billing → Budgets 에서 예산 알림**(예: 월 1달러)을 꼭 만든다.
6. **PC 의 `~/.ssh/config`**
   ```
   Host dev-box
     HostName <공인 IP>
     User ubuntu
     IdentityFile ~/.ssh/oci_devbox
   ```
   이후 `ssh dev-box` 로 접속.

---

## 4. 첫 접속, 설치, 기본 보안

### 4.1 저장소를 VM 에 받기
```bash
git clone https://github.com/Utaewook/ai-team ~/ai-team
```
(직접 고쳐 쓸 거면 fork 한 뒤 자기 저장소 주소로)

### 4.2 설치
```bash
cd ~/ai-team
sudo bash install.sh
```
하는 일: 패키지(Node 22, uv, GitHub CLI, Docker rootless 용 패키지, tmux, PyYAML) → 디스패처·`ai-bot`·볼트 동기화 설치 →
`/etc/ai-team/` 설정 예시 → 봇 정의 템플릿을 `/etc/ai-team/bots.d/` 로 → systemd 유닛 → `ai-bot init`(공용 claude/bun 설치, 감시 타이머).
여러 번 실행해도 안전하고, 이미 있는 설정은 덮어쓰지 않는다. 마지막 "설치 결과"에 ❌ 가 없어야 한다.

> root 권한 Docker 데몬은 일부러 끈다(mask). `docker` 그룹은 사실상 root 라서, 봇은 자기 사용자의 **rootless Docker** 만 쓴다.

### 4.3 기본 보안
```bash
sudo passwd ubuntu        # sudo 비밀번호 정하기 (잊지 않게 비밀번호 관리자에 저장)
sudo bash harden.sh
```
하는 일: 비밀번호 없는 sudo 제거 · SSH 키 전용/root 로그인 금지/ubuntu 만 허용 · rpcbind 끄기 · 자동 보안 업데이트 · ubuntu 를 `lxd`·`docker` 그룹에서 제외.

**지금 창을 닫지 말고** 새 터미널에서 `ssh dev-box` 가 되는지, `sudo -k; sudo true` 가 비밀번호를 묻는지 확인한다. 그룹 변경은 재접속 후 적용된다.

---

## 5. Jira 준비

### 5.1 사이트와 스페이스
1. Jira Cloud Free 가입 → 사이트 주소 정하기(`<사이트>.atlassian.net`)
2. **팀 관리형 소프트웨어 스페이스** 생성 → 키 **`TEAM`** → 스프린트 기능은 끈다(칸반처럼 사용)
3. **워크플로**(스페이스 설정 → 작업 유형 → 작업 → 워크플로 편집). 모든 상태 사이 전환을 허용한다.
   `해야 할 일` · **`Scheduled`** · `진행 중` · **`Paused`** · `검토 중` · `완료` · **`Failed`**
4. 보드 컬럼에 Scheduled · Paused · Failed 추가
5. **작업 유형 '작업'에 커스텀 필드 7개** (이름을 정확히 이렇게)

   | 필드 이름 | 타입 | 값 |
   |---|---|---|
   | `Agent` | 드롭다운 | `leader` / `dev` / `research` (봇을 추가하면 옵션도 추가) |
   | `Product` | 드롭다운 | `OPS` (+ 제품 키, 예: `APP`) |
   | `Window` | 드롭다운 | `ASAP` / `Night` / `Weekend` |
   | `Run At` | 날짜/시간 | 이 시각 이전에는 시작하지 않음 |
   | `Max Turns` | 숫자 | 작업당 턴 상한 |
   | `Usage` | 숫자 | 디스패처가 기록 |
   | `Session ID` | 짧은 텍스트 | 디스패처가 기록 |

### 5.2 봇 전용 계정과 토큰
1. 설정 → 사용자 관리 → **초대**: `me+jirabot@gmail.com` (Jira 사용자 권한만)
2. **시크릿 창**에서 초대 메일로 가입 (표시 이름 예: `ai-team-bot`) → 스페이스에 **구성원** 역할로 추가
3. 봇 계정으로 로그인한 상태에서 **범위 지정 API 토큰** 발급(계정 설정 → 보안 → API 토큰 → 범위가 있는 토큰)
   - 앱: Jira · 범위 **세 개**: `read:jira-work`, `write:jira-work`, `read:jira-user` · 만료 1년
   - 범위 지정 토큰은 `api.atlassian.com/ex/jira/<cloudId>` 주소로만 동작한다(디스패처가 알아서 처리).

### 5.3 VM 에 등록하고 ID 찾기
```bash
sudo nano /etc/ai-team/jira.env        # JIRA_URL, JIRA_EMAIL(봇 계정), JIRA_TOKEN 채우기 (줄 끝 주석 금지)
sudo python3 /opt/ai-team/jira-setup.py --approver-email <내 Jira 계정 이메일>
```
`jira-setup.py` 는 읽기만 한다. Cloud ID 가 비어 있으면 찾아서 알려 주고(→ `JIRA_CLOUD_ID=` 에 넣기),
작업 유형·상태·필드 ID 와 **승인자(나) 계정 ID** 를 찾아 `team-config.yaml` 에 붙여넣을 내용을 출력한다.
"⚠️ 확인할 것"이 나오면 Jira 쪽 이름·워크플로를 고친 뒤 다시 실행한다.

---

## 6. Telegram·Claude·GitHub 토큰 준비

토큰은 **채팅·문서·명령줄에 붙여넣지 않는다.** VM 에서는 `sudo ai-bot secret …` 의 숨김 입력으로만 넣는다.

### 6.1 Telegram 봇 3개
1. Telegram 에서 **@BotFather** → `/newbot` → 이름·사용자명(`…_bot`) → 토큰 받기. leader, dev, research 세 번 반복.
2. **내 Telegram 사용자 ID 찾기**: 방금 만든 봇 아무거나에 "hi" 를 보낸 뒤 VM 에서
   ```bash
   read -rs T; curl -s "https://api.telegram.org/bot$T/getUpdates" | jq '.result[].message.chat.id' | sort -u; unset T
   ```
   (`read -rs` 는 입력을 화면과 셸 기록에 남기지 않는다.) 나온 숫자를 `team-config.yaml` 의 `telegram.chat_id` 에 넣는다.
   봇은 이 ID 의 메시지만 받는다(static 접근 목록).

### 6.2 Claude 토큰 3개
봇마다 따로 발급한다(하나만 폐기할 수 있게). PC 나 VM 의 ubuntu 셸에서:
```bash
claude setup-token        # 브라우저 로그인 → 1년짜리 토큰 출력. 봇 수만큼 반복
```
출력된 토큰은 바로 다음 단계(7)에서 숨김 입력으로 넣고, 따로 저장하지 않는다. **만료일(1년 뒤)을 캘린더에 등록.**

### 6.3 GitHub
- **dev 용 fine-grained 토큰**: Settings → Developer settings → Fine-grained tokens
  - Repository access: **Only select repositories** → 봇이 작업할 코드 레포만
  - Permissions: Contents **Read and write**, Pull requests **Read and write** (Metadata 읽기는 자동). Workflows·Administration 은 주지 않는다.
- **(선택) leader 볼트용 토큰**: 볼트 저장소 **하나만**, Contents Read and write
- **브랜치 보호**(각 코드 레포 → Settings → Rules → Rulesets)
  - `main`: Require a pull request · Block force pushes · Restrict deletions
  - `develop`: Block force pushes · Restrict deletions

---

## 7. 봇 만들기

### 7.0 먼저 설정 두 줄
봇을 만들기 전에 `team-config.yaml` 에 이 두 값을 넣는다 (봇의 Telegram 접근 목록과 지침에 쓰인다).
```bash
sudo nano /etc/ai-team/team-config.yaml
#   owner_name: "<봇들이 부를 내 이름>"
#   telegram.chat_id: "<6.1 에서 찾은 숫자 ID>"   ← 숫자가 아니면 봇 생성이 멈춘다
```

### 7.1 정의 확인·수정
`install.sh` 가 `/etc/ai-team/bots.d/{leader,dev,research}/` 에 템플릿을 넣어 두었다.
```bash
sudo nano /etc/ai-team/bots.d/dev/bot.yaml
#   github.must_see: [<계정>/<코드 레포>]      ← 토큰이 이 레포를 볼 수 있는지 검사
#   github.must_not_see: [<계정>/<볼트 저장소>]  ← 이 저장소가 보이는 토큰이면 등록 거부
#   repos: Product → projects/<레포>
#   git: 커밋 작성자 이름·이메일
sudo nano /etc/ai-team/bots.d/leader/bot.yaml   # 볼트를 쓰면 vault: 줄 주석 해제
```
`CLAUDE.md` 는 역할 지침, `settings.json` 은 Claude 권한 규칙이다. 맨 위의 **"Telegram 응답"·"프로젝트 맥락" 규칙은 지우지 않는다**(없으면 봇이 답을 화면에만 쓰거나, 특정 프로젝트를 기본값으로 가정한다).
지침은 `sudo ai-team-update-instructions` 로 역할별 표준안을 다시 생성할 수 있다. 직접 고친 내용이 있다면 그 명령 대신 `--index-only`(프로젝트 목록만 갱신)를 쓴다 — 전체 재생성은 leader·dev·research(·architect) 지침을 덮어쓴다(이전 것은 `/root/ai-team-migration/` 에 백업).

### 7.2 만들기 (봇마다)
```bash
sudo ai-bot add leader      # 사용자 bot-leader 생성 → 토큰 입력(숨김): claude → telegram → (볼트 쓰면) github → 시작
sudo ai-bot add research
sudo ai-bot add dev         # dev 는 rootless Docker 설정까지 (1~2분)
```
입력을 받는 명령이라 **한 줄씩 직접** 친다(여러 줄을 붙여넣으면 다음 줄이 토큰 입력으로 들어간다).

### 7.3 dev 레포 준비
```bash
sudo ai-bot shell dev                       # bot-dev 로 셸 열기
mkdir -p ~/projects/sandbox && git -C ~/projects/sandbox init -b main
git clone https://github.com/<계정>/<레포>.git ~/projects/<레포>   # 토큰은 등록돼 있어 묻지 않음
git -C ~/projects/<레포> checkout develop
exit
sudo ai-bot apply dev                       # 레포 폴더를 Claude 신뢰 목록·권한에 반영
```

### 7.4 확인
```bash
sudo ai-bot check leader; sudo ai-bot check dev; sudo ai-bot check research   # Claude 토큰 실제 응답
sudo ai-bot status        # 서비스 active · 플러그인 ok · 토큰 모두 ✓
```
플러그인이 `ok` 가 아니면 1~2분 기다린다. 계속 안 되면 `sudo ai-bot logs <봇>` 으로 화면을 본다.

---

## 8. 디스패처 켜기

```bash
sudo nano /etc/ai-team/team-config.yaml
#   jira.site, jira-setup.py 출력(jira/admin 부분), products(비율), default_product
sudo ai-team-update-instructions --index-only                # products 를 바꿨으면 프로젝트 목록 갱신
sudo python3 /opt/ai-team/dispatcher.py notify-test          # Telegram 알림이 오면 OK
sudo python3 /opt/ai-team/dispatcher.py tick --dry-run       # 오류 없이 "실행할 작업 없음" 등이 나오면 OK
sudo sed -i 's/^dry_run: true/dry_run: false/' /etc/ai-team/team-config.yaml
sudo systemctl enable --now ai-team-dispatch.timer ai-team-summary.timer
```

**프로젝트(Product) 추가**
1. Jira `Product` 드롭다운에 옵션 추가 (예: `APP`)
2. `team-config.yaml` → `products` 에 비율 추가
3. dev 정의 `repos` 에 `APP: projects/<레포>` → 레포 clone(7.3) → `sudo ai-bot apply dev`
4. 브리프: `sudo cp /etc/ai-team-projects/PRODUCT.md.example /etc/ai-team-projects/APP.md && sudo nano …` (root 644 유지, 첫 줄 `# 제목` 이 목록에 표시됨)
5. `sudo ai-team-update-instructions --index-only` → 봇들이 보는 프로젝트 목록(`INDEX.md`)에 반영. 봇 재시작은 필요 없다(요청마다 파일을 읽음)

---

## 9. 테스트 시나리오

설치 직후와 큰 변경 뒤에 순서대로 돌린다. 프롬프트는 Telegram 으로 보낸다.

| # | 대상 | 보낼 말 | 기대 결과 |
|---|---|---|---|
| T1 | leader·dev·research 각각 | `안녕, 너는 무슨 역할이야?` | 세 봇 모두 Telegram 으로 답장 |
| T2 | leader | `research한테 "Claude Code 최신 버전과 최근 주요 변경점" 조사 맡겨줘. OPS, 급해.` | 📝 등록 알림(TEAM-N) → 1~2분 내 실행 → ✅ 검토 대기 알림, Jira 에 서식 있는 보고서 |
| T3 | leader | `TEAM-N 승인` | 🏁 완료 알림, Jira 완료 |
| T4 | leader | `dev한테 OPS 작업 맡겨줘, 급해: sandbox 레포에 hello.py 와 pytest 테스트를 만들고 테스트 통과 확인, docker run --rm python:3.12-slim python -c "print(42)" 로 도커 동작 확인, 커밋까지만(푸시 금지).` | ✅ 검토 대기, 보고서에 테스트·도커 결과. `sudo ai-bot shell dev` → `git -C ~/projects/sandbox log --oneline` 에 커밋 |
| T5 | leader | `TEAM-(T4) 다시 해줘, 지금. 메모: 실패하는 경우 테스트도 하나 추가해줘` | 재예약 알림, 실행 때 메모가 봇에게 전달(보고서에 반영) |
| T6 | leader | `VM에 tree 패키지 설치해줘` | 🔐 관리 요청 알림(명령 전문) → **Jira 에서 내가 Scheduled 로 옮기면** root 로 실행 → 결과 알림 |
| T7 | leader (볼트 설정 시) | `볼트 최상위 폴더 목록 알려줘` → `볼트에 "AI팀 테스트.md" 노트 만들고 오늘 날짜 한 줄 적어줘` | 목록 답장, 5분 안에 GitHub·Obsidian 에 노트. 다른 기기에서 같은 줄을 고치면 그 내용이 남음(원격 우선) |
| T8 | research | `/home/bot-dev/projects 안에 뭐가 있는지 알려줘` | 볼 수 없다고 답함 (사용자 분리 확인) |
| T9 | leader | `research한테 내일 새벽에 "OCI Always Free 정책 변경 사항" 조사 맡겨줘. OPS.` | Window=Night 로 등록, 01:00 이후 실행, 07:30 아침 요약에 포함 |
| T10 | VM | `sudo ai-bot restart research` 후 2분 뒤 `sudo ai-bot status` | 플러그인 ok 로 복귀 (감시가 필요하면 자동 재시작 + 🔄 알림) |

결과 확인: Jira 보드, `sudo journalctl -u ai-team-dispatch --since today -o cat`, `sudo ai-bot status`.

---

## 10. 사용법

### Telegram 으로 지시 (기본)
leader 에게 자연어로 말한다. 팀원에게 가는 일은 leader 가 Jira 에 등록 요청한다.
- `dev한테 <제품> 노드 툴팁 개선, 오늘 밤에` → Night
- `research한테 ○○ 조사해줘, 급해` → ASAP
- `TEAM-12 승인` → 검토 중 → 완료
- `TEAM-12 다시 해줘, 내일 새벽에` → 재예약
- `TEAM-12 다시 해줘, 메모: uv 로 설치해봐` → 재예약 + 메모(다음 실행 때 봇에게 전달)
- dev·research 에게 직접 말을 걸면 그 봇이 대화형으로 바로 처리한다(Jira 기록 없음).

알림: 📝 등록 · ✅ 검토 대기 · ⚠️ 일부 권한 거부 · ❌ 실패 · ⏸ 중단 · 🏁 승인 · 🔐 관리 요청 · ☀️ 아침 요약 · 🔄 봇 자동 재시작

### Jira 에서 직접 등록
작업 유형 '작업', **Agent 필수**, 상태를 **Scheduled** 로. Agent 가 비어 있으면 사람 작업으로 보고 디스패처가 건드리지 않는다.

| 필드 | 의미 |
|---|---|
| Window | Night = 01:00~07:00 에만 · ASAP = 언제든 · Weekend = 토·일만 (비우면 Night) |
| Run At | 이 시각 이전에는 시작하지 않음 |
| Product | 사용량 분배 단위 + dev 의 작업 레포 + 주입할 브리프 |
| Max Turns | 넘으면 Failed. 늘리고 Scheduled 로 바꾸면 같은 세션으로 이어서 |

### 실행 결과 처리
| 결과 | 상태 |
|---|---|
| 정상 완료 | 검토 중 (보고서: 요약 / 변경 사항 / 남은 일 / 다음 담당자에게) |
| 완료했지만 중간에 권한 거부 | 검토 중 + ⚠️ (거부된 호출 목록 첨부) |
| Max Turns 도달 · 오류 · 결과 없음 · 봇 토큰 없음 | Failed |
| 시간 창 종료 · 최대 실행 시간 초과 · 사용량 한도 · 프로세스 소실 | Paused → 다음 창에 같은 세션으로 이어서 |

### 사용량 분배
- 사이클 = 01:00 ~ 다음 01:00, 상한 `budget.per_cycle` (Claude 가 보고하는 비용 환산값 합계)
- `products` 비율 대비 **덜 쓴 프로젝트의 작업부터** 실행, 동시 실행 수는 `max_concurrent`

### 승인형 관리 명령 (sudo 대신)
```
봇 보고서의 "## 관리자 명령 요청" 또는 leader 에게 "○○ 설치해줘"
  → Jira 관리 요청 이슈(라벨 admin-request) + Telegram 🔐 알림(명령 전문)
  → 내가 명령을 읽고 Jira 에서 Scheduled 로 이동 (거절은 완료로)
  → 디스패처가 '마지막 상태 변경이 내 계정 → Scheduled' 인지 다시 확인 후 root 로 실행 (20분 제한)
  → 결과 코멘트·알림, 성공하면 요청을 낸 작업 자동 재예약
```
**승인 전에 명령을 반드시 읽는다.** 요청은 봇이 쓴 것이라 프롬프트 인젝션의 결과일 수 있다.

### leader 에게 Jira 직접 권한 주기 (선택)
기본은 leader 가 outbox 요청 파일로만 Jira 를 바꾼다(디스패처가 검증·알림). 더 유연하게 쓰려면:
1. Jira 에 **leader 전용 계정**을 초대·가입시키고 스페이스 구성원으로 추가 (변경 기록이 구분되고, 토큰이 새면 그 계정만 막으면 됨)
2. 그 계정으로 범위 지정 토큰 발급 (`read:jira-work`, `write:jira-work`, `read:jira-user`) → `sudo ai-bot secret leader jira`
   — 관리 요청 **승인자(사람) 계정의 토큰은 등록이 거부**된다 (봇이 관리 명령을 스스로 승인하지 못하게)
3. `bots.d/leader/bot.yaml` 에 `jira: true` → `sudo ai-team-update-instructions && sudo ai-bot apply leader && sudo ai-bot restart leader`

leader 는 `ai-jira list/search/show/create/update/move/comment` 를 쓴다. 삭제 명령은 없고, 관리 요청 이슈는 읽기만 되며, 진행 중 상태는 디스패처만 바꾼다.
직접 조작에는 디스패처 알림이 가지 않으므로 leader 가 바뀐 내용을 직접 알린다. 메모를 붙인 재예약과 관리 요청은 계속 outbox 로 한다.

### 확인 서비스와 Gmail·Calendar (선택)
되돌릴 수 없는 행동(메일 발송 등)은 **확인 서비스**(`ai-approve`, root)만 실행할 수 있다. 봇이 요청하면 서비스가 내용과 **6자리 일회용 코드**를 Telegram 으로 보내고, 사람이 그 코드를 봇에게 답장해야 실행된다. 코드는 그 요청 하나에만 맞고 15분 뒤 만료, 3번 틀리면 취소된다. 발송 토큰(`gmail.send` 만)은 root 전용 파일에 있어 봇은 직접 보낼 수 없다.

**Google Cloud 준비 (1회)**
1. console.cloud.google.com → 새 프로젝트 (예: `ai-team`)
2. API 및 서비스 → 라이브러리 → **Gmail API**, **Google Calendar API** 사용 설정
3. Google 인증 플랫폼(OAuth 동의 화면) → 시작하기: 앱 이름, 지원 이메일, 대상 **외부**, 연락처
4. 대상(Audience) → **앱 게시 → 프로덕션**. *테스트* 상태로 두면 refresh 토큰이 7일마다 만료된다. 검증 신청은 하지 않아도 본인 계정은 쓸 수 있다(동의 때 "확인되지 않은 앱" 경고 → 고급 → 이동)
5. 클라이언트 → 클라이언트 만들기 → 유형 **데스크톱 앱** → 클라이언트 ID·보안 비밀번호 확인

**VM**
```bash
sudo ai-approve setup-google            # 발송 전용 토큰 (gmail.send). 처음에 클라이언트 ID·비밀번호를 한 번 묻는다
sudo ai-bot secret leader google        # leader 토큰 (gmail.readonly + calendar.events). 발송 권한이 섞이면 거부
# bots.d/leader/bot.yaml: google: true, approve: [mail.send]
sudo ai-team-update-instructions && sudo ai-bot apply leader && sudo ai-bot restart leader
```
동의는 PC 브라우저에서 하고, 동의 후 "사이트에 연결할 수 없음" 페이지의 **주소창 전체**를 VM 입력란에 붙여넣는다(토큰은 화면에 나오지 않는다). 대기 요청·기록: `sudo ai-approve status`.

### 브라우저 봇 concierge (선택)
로그인이 필요한 사이트(채용·커리어 등)를 대신 다루는 봇. 다른 봇의 토큰·볼트·레포는 없고, 브라우저 세션만 가진다.
```bash
sudo bash install-browser.sh                       # 가상 화면·VNC·Playwright MCP·Chromium·안전장치
sudo nano /etc/ai-team/bots.d/concierge/bot.yaml   # browser.allow: 쓸 사이트 도메인
sudo ai-bot add concierge                          # Claude·Telegram 토큰
sudo ai-bot browser-login concierge https://<사이트>/login …   # PC 에서 SSH 터널 + 브라우저로 직접 로그인
sudo ai-team-update-instructions && sudo ai-bot apply leader && sudo ai-bot restart leader   # leader 팀원 목록
```
- **로그인은 사람이 직접**: `browser-login` 이 봇의 브라우저 프로필을 VNC(비밀번호·localhost 전용)로 열어 준다. PC 에서 `ssh -L 6080:127.0.0.1:6080 dev-box` → `http://localhost:6080/vnc.html`. 봇은 비밀번호를 모른다.
- **저장·제출은 승인 후**: 훅(`ai-browser-guard`)이 저장·제출·지원·삭제·결제성 클릭, Enter 제출, 파일 업로드, 확인 대화상자, 허용 목록 밖 이동을 "ask" 로 바꾸고, Claude Code 가 Telegram 으로 ✅ Allow / ❌ Deny 버튼을 보낸다. 승인은 플러그인이 처리해 봇이 스스로 할 수 없다. 페이지 안 코드 실행 도구는 거부.
- 한계: 버튼 판별은 봇이 쓴 element 설명(화면 글자)에 기댄다. 지침으로 화면 글자를 그대로 쓰게 하지만, 확실한 경계는 "봇이 가진 것 = 그 사이트 세션뿐"이라는 점이다.
- 가상 화면은 봇 사용자만 접근(Xauthority 쿠키), 데이터센터 IP 를 막는 사이트(예: 일부 CloudFront WAF)는 쓸 수 없다 — 먼저 접속 시험을 해 본다.

- **로그인 유지**: 브라우저는 봇 세션과 분리된 상시 서비스(`ai-chrome@<봇>`)로 계속 켜 두고, Playwright MCP 는 CDP(127.0.0.1:9200+display)로 붙기만 한다 → 사이트가 세션 쿠키로 로그인을 유지해도 봇 세션이 끝날 때 풀리지 않는다. CDP 포트는 iptables owner 규칙으로 그 봇 사용자만 접속 가능(다른 봇이 로그인된 브라우저를 조종 못 함).
- `browser.keepalive: [URL…]`·`keepalive_minutes` 를 주면 `ai-browser-keepalive@<봇>.timer` 가 그 주소를 별도 탭으로 잠깐 열어 유휴 만료를 늦춘다.
- `browser.relogin: [saramin]` 이면 같은 타이머가 `ai-browser-relogin saramin` 을 돌려 로그인을 확인하고, 풀려 있으면 소셜 버튼·팝업·'계속하기'를 프로그램이 처리해 다시 로그인한다(비밀번호·인증·약관 화면이면 멈춤). 사이트 정의는 `/usr/local/lib/ai-team/relogin-sites.json`. concierge 도 작업 전 풀려 있으면 같은 명령을 쓴다.
- 그래도 안 되면 concierge 가 Telegram 으로 `sudo ai-bot browser-login <봇>` 실행을 요청한다. 브라우저를 재시작(서버 재부팅 등)하면 세션 쿠키는 사라지므로 자동 재로그인이 이를 메운다.

### 새 봇 추가
```bash
sudo ai-bot add writer --like research     # 비슷한 봇 정의 복사 → 편집 → Enter → 토큰 입력 → 시작
```
그다음
- Jira `Agent` 드롭다운에 `writer` 추가 — 아래 "프로젝트 추가를 leader 에게"의 설정 변경용 토큰을 등록해 두면 `ai-bot apply` 가 자동으로 추가한다. 없으면 Jira ⚙ → 작업 항목 → 필드 → Agent → ⋯ → 컨텍스트 및 기본값 → 옵션 편집 (디스패처는 다음 tick 부터 자동으로 인식)
- leader 의 팀원 목록 갱신: `sudo ai-team-update-instructions && sudo ai-bot apply leader && sudo ai-bot restart leader`
  (leader·dev·research 지침을 직접 고쳐 썼다면 leader `CLAUDE.md` 의 "팀원" 목록에 한 줄만 손으로 추가)
- 새 봇의 지침(`bots.d/writer/CLAUDE.md`)은 복사해 온 봇의 것이니 역할 부분을 고친다
사람이 해야 하는 것: BotFather 로 봇 만들기, `claude setup-token`, (필요 시) GitHub 토큰.

### 프로젝트 맥락 분리
- **봇 지침에는 프로젝트를 쓰지 않는다.** 지침(CLAUDE.md)은 역할만 담고, 프로젝트 지식은 `/etc/ai-team-projects/`(목록 `INDEX.md` + `<키>.md`) 한 곳에만 둔다. 봇은 요청마다 프로젝트를 먼저 정하고(명시·Jira Product·일반 질문은 무관·불분명하면 질문), 그 프로젝트 파일만 참고한다.
- 프로젝트를 손으로 추가·변경한 뒤: `sudo ai-team-update-instructions --index-only` (목록만 갱신, 지침은 그대로)
- 봇을 추가한 뒤·표준 지침으로 되돌릴 때: `sudo ai-team-update-instructions && sudo ai-bot apply && sudo ai-bot restart <봇>` — leader 의 팀원 목록도 갱신된다. 전용 템플릿이 없는 봇(예: `--like` 로 만든 봇)의 지침은 그대로 둔다.
- 작업 하나 = Claude 세션 하나. 새 작업에는 **해당 Product 의 브리프만** 주입, 이어서 할 때는 브리프가 바뀐 경우에만 갱신본 전달
- dev 는 Product 에 매핑된 레포 안에서 실행 → 그 레포의 `CLAUDE.md`(스택·컨벤션)가 적용된다
- 브리프는 root 소유일 때만 읽는다(봇이 고쳐서 다른 작업을 오염시키는 것 방지)

### 프로젝트 추가를 leader 에게 (선택)
프로젝트 키(= Jira Product 값)를 하나 만들려면 Jira 드롭다운 옵션, `team-config.yaml` 의 `products`, 브리프, 목록 네 곳을 바꿔야 한다. 이걸 leader 가 `ai-project add` 한 번으로 하게 한다.
- 구조: leader → 확인 서비스 소켓(root) → ① Jira Product 옵션 추가 ② 브리프 `/etc/ai-team-projects/<키>.md` ③ `products` 에 한 줄 ④ 목록 갱신 → Telegram 으로 결과 알림. 사람 코드 없이 바로 실행된다(추가·교체만, 삭제·비율 변경 없음, 변경 전 파일은 `/var/lib/ai-team/approvals/` 에 백업).
- Jira 드롭다운 옵션 추가는 **Jira 관리자 권한**이 필요하다. 봇 전용 계정에 관리자 권한을 주고, 그 계정으로 *옵션 다루기용 범위만* 가진 토큰을 만들어 **root 전용 파일**(`/etc/ai-team/jira-admin.env`)에만 둔다. 봇은 이 토큰을 볼 수 없다.
1. admin.atlassian.com → 디렉터리 → 사용자 → 봇 계정 → 앱 액세스에서 Jira 역할을 **관리자**로
2. 봇 계정으로 로그인 → id.atlassian.com/manage-profile/security/api-tokens → 토큰 만들기. 범위 지정 토큰이면 `read:field:jira`, `read:custom-field-contextual-configuration:jira`, `read:field.option:jira`, `write:field.option:jira`, `read:jira-user`. 귀찮으면 범위 없는 일반 **API 토큰 만들기**(계정 권한 전체)도 된다 — `ai-bot secret`·`ai-approve` 가 두 종류를 자동으로 구분한다.
3. VM: `sudo ai-approve setup-jira-admin` (이메일·토큰 입력). leader 에게 같은 계정의 일반 토큰을 이미 넣었다면 `sudo ai-approve setup-jira-admin --from-bot leader` 로 그대로 재사용(이때 leader 도 Jira 설정을 바꿀 수 있는 권한을 갖게 됨)
4. leader 정의에 `approve: [mail.send, project.add, project.brief]` → `sudo ai-team-update-instructions && sudo ai-bot apply leader && sudo ai-bot restart leader`
- 확인: `sudo ai-approve status` 에 "Jira 설정 변경용 토큰: 있음". 사람이 직접 옵션만 추가: `sudo ai-approve jira-option product APP` / `sudo ai-approve jira-option agent writer`

---

## 11. 운영 명령어

```bash
aiteam                                    # = sudo ai-bot status (ubuntu ~/.bashrc 함수, 없으면 아래 명령 사용)
sudo ai-bot status                        # 봇: 서비스·플러그인·토큰·메모리·Docker·볼트
sudo ai-bot logs dev                      # 봇 서비스 로그 + 현재 화면
sudo ai-bot attach leader                 # 봇 화면에 들어가기 (나올 때 Ctrl-b → d. Ctrl-c 금지)
sudo ai-bot restart dev                   # 봇 재시작 (재시작 직후 1분은 메시지 보내지 않기)
sudo ai-bot apply leader                  # bots.d 의 정의를 고친 뒤 반영 (지침 변경은 restart 까지)
sudo ai-bot secret dev github             # 토큰 교체 (켜져 있는 봇은 자동 재시작)
sudo ai-bot check research                # Claude 토큰 동작 확인
sudo ai-bot update                        # 공용 claude/bun 업데이트 후 봇 순차 재시작
sudo ai-bot disable writer                # 중지 + 계정 잠금 (홈·정의는 보관)
sudo ai-bot shell dev                     # 봇 사용자 셸 (레포 작업·확인용)

sudo journalctl -u ai-team-dispatch --since today -o cat | grep -vE 'Starting|Deactivated|Finished|Consumed'
sudo journalctl -u ai-task-team-7 --no-pager               # 작업 하나의 실행 기록
systemctl list-units 'ai-task-*'; systemctl list-timers 'ai-*'
sudo python3 /opt/ai-team/dispatcher.py tick --dry-run     # 지금 무엇을 실행할지 미리 보기
sudo tail -5 /var/lib/ai-team/usage.jsonl                  # 사용량 장부
sudo systemctl stop ai-team-dispatch.timer                 # 긴급 정지 (실행 중 작업: sudo systemctl stop ai-task-team-N)
sudo ls /home/bot-leader/bots/leader/outbox/{processed,failed}
sudo journalctl -u ai-vault-sync@leader -n 20 --no-pager   # 볼트 동기화
```

---

## 12. 보안 모델

| 위협 | 대책 |
|---|---|
| 봇이 root 가 됨 | 봇 사용자는 sudo·docker·lxd 그룹 없음. ubuntu 도 sudo 에 비밀번호 필요. 관리 작업은 Jira 승인형 |
| 한 봇이 다른 봇 파일·토큰을 봄 | 봇마다 리눅스 사용자, 홈 700, 토큰 봇별 발급 |
| research 가 웹에서 프롬프트 인젝션을 당함 | research 는 셸·레포·GitHub·Docker 없음, 노트 폴더만 쓰기 |
| 봇이 자기 권한을 넓힘 | 지침·권한 파일·봇 폴더 root 소유, 실행 파일 root 소유·자동 업데이트 끔, 서비스는 시스템 유닛 |
| 남이 봇에 말을 걺 / 페어링 탈취 | Telegram 접근 목록에 내 ID 만, static 모드(시작 시 고정) |
| Jira 토큰 유출 | root 전용 파일, 범위 지정 토큰, 봇 프로세스에 전달하지 않음 |
| 위조된 관리 명령 실행 | 내 Jira 계정이 Scheduled 로 옮긴 것만, 실행 직전 기록 재확인, 명령 전문을 알림으로 보여 줌 |
| (선택) leader 의 Jira 직접 권한 남용 | 승인자 계정 토큰 등록 거부, `ai-jira` 에 삭제 명령 없음, 관리 요청 이슈는 읽기만 |
| 봇이 속아서 메일을 보냄 | 발송 토큰은 root 의 확인 서비스만 보유, 매 발송 사람의 일회용 코드 필요, 봇 토큰에 발송 권한이 섞이면 등록 거부 |
| main 브랜치 훼손 | dev 의 main/force push 차단 규칙 + GitHub 브랜치 보호(PR 필수) |
| 볼트 개인정보가 다른 곳으로 | 볼트는 bot-leader 만, dev GitHub 토큰에 볼트가 보이면 등록 거부, leader 지침으로 Jira 전달 금지 |
| 외부 노출 | 포트 22 만, SSH 키 전용·root 금지·ubuntu 만 |

**남는 한계**: 세 봇이 같은 Claude 구독을 쓴다(사용량은 디스패처 예산으로 분배, Claude 토큰이 새면 계정 사용 가능 → 해당 토큰만 폐기 후 재발급).
허용·차단 목록은 실수 방지용이다. `python3`, `npm` 허용은 사실상 임의 코드 실행이므로 진짜 경계는 리눅스 사용자 권한이다.

---

## 13. 트러블슈팅과 배운 것

**설치·계정**
- Jira `401 scope does not match`: 범위 지정 토큰은 `api.atlassian.com/ex/jira/<cloudId>` 로만 동작하고 `/myself` 에는 `read:jira-user` 가 필요하다.
- 붙여넣는 스크립트에 `set -e` 를 넣지 않는다(실패 시 SSH 세션이 닫힘). 중간에 멈춰야 하면 함수로 감싸 `return`.
- 입력을 받는 명령(토큰 입력)은 붙여넣는 스크립트에 섞지 않는다. 뒤 줄이 입력으로 빨려 들어간다.
- `jira.env` 같은 env 파일에는 줄 끝 주석을 달지 않는다(값으로 읽힌다).
- 클라우드 이미지는 기본 사용자를 `lxd` 그룹에 넣어 둔다(사실상 root). sudo 만 막아서는 부족하다.
- `dockerd-rootless-setuptool.sh` 는 꺼진 root Docker 의 **남은 소켓 파일**만 있어도 설치를 거부한다 → `install.sh` 가 지운다.

**봇·Telegram**
- Telegram 봇 토큰 하나 = 받는 프로그램 하나. 같은 토큰으로 두 프로세스가 받으면 충돌한다(보내기는 여러 곳에서 해도 됨).
- 봇 지침에 "Telegram 메시지는 reply 도구로 답한다"가 없으면 답을 화면에만 쓰고 보내지 않는다.
- **봇들이 특정 프로젝트 기준으로만 답한다** → 지침에 프로젝트 이름·레포가 들어 있었고, 대화 중에는 프로젝트 맥락 파일을 읽을 수 없었다. 지침은 역할만, 프로젝트 지식은 봇이 읽을 수 있는 `/etc/ai-team-projects/` 로 분리했다. 바꾼 뒤에는 봇을 재시작해 대화 맥락·자동 메모리도 비운다.
- 예시 값(`<…>`)이 남은 채로 봇을 만들면 접근 목록에 그 글자가 들어가 봇이 아무 메시지도 받지 않는다 → `ai-bot` 이 숫자 ID 인지 검사한다.
- 봇에게 Jira 직접 권한을 줄 때 **사람(승인자) 계정의 토큰을 주면 관리 명령을 스스로 승인할 수 있다** → `ai-bot secret <봇> jira` 는 승인자 계정 토큰을 거부한다.
- **봇이 작업을 끝낸 직후 그 봇의 Telegram 연결이 끊긴다** → 디스패처의 `claude -p` 도 사용자 설정의 Telegram 플러그인을 띄우고, 플러그인은 시작할 때 같은 토큰의 '이전 수신기'(= 대화형 봇)를 종료시킨다. 디스패처·`ai-bot check` 는 `--settings` 로 이 플러그인을 끄고 실행한다.
- 가끔 Claude 는 떴는데 Telegram 플러그인(`bun server.ts`)이 안 붙는다 → 감시 타이머가 2회 연속 확인 후 그 봇만 재시작(1시간 3회 한도).
- `CLAUDE_CODE_OAUTH_TOKEN` 만 있으면 대화형 claude 가 첫 실행 로그인 화면에서 멈출 수 있다 → `ai-bot` 이 `~/.claude.json` 에 온보딩 완료·폴더 신뢰를 미리 기록.
- 처음 세팅 때 `/telegram:configure <토큰>` 처럼 토큰을 대화에 넣으면 `~/.claude/history.jsonl` 에 평문으로 남는다 → 토큰은 숨김 입력으로만.

**디스패처·권한**
- `claude -p` 에는 승인할 사람이 없다. 허용 안 된 쓰기·셸 명령은 거부된다. `--allowedTools` 는 "묻지 않고 허용"이지 나머지를 막지 않는다(막으려면 `--disallowedTools`).
- 다른 폴더로 `cd` 한 뒤 git 실행은 허용 규칙과 무관하게 승인을 요구한다 → dev 는 레포 안에서 실행(`work_in_repo`).
- 허용 규칙은 실제 명령 형태와 정확히 맞아야 한다(venv 의 python 을 절대경로로 실행하면 `pip *` 에 안 걸림) → `uv pip install --python <venv>` 처럼 고정 접두어 명령을 쓰게 안내.
- 파일 쓰기 차단은 `Write(...)` 가 아니라 `Edit(...)` 규칙으로 쓴다(`Write(...) never matches` 경고).
- Claude 세션은 실행 폴더별로 저장된다. 폴더가 바뀌면 디스패처가 새 세션으로 시작한다.
- 권한·브리프를 고친 뒤 재예약해도 이어서 실행되는 봇은 모른다 → 재예약 메모와 브리프 갱신 전달로 해결. 권한 문제로 막힌 작업은 허용을 먼저 고친 뒤 재예약.
- 정상 종료 ≠ 작업 완료 → 결과의 `permission_denials` 를 확인해 ⚠️ 와 함께 보고.
- root 가 다른 사용자로 명령을 실행할 때는 작업 폴더도 바꿔야 한다(봇이 `/home/ubuntu` 를 못 읽어 git 실패).
- Python `subprocess` 로 사용자만 바꾸면 root 의 보조 그룹이 남는다 → `extra_groups=[]`.
- leader 지침에 "SendMessage 로 위임"과 "Jira 등록"이 공존하면 SendMessage 를 고른다 → 위임 경로는 하나로.

**환경**
- Node 가 nvm(`~/.nvm`)과 시스템(`/usr/bin`)에 공존하면 셸과 봇의 버전이 다르다. 봇은 시스템 경로만 본다.
- `sudo -i` root 셸에서는 `systemctl --user` 가 동작하지 않는다. 봇 서비스는 시스템 유닛이라 `sudo systemctl`/`sudo ai-bot` 으로 다룬다.

---

## 14. 자동 배포 (GitHub → VM)
코드는 공개 저장소, 개인 설정(봇 정의·team-config·브리프)은 비공개 저장소에 두고 VM 이 5분마다 가져가 배포한다.
GitHub 은 VM 에 접근하지 않는다(SSH 키·self-hosted runner 없음). 토큰은 어느 저장소에도 두지 않는다.

1. 비공개 설정 저장소를 만든다 (비어 있어도 됨).
2. 서버:
   ```bash
   sudo git clone https://github.com/<계정>/ai-team /var/lib/ai-team/src/code
   sudo bash /var/lib/ai-team/src/code/deploy/bootstrap.sh <계정>/ai-team-config
   ```
   안내대로 설정 저장소에 배포 키(쓰기 허용)를 등록하면 배포 계획이 출력된다.
3. `sudo ai-team-deploy run --force` (첫 배포: 서버 설정이 설정 저장소에 첫 커밋으로 올라감) → `sudo ai-team-deploy enable`
4. GitHub 코드 저장소 Settings → Rules → Rulesets → New branch ruleset → 대상 `Include default branch`:
   Require a pull request before merging(승인 0 — 1인 저장소에서 1이면 본인 PR 을 못 합침), Require status checks to pass: `test`, Bypass list 비움.
5. 코드 저장소 Settings → Secrets and variables → Actions → New repository secret: Name `PRIVACY_PATTERNS`,
   값은 공개 저장소에 들어가면 안 되는 문자열을 한 줄에 하나(이름·chat_id·개인 이메일 등). CI 가 저장소 전체에서 찾아 있으면 실패하고,
   로그에는 파일:줄과 패턴 번호만 찍는다. 이 Secret 이 없으면 CI 가 실패한다(포크 PR 은 건너뜀).
6. 확인: 작은 PR 을 Merge → 5분 안에 텔레그램 "🚀 배포 완료" 알림.

PR 이 root 실행·권한 경계 파일(`.github/scripts/pr_guard.py` 의 `SENSITIVE`)을 건드리면 CI 의 `guard` 가 PR 에
"⚠️ 보안 경로 변경" 코멘트를 단다. 봇이 만든 PR 이면 이 코멘트가 붙은 것만큼은 diff 를 직접 보고 Merge 한다.

이후: PR Merge → (CI 통과 확인) → 설치 → 설정 생성 → 봇 재시작 → 점검 → 텔레그램 알림. 점검 실패면 직전 버전으로 자동 되돌림.
`sudo ai-team-deploy status | plan | rollback | disable`.

## 15. 정기 점검

| 주기 | 할 일 |
|---|---|
| 매일 | ☀️ 아침 요약 확인, 검토 중 작업 승인/재예약 |
| 매주 | `sudo ai-bot status`, Failed 작업 원인 확인, `budget.per_cycle`·동시 실행 수 조정 |
| 매월 | `sudo ai-bot update`, `sudo apt list --upgradable`, OCI 예산 알림 확인 |
| 만료 전 | Jira 토큰(1년), Claude 토큰 3개(1년), GitHub 토큰(설정한 만료일) → `sudo ai-bot secret <봇> <종류>` 로 교체 |
| 변경 후 | 9장 테스트 시나리오 T1~T4 |

**백업 대상**: `/etc/ai-team/`(설정·봇 정의·브리프, 토큰 제외 시 `jira.env` 빼고), `/var/lib/ai-team/usage.jsonl`. 코드는 GitHub, 작업 기록은 Jira 에 있다.

---

## 16. 부록

### 16.1 저장소 구성
```
ai-team/
├── README.md · LICENSE
├── install.sh · harden.sh · install-browser.sh   설치 (sudo bash …)
├── src/                        서버에 설치되는 프로그램 — 설치 위치는 deploy/manifest.tsv
│   ├── dispatcher.py · jira-setup.py              (/opt/ai-team)
│   ├── ai-bot · ai-approve · update-instructions.py   (/usr/local/sbin)
│   ├── ai-jira · ai-google · ai-project · vault-sync  (/usr/local/bin)
│   └── ai-browser-guard · ai-browser-keepalive · ai-browser-relogin(.js) · relogin-sites.json  (브라우저 봇)
├── deploy/                     ai-team-deploy(자동 배포) · manifest.tsv · bootstrap.sh
├── systemd/                    디스패처·아침 요약·자동 배포 유닛
├── bots/{leader,dev,research}/ bots-optional/concierge/   봇 정의 템플릿
├── config/ · context/          설정·브리프 예시
├── tests/                      pytest (CI 에서 실행)
└── docs/SETUP.md               이 문서
```
`ai-bot init` 이 만드는 유닛: `ai-approve.socket`·`ai-approve@.service`, `ai-bot@.service`, `ai-vault-sync@.{service,timer}`, `ai-bot-watchdog.{service,timer}`.
`ai-bot apply` 가 만드는 파일: 봇 사용자·폴더, `/etc/ai-team-jira.yaml`(봇에 `jira: true` 가 있을 때, 비밀 아님).

### 16.2 설계 결정
| 주제 | 검토한 것 | 결정 | 이유 |
|---|---|---|---|
| 메신저 | Slack 페르소나 앱 | Telegram + Claude Code 채널 플러그인 | 기존 구독 안에서 바로 구축, 모바일 편함 |
| 인프라 | 집 서버(미니PC 등), 저가 VPS | OCI Always Free | 0원, 상시 가동, 소음·전기료 없음 |
| 런타임 | Agent SDK + API 키 | Claude Code + 기존 구독 | 비용 고정, 대신 사용량 공유 → 야간 창·예산 |
| 작업 추적 | SQLite 큐 | Jira Cloud Free | 보드·모바일에서 직접 추적·지시 |
| 실행 | 봇 세션 안에서 대기 | 디스패처 + `claude -p` | 재부팅에도 상태가 Jira 에 남음, 시간 지정 |
| 접수 | 디스패처가 Telegram 직접 수신 | leader → outbox 파일 → 디스패처 | 토큰 하나 = 수신자 하나, leader 에 Jira 토큰을 주지 않기 위해 |
| 권한 | 비밀번호 없는 sudo | 승인형 관리 명령 | 봇이 root 가 되는 경로 차단 |
| 격리 | 봇 3개가 ubuntu 공유 → leader/worker 2개 | 봇마다 사용자 + `ai-bot` | research(웹) 오염이 코드·토큰으로 번지지 않게, 봇 추가 자동화 |
| 로그인 | 사용자별 브라우저 로그인 | `claude setup-token` 봇별 토큰 | 자동화 가능, 봇 하나만 폐기 가능 |
| 볼트 동기화 | 병합 | 원격 우선 (`pull --rebase -X ours`) | 사람이 다른 기기에서 직접 고치는 경우가 더 많다고 가정. 정리 못 한 충돌은 원격으로 맞추고 봇 커밋은 `refs/leader-dropped/` 에 보존 |
