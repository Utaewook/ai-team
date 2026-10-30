#!/usr/bin/env python3
"""AI Team 디스패처 — Jira 를 단일 진실 원천으로 삼아 봇 작업을 시간대·예산에 맞춰 실행합니다.

  dispatcher.py tick [--dry-run]   매분 systemd timer 가 실행. 작업 선택·시작·중단
  dispatcher.py run TEAM-12        tick 이 systemd-run 으로 띄우는 러너. 작업 1개 실행 후 결과 기록
  dispatcher.py summary            아침 요약을 Telegram 으로 전송 (07:30 timer)
  dispatcher.py notify-test        Telegram 알림 연결 확인
  dispatcher.py admin-run TEAM-9   승인된 관리 요청을 root 로 실행 (tick 이 systemd-run 으로 띄움)

설계 원칙
  - 언제 실행할지는 코드가, 무엇을 할지는 Claude 가 결정한다.
  - Jira 토큰은 루트로 도는 디스패처·러너만 가진다. claude 프로세스는 봇 사용자 권한으로 실행되고
    토큰이 담긴 환경변수를 받지 않는다.
  - 의존성: python3 표준 라이브러리 + PyYAML (sudo apt install python3-yaml)
"""
import argparse
import base64
import datetime as dt
import fcntl
import json
import os
import pwd
import re
import signal
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML 이 필요합니다: sudo apt install python3-yaml")

CONFIG_PATH = os.environ.get("AI_TEAM_CONFIG", "/etc/ai-team/team-config.yaml")
JIRA_TIME_FMT = "%Y-%m-%dT%H:%M:%S.%f%z"
PRIORITY_RANK = {"Highest": 1, "High": 2, "Medium": 3, "Low": 4, "Lowest": 5}


# ───────────────────────────── 설정 ─────────────────────────────

def load_config(path=CONFIG_PATH):
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg["_tz"] = ZoneInfo(cfg.get("timezone", "Asia/Seoul"))
    bots_dir = cfg.get("bots_dir") or os.path.join(os.path.dirname(path), "bots.d")
    agents = dict(cfg.get("agents") or {})
    agents.update(load_bot_agents(bots_dir))   # ai-bot 으로 관리하는 봇 정의가 우선
    cfg["agents"] = agents
    return cfg


def load_bot_agents(bots_dir, home_base="/home", claude_bin="/usr/local/bin/claude"):
    """/etc/ai-team/bots.d/<이름>/bot.yaml → 디스패처 봇 설정 (ai-bot 의 dispatcher_agents 와 같은 규칙).
    봇 하나 = 리눅스 사용자 bot-<이름>, 작업 폴더 /home/bot-<이름>/bots/<이름>."""
    agents = {}
    if not os.path.isdir(bots_dir):
        return agents
    for name in sorted(os.listdir(bots_dir)):
        path = os.path.join(bots_dir, name, "bot.yaml")
        if name.startswith((".", "_")) or not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as fh:
            bot = yaml.safe_load(fh) or {}
        if bot.get("enabled", True) is False or bot.get("dispatch") is False:
            continue
        d = bot.get("dispatch") or {}
        home = os.path.join(home_base, f"bot-{name}")

        def in_home(p):
            full = os.path.normpath(os.path.join(home, p))
            if not (full == home or full.startswith(home + os.sep)):
                raise ValueError(f"{name}: 홈 폴더 밖 경로: {p}")
            return full
        agents[name] = {"user": f"bot-{name}", "cwd": os.path.join(home, "bots", name),
                        "claude_bin": claude_bin, "model": d.get("model", bot.get("model")),
                        "work_in_repo": bool(d.get("work_in_repo")),
                        "extra_args": list(d.get("extra_args") or []),
                        "repos": {k: in_home(v) for k, v in (bot.get("repos") or {}).items()},
                        "env_file": os.path.join(home, ".config", "ai-bot", "claude.env"),
                        "display": (bot.get("browser") or {}).get("display", 50) if bot.get("browser") else None}
    return agents


def load_env(path):
    env = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
    return env


def log(msg):
    print(f"{dt.datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)


# ───────────────────────────── Jira ─────────────────────────────

class JiraError(Exception):
    pass


class Jira:
    def __init__(self, cfg):
        env = load_env(cfg["jira"]["env_file"])
        if env.get("JIRA_TOKEN_SCOPED", "").lower() in ("1", "true", "yes"):
            self.base = f"https://api.atlassian.com/ex/jira/{env['JIRA_CLOUD_ID']}"
        else:
            self.base = env["JIRA_URL"].rstrip("/")
        token = base64.b64encode(f"{env['JIRA_EMAIL']}:{env['JIRA_TOKEN']}".encode()).decode()
        self._auth = f"Basic {token}"
        self.f = cfg["jira"]["fields"]
        self.text_fields = set(cfg["jira"].get("text_fields") or [])
        self.s = cfg["jira"]["statuses"]
        self.project = cfg["jira"]["project_key"]

    def choice(self, alias, value):
        """선택 필드 값. 텍스트 필드로 바꾼 필드(jira.text_fields)는 문자열 그대로."""
        return value if alias in self.text_fields else {"value": value}

    def _req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": self._auth,
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as err:
            raise JiraError(f"{method} {path} → HTTP {err.code}: {err.read().decode(errors='replace')[:300]}")

    def search(self, jql, max_results=50):
        fields = ["summary", "status", "priority", "description"] + list(self.f.values())
        out = self._req("POST", "/rest/api/3/search/jql",
                        {"jql": jql, "fields": fields, "maxResults": max_results})
        return out.get("issues", [])

    def get(self, key):
        fields = ",".join(["summary", "status", "priority", "description"] + list(self.f.values()))
        return self._req("GET", f"/rest/api/3/issue/{key}?fields={fields}")

    def set_fields(self, key, values):
        """values: {'usage': 1.2, 'session_id': '...', 'run_at': datetime} — 설정 파일의 필드 별칭 사용."""
        payload = {}
        for alias, value in values.items():
            if isinstance(value, dt.datetime):
                value = to_jira_time(value)
            payload[self.f[alias]] = value
        self._req("PUT", f"/rest/api/3/issue/{key}", {"fields": payload})

    def set_raw(self, key, fields):
        self._req("PUT", f"/rest/api/3/issue/{key}", {"fields": fields})

    def create(self, fields):
        return self._req("POST", "/rest/api/3/issue", {"fields": fields})["key"]

    def changelog(self, key):
        """상태 변경 이력 (오래된 것 → 최신 순)."""
        histories, start = [], 0
        while True:
            page = self._req("GET", f"/rest/api/3/issue/{key}/changelog?startAt={start}&maxResults=100")
            values = page.get("values", [])
            histories += values
            start += len(values)
            if page.get("isLast", True) or not values:
                break
        return sorted(histories, key=lambda h: h.get("created", ""))

    def raw_issue(self, key, fields):
        return self._req("GET", f"/rest/api/3/issue/{key}?fields={','.join(fields)}")

    def transition(self, key, status_alias):
        target = self.s[status_alias]
        options = self._req("GET", f"/rest/api/3/issue/{key}/transitions").get("transitions", [])
        for tr in options:
            if tr.get("to", {}).get("id") == target:
                self._req("POST", f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": tr["id"]}})
                return
        raise JiraError(f"{key}: 상태 {status_alias}({target}) 로 가는 전환이 없습니다")

    def comment(self, key, text, panel=None):
        """text 는 마크다운으로 해석해 서식 있는 코멘트로. panel=(종류, 한 줄 제목) 이면 맨 위에 상태 패널."""
        doc = md_to_adf(text) if text.strip() else {"type": "doc", "version": 1, "content": []}
        if not panel and not doc["content"]:
            doc["content"].append({"type": "paragraph", "content": []})
        if panel:
            kind, title = panel
            doc["content"].insert(0, {"type": "panel", "attrs": {"panelType": kind},
                                      "content": [{"type": "paragraph", "content": inline_nodes(title)}]})
        self._req("POST", f"/rest/api/3/issue/{key}/comment", {"body": doc})


def text_to_adf(text, limit=12000):
    if len(text) > limit:
        text = text[:limit] + "\n…(잘림)"
    paragraphs = []
    for line in text.split("\n"):
        node = {"type": "paragraph", "content": []}
        if line:
            node["content"].append({"type": "text", "text": line})
        paragraphs.append(node)
    return {"type": "doc", "version": 1, "content": paragraphs}


INLINE_RE = re.compile(r"(`[^`\n]+`|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\((?:https?://)[^)\s]+\)|https?://[^\s)<>]+)")
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def inline_nodes(text):
    """굵게(**), 인라인 코드(`), 링크([t](url), 맨 URL) 를 ADF text 노드로."""
    nodes = []
    for part in INLINE_RE.split(text):
        if not part:
            continue
        if part.startswith("`") and part.endswith("`") and len(part) > 2:
            nodes.append({"type": "text", "text": part[1:-1], "marks": [{"type": "code"}]})
        elif part.startswith("**") and part.endswith("**") and len(part) > 4:
            nodes.append({"type": "text", "text": part[2:-2], "marks": [{"type": "strong"}]})
        elif LINK_RE.fullmatch(part):
            label, url = LINK_RE.fullmatch(part).groups()
            nodes.append({"type": "text", "text": label, "marks": [{"type": "link", "attrs": {"href": url}}]})
        elif part.startswith(("http://", "https://")):
            nodes.append({"type": "text", "text": part, "marks": [{"type": "link", "attrs": {"href": part}}]})
        else:
            nodes.append({"type": "text", "text": part})
    return nodes


def _paragraph(text):
    return {"type": "paragraph", "content": inline_nodes(text)}


def _table(rows):
    def cells(line):
        return [c.strip() for c in line.strip().strip("|").split("|")]
    header, body = cells(rows[0]), [cells(r) for r in rows[2:]]
    width = len(header)

    def row(values, cell_type):
        values = (values + [""] * width)[:width]
        return {"type": "tableRow", "content": [
            {"type": cell_type, "content": [_paragraph(v) if v else {"type": "paragraph", "content": []}]}
            for v in values]}
    return {"type": "table", "content": [row(header, "tableHeader")] + [row(r, "tableCell") for r in body]}


def md_to_adf(text, limit=15000):
    """보고서에 흔히 쓰이는 마크다운을 Jira 서식(ADF)으로 변환. 모르는 문법은 평문 문단으로 남김."""
    if len(text) > limit:
        text = text[:limit] + "\n\n…(잘림, 전체 내용은 실행 로그 참고)"
    lines = text.split("\n")
    blocks, para, i = [], [], 0

    def flush():
        if para:
            content = []
            for n, line in enumerate(para):
                if n:
                    content.append({"type": "hardBreak"})
                content.extend(inline_nodes(line))
            blocks.append({"type": "paragraph", "content": content})
            para.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        fence = re.match(r"^\s*```(\S*)", line)
        if fence:
            flush()
            lang, code = fence.group(1), []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            node = {"type": "codeBlock", "content": [{"type": "text", "text": "\n".join(code)}] if any(code) else []}
            if lang:
                node["attrs"] = {"language": lang}
            blocks.append(node)
            i += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            flush()
            level = min(len(heading.group(1)) + 1, 6)
            blocks.append({"type": "heading", "attrs": {"level": level}, "content": inline_nodes(heading.group(2))})
        elif re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            flush()
            blocks.append({"type": "rule"})
        elif stripped.startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-{3,}", lines[i + 1]):
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i])
                i += 1
            blocks.append(_table(rows))
            continue
        elif re.match(r"^[-*+]\s+", stripped) or re.match(r"^\d+[.)]\s+", stripped):
            flush()
            ordered = bool(re.match(r"^\d+[.)]\s+", stripped))
            pattern = r"^\d+[.)]\s+" if ordered else r"^[-*+]\s+"
            items = []
            while i < len(lines) and re.match(pattern, lines[i].strip()):
                item = re.sub(pattern, "", lines[i].strip())
                items.append({"type": "listItem", "content": [_paragraph(item) if item else
                                                             {"type": "paragraph", "content": []}]})
                i += 1
            blocks.append({"type": "orderedList" if ordered else "bulletList", "content": items})
            continue
        elif stripped.startswith(">"):
            flush()
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip().lstrip(">").strip())
                i += 1
            blocks.append({"type": "blockquote", "content": [_paragraph(q) for q in quote if q] or
                           [{"type": "paragraph", "content": []}]})
            continue
        elif not stripped:
            flush()
        else:
            para.append(line.rstrip())
        i += 1
    flush()
    return {"type": "doc", "version": 1, "content": blocks or [{"type": "paragraph", "content": []}]}


def adf_to_text(node):
    """Jira 설명(ADF)을 평문으로."""
    if not node:
        return ""
    if isinstance(node, str):
        return node
    if node.get("type") == "text":
        return node.get("text", "")
    if node.get("type") == "hardBreak":
        return "\n"
    inner = "".join(adf_to_text(child) for child in node.get("content", []))
    if node.get("type") in ("paragraph", "heading", "listItem", "codeBlock", "blockquote"):
        return inner.rstrip("\n") + "\n"
    return inner


def option_value(value):
    return value.get("value") if isinstance(value, dict) else value


def product_key(value):
    """Product 값 (드롭다운이든 텍스트 필드든). 텍스트는 공백 제거·대문자로 맞춘다."""
    v = option_value(value)
    return v.strip().upper() if isinstance(v, str) and v.strip() else None


def to_jira_time(when):
    return when.strftime("%Y-%m-%dT%H:%M:%S.000%z")


def parse_jira_time(text):
    return dt.datetime.strptime(text, JIRA_TIME_FMT) if text else None


# ─────────────────────────── 시간 창 ───────────────────────────

def _hhmm(text):
    hour, minute = text.split(":")
    return dt.time(int(hour), int(minute))


def in_night(cfg, now):
    start, end = _hhmm(cfg["windows"]["night"]["start"]), _hhmm(cfg["windows"]["night"]["end"])
    t = now.timetz().replace(tzinfo=None)
    return start <= t < end if start < end else (t >= start or t < end)


def next_night_start(cfg, now):
    start = _hhmm(cfg["windows"]["night"]["start"])
    candidate = now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    return candidate if candidate > now else candidate + dt.timedelta(days=1)


def cycle_start(cfg, now):
    """예산 사이클 시작 = 가장 최근의 야간 창 시작 시각."""
    return next_night_start(cfg, now) - dt.timedelta(days=1)


def window_allows(window, cfg, now):
    window = window or "Night"
    if window == "ASAP":
        return True
    if window == "Weekend":
        return now.weekday() >= 5
    return in_night(cfg, now)


def next_allowed_start(window, cfg, now):
    window = window or "Night"
    backoff = dt.timedelta(minutes=cfg["budget"].get("rate_limit_backoff_minutes", 60))
    if window == "ASAP":
        return now + backoff
    if window == "Weekend":
        if now.weekday() >= 5:
            return now + backoff
        days = 5 - now.weekday()
        return (now + dt.timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    return next_night_start(cfg, now)


# ─────────────────────────── 사용량 장부 ───────────────────────────

def ledger_path(cfg):
    return os.path.join(cfg["state_dir"], "usage.jsonl")


def ledger_add(cfg, key, product, agent, cost):
    os.makedirs(cfg["state_dir"], exist_ok=True)
    row = {"ts": dt.datetime.now(cfg["_tz"]).isoformat(), "key": key,
           "product": product, "agent": agent, "cost": round(cost, 4)}
    with open(ledger_path(cfg), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def usage_since(cfg, since):
    used = {}
    try:
        with open(ledger_path(cfg), encoding="utf-8") as fh:
            for line in fh:
                row = json.loads(line)
                if dt.datetime.fromisoformat(row["ts"]) >= since:
                    used[row["product"]] = used.get(row["product"], 0.0) + row["cost"]
    except FileNotFoundError:
        pass
    return used


# ─────────────────────────── 작업 선택 ───────────────────────────

def describe(issue, f):
    fields = issue["fields"]
    return {
        "key": issue["key"],
        "summary": fields.get("summary", ""),
        "agent": option_value(fields.get(f["agent"])),
        "product": product_key(fields.get(f["product"])),
        "window": option_value(fields.get(f["window"])),
        "run_at": parse_jira_time(fields.get(f["run_at"])),
        "priority": PRIORITY_RANK.get((fields.get("priority") or {}).get("name"), 3),
    }


def rank_candidates(cfg, tasks, now, used):
    """실행 가능한 작업을 '할당 대비 덜 쓴 프로젝트 → 우선순위 → 실행 시각' 순으로."""
    budget = cfg["budget"]["per_cycle"]
    shares = cfg["products"]
    ready, waiting = [], []
    for t in tasks:
        product = t["product"] or cfg["default_product"]
        if not t["agent"] or t["agent"] not in cfg["agents"]:
            waiting.append((t, "Agent 미지정 또는 설정에 없는 봇"))
        elif t["run_at"] and t["run_at"] > now:
            waiting.append((t, f"Run At {t['run_at']:%m-%d %H:%M} 대기"))
        elif not window_allows(t["window"], cfg, now):
            waiting.append((t, f"{t['window'] or 'Night'} 창 밖"))
        else:
            share = shares.get(product, min(shares.values()))
            load = used.get(product, 0.0) / max(budget * share, 1e-9)
            far_future = dt.datetime.max.replace(tzinfo=now.tzinfo)
            ready.append((load, t["priority"], t["run_at"] or far_future, t))
    ready.sort(key=lambda item: item[:3])
    return [item[3] for item in ready], waiting


# ─────────────────────────── systemd ───────────────────────────

def unit_name(key):
    return f"ai-task-{key.lower()}"


def active_units():
    out = subprocess.run(
        ["systemctl", "list-units", "ai-task-*", "--state=active,activating",
         "--no-legend", "--plain"],
        capture_output=True, text=True).stdout
    return {line.split()[0].removesuffix(".service") for line in out.splitlines() if line.strip()}


def launch(key):
    cmd = ["systemd-run", f"--unit={unit_name(key)}", "--collect",
           "-p", "KillMode=mixed", "-p", "TimeoutStopSec=90",
           f"--setenv=AI_TEAM_CONFIG={CONFIG_PATH}",
           sys.executable, os.path.abspath(__file__), "run", key]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())


def stop(key):
    subprocess.run(["systemctl", "stop", unit_name(key)], capture_output=True)


# ───────────────────────────── tick ─────────────────────────────

def tick(cfg, dry_run):
    os.makedirs(cfg["state_dir"], exist_ok=True)
    lock = open(os.path.join(cfg["state_dir"], "tick.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return

    jira = Jira(cfg)
    s, f = jira.s, jira.f
    now = dt.datetime.now(cfg["_tz"])
    night = in_night(cfg, now)
    tag = "[dry-run] " if dry_run else ""

    # 0) leader 가 남긴 요청(작업 등록·승인·재예약·관리 요청) 처리 — 등록된 ASAP 작업은 이번 tick 에 바로 시작될 수 있음
    process_outbox(jira, cfg, dry_run)
    # 0-1) 사용자 계정이 승인한 관리 요청 실행
    process_admin_approvals(jira, cfg, dry_run)

    units = active_units()

    # 1) 진행 중 작업 점검: 창이 닫혔으면 중단, 프로세스가 없으면 복구
    running = [describe(i, f) for i in jira.search(f"project = {jira.project} AND status = {s['in_progress']}")]
    alive = 0
    for t in running:
        if not t["agent"]:  # Agent 없는 이슈는 사람 작업 → 건드리지 않음
            continue
        has_unit = unit_name(t["key"]) in units
        if has_unit and not window_allows(t["window"], cfg, now):
            log(f"{tag}{t['key']} {t['window'] or 'Night'} 창 종료 → 중단 요청")
            if not dry_run:
                stop(t["key"])  # 러너가 SIGTERM 을 받아 Paused 로 기록
        elif has_unit:
            alive += 1
        else:
            log(f"{tag}{t['key']} 진행 중인데 프로세스 없음(재부팅 등) → Paused 로 복구")
            if not dry_run:
                jira.set_fields(t["key"], {"run_at": next_allowed_start(t["window"], cfg, now)})
                jira.transition(t["key"], "paused")
                jira.comment(t["key"], "[dispatcher] 실행 프로세스가 사라져 Paused 로 되돌렸습니다. 다음 실행 때 이어서 진행합니다.")

    # 2) 남은 슬롯과 예산
    slots = cfg["max_concurrent"]["night" if night else "day"] - alive
    used = usage_since(cfg, cycle_start(cfg, now))
    total_used, budget = sum(used.values()), cfg["budget"]["per_cycle"]
    if slots <= 0:
        return
    if total_used >= budget:
        log(f"이번 사이클 예산 소진 ({total_used:.2f}/{budget}) → 새 작업 시작 안 함")
        return

    # 3) 후보 조회 → 공정 분배 순으로 시작
    jql = (f"project = {jira.project} AND status in ({s['scheduled']}, {s['paused']}) "
           f"AND (labels is EMPTY OR labels != {admin_label(cfg)}) ORDER BY priority DESC")
    candidates = [describe(i, f) for i in jira.search(jql)]
    ready, waiting = rank_candidates(cfg, candidates, now, used)
    if dry_run:
        for t, why in waiting:
            log(f"{tag}대기 {t['key']} [{t['agent']}] {why}")

    for t in ready[:slots]:
        log(f"{tag}시작 {t['key']} [{t['agent']}/{t['product'] or cfg['default_product']}] {t['summary']}")
        if dry_run:
            continue
        jira.transition(t["key"], "in_progress")  # 선점
        try:
            launch(t["key"])
        except Exception as err:  # noqa: BLE001
            jira.transition(t["key"], "scheduled")
            jira.comment(t["key"], f"[dispatcher] 실행 실패로 Scheduled 로 되돌림: {err}")
            log(f"{t['key']} 실행 실패: {err}")


# ───────────────────────────── run ─────────────────────────────

REPORT_GUIDE = (
    "작업이 끝나면 마지막 메시지를 아래 마크다운 형식으로 작성하세요. 디스패처가 이 내용을 서식 있는 Jira 코멘트로 옮깁니다.\n"
    "## 요약\n(이 섹션은 휴대폰 Telegram 알림으로 그대로 갑니다. 결론·핵심 숫자·날짜를 먼저, `- ` 목록 3~5줄, "
    "한 줄 50자 안팎. 표·코드 블록·링크는 쓰지 말고 아래 섹션에 둡니다)\n## 상세\n(표·근거 등 전체 결과)\n## 변경 사항\n(파일, 브랜치, 커밋, PR 링크. 없으면 '없음')\n## 남은 일\n## 다음 담당자에게\n\n"
    "서식 규칙: 목록은 `- `, 순서가 있으면 `1. `, 명령·경로·코드 이름은 `백틱`, 여러 줄 코드는 ``` 블록, "
    "비교는 마크다운 표, 출처는 [제목](URL) 링크. 결과 본문은 이 메시지에 모두 담고 별도 노트 파일은 만들지 마세요.\n\n"
    "관리자 권한(sudo)이 필요한 작업이 있으면 직접 시도하지 마세요. 대신 보고서 맨 끝에 아래 섹션을 추가하면 "
    "사용자의 승인을 받아 root 로 실행되고, 이 작업이 자동으로 다시 예약됩니다.\n"
    "## 관리자 명령 요청\n```bash\n(실행할 명령. sudo 없이 root 로 실행됨. 여러 줄 가능)\n```\n이유: (한 줄)"
)
MAX_BRIEF_CHARS = 8000


def load_brief(cfg, product):
    """root 소유 폴더의 프로젝트 브리프. 봇이 고칠 수 없는 위치에서만 읽습니다."""
    folder = cfg.get("context_dir")
    if not folder or not product:
        return ""
    path = os.path.join(folder, f"{product}.md")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        st = os.fstat(fd)
        with os.fdopen(fd, "rb") as fh:
            if st.st_uid != 0 or st.st_mode & 0o022:
                log(f"브리프 무시 (root 소유가 아니거나 다른 사용자가 쓸 수 있음): {path}")
                return ""
            text = fh.read(MAX_BRIEF_CHARS * 4).decode("utf-8", errors="replace")
    except FileNotFoundError:
        log(f"브리프 없음: {path} (프로젝트 맥락 없이 실행)")
        return ""
    except OSError as err:
        log(f"브리프 읽기 실패: {path} ({err})")
        return ""
    return text[:MAX_BRIEF_CHARS].strip()


def build_prompt(task_key, summary, description, resuming, product=None, brief="", notes=""):
    rules = "Jira 에는 직접 접근하지 마세요. 진행 기록과 상태 변경은 디스패처가 처리합니다."
    memo = (f"# 사용자의 추가 지시 (가장 최근 것이 우선)\n{notes}\n\n---\n\n" if notes else "")
    if resuming:
        updated = (f"# 프로젝트 맥락 갱신 ({product})\n이 작업을 시작한 뒤 프로젝트 설명이 바뀌었습니다. "
                   f"아래 최신 내용을 따르세요.\n\n{brief}\n\n---\n\n" if brief else "")
        return (f"{updated}{memo}이전 세션에서 이어지는 Jira 작업 {task_key}「{summary}」를 계속 진행하세요. "
                f"위에 추가 지시나 갱신 내용이 있으면 그에 맞춰 이전에 막혔던 단계를 다시 시도하세요.\n"
                f"{rules}\n\n{REPORT_GUIDE}")
    context = ""
    if brief:
        context = (f"# 프로젝트 맥락 ({product})\n아래는 이 작업이 속한 프로젝트에 대한 설명입니다. "
                   f"다른 프로젝트의 정보는 섞지 마세요.\n\n{brief}\n\n---\n\n")
    return (f"{context}{memo}# 작업 {task_key}\n\n제목: {summary}\n\n설명:\n{description or '(없음)'}\n\n"
            f"{rules}\n\n{REPORT_GUIDE}")


# ─────────────── 재예약 메모 · 브리프 버전 (state_dir, root 전용) ───────────────

def _state_path(cfg, *parts):
    path = os.path.join(cfg["state_dir"], *parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def add_note(cfg, key, note):
    stamp = dt.datetime.now(cfg["_tz"]).strftime("%m-%d %H:%M")
    with open(_state_path(cfg, "notes", f"{key}.txt"), "a", encoding="utf-8") as fh:
        fh.write(f"- [{stamp}] {note.strip()}\n")


def pop_notes(cfg, key):
    path = _state_path(cfg, "notes", f"{key}.txt")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read().strip()
        os.remove(path)
        return text[-3000:]
    except FileNotFoundError:
        return ""


def brief_changed_since_start(cfg, key, brief, starting_new):
    """새 세션이면 브리프 버전을 기록하고, 이어서 실행이면 그 뒤로 바뀌었는지 알려줍니다."""
    import hashlib
    digest = hashlib.sha256(brief.encode()).hexdigest() if brief else ""
    path = _state_path(cfg, "brief-version.json")
    try:
        versions = json.load(open(path, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        versions = {}
    # 기록이 없는 경우(이 기능 이전에 시작된 작업)도 '바뀜'으로 보고 전달
    changed = (not starting_new) and bool(digest) and versions.get(key) != digest
    versions[key] = digest
    json.dump(versions, open(path, "w", encoding="utf-8"))
    return changed


def norm_key(cfg, value):
    """'team-08' → 'TEAM-8'."""
    value = str(value or "").strip().upper()
    match = re.fullmatch(rf"({re.escape(cfg['jira']['project_key'])})-0*(\d+)", value)
    return f"{match.group(1)}-{int(match.group(2))}" if match else value


def session_exists(home, work_dir, session_id):
    """Claude Code 는 세션을 ~/.claude/projects/<작업 디렉터리를 '-'로 이은 이름>/<세션>.jsonl 에 저장합니다.
    작업 디렉터리가 바뀌면 --resume 이 실패하므로, 그 디렉터리에 세션 파일이 있을 때만 이어서 실행합니다."""
    folder = re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(work_dir))
    return os.path.isfile(os.path.join(home, ".claude", "projects", folder, f"{session_id}.jsonl"))


def resolve_workspace(cfg, acfg, product):
    """작업 디렉터리 결정.
    work_in_repo 인 봇(dev)은 Product 에 매핑된 레포 안에서 실행하고,
    봇 디렉터리의 역할 지침(CLAUDE.md)과 권한 규칙(.claude/settings.json)을 플래그로 그대로 불러옵니다.
    (Claude Code 는 다른 디렉터리로 cd 한 뒤의 git 실행을 허용 규칙과 무관하게 막기 때문)"""
    repo = (acfg.get("repos") or {}).get(product) or (cfg.get("repos") or {}).get(product)
    if not (acfg.get("work_in_repo") and repo):
        return acfg["cwd"], ["--settings", headless_settings(None)]
    if not os.path.isdir(repo):
        raise FileNotFoundError(f"Product {product} 의 레포 경로가 없습니다: {repo}")
    args = []
    role_md = os.path.join(acfg["cwd"], "CLAUDE.md")
    settings = os.path.join(acfg["cwd"], ".claude", "settings.json")
    if os.path.isfile(role_md):
        args += ["--append-system-prompt-file", role_md]
    args += ["--settings", headless_settings(settings if os.path.isfile(settings) else None)]
    return repo, args


# 디스패처가 띄우는 claude -p 에서는 Telegram 플러그인을 끈다.
# 플러그인은 시작할 때 같은 봇 토큰의 '이전 수신기'를 종료시키므로, 켜 두면 대화형 봇의 Telegram 연결이 끊긴다.
HEADLESS_DISABLED_PLUGINS = ["telegram@claude-plugins-official"]


def headless_settings(path):
    """봇 권한 파일(있으면) + Telegram 플러그인 끄기 → --settings 에 넘길 JSON 문자열."""
    settings = {}
    if path:
        with open(path, encoding="utf-8") as fh:
            settings = json.load(fh)
    plugins = dict(settings.get("enabledPlugins") or {})
    for name in HEADLESS_DISABLED_PLUGINS:
        plugins[name] = False
    settings["enabledPlugins"] = plugins
    return json.dumps(settings, ensure_ascii=False)


def parse_stream(lines, on_session=None):
    """claude -p --output-format stream-json 출력에서 세션 ID 와 최종 결과를 뽑습니다."""
    session_id, result = None, None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "system" and event.get("subtype") == "init" and event.get("session_id"):
            session_id = event["session_id"]
            if on_session:
                on_session(session_id)
        elif event.get("type") == "result":
            result = event
            session_id = event.get("session_id") or session_id
    return session_id, result


def looks_rate_limited(result):
    text = (result.get("result") or "").lower()
    return result.get("is_error") and any(w in text for w in ("usage limit", "rate limit", "limit reached"))


def run(cfg, key):
    jira = Jira(cfg)
    f = jira.f
    issue = jira.get(key)
    fields = issue["fields"]
    agent = option_value(fields.get(f["agent"]))
    product = product_key(fields.get(f["product"])) or cfg["default_product"]
    window = option_value(fields.get(f["window"]))
    max_turns = int(fields.get(f["max_turns"]) or 30)
    prev_usage = float(fields.get(f["usage"]) or 0)
    old_session = fields.get(f["session_id"])
    acfg = cfg["agents"][agent]
    try:
        work_dir, role_args = resolve_workspace(cfg, acfg, product)
    except FileNotFoundError as err:
        jira.transition(key, "failed")
        jira.comment(key, f"[dispatcher] {err}\n레포를 만들거나 team-config.yaml 의 repos 를 고친 뒤 Scheduled 로 바꿔 주세요.")
        log(f"{key} 실패: {err}")
        return
    if old_session and not session_exists(pwd.getpwnam(acfg["user"]).pw_dir, work_dir, old_session):
        log(f"{key} 이전 세션이 이 작업 디렉터리({work_dir})에 없어 새로 시작합니다")
        old_session = None

    brief = load_brief(cfg, product)
    send_brief = brief if (not old_session or brief_changed_since_start(cfg, key, brief, False)) else ""
    if not old_session:
        brief_changed_since_start(cfg, key, brief, True)
    notes = pop_notes(cfg, key)
    claude_bin = acfg.get("claude_bin") or cfg["claude_bin"]   # 봇마다 다른 사용자에 설치된 claude
    cmd = [claude_bin, "-p",
           build_prompt(key, fields.get("summary", ""), adf_to_text(fields.get("description")).strip(),
                        bool(old_session), product, send_brief, notes),
           "--output-format", "stream-json", "--verbose", "--max-turns", str(max_turns)]
    if acfg.get("model"):
        cmd += ["--model", acfg["model"]]
    if old_session:
        cmd += ["--resume", old_session]
    cmd += role_args
    cmd += list(acfg.get("extra_args") or [])

    user = pwd.getpwnam(acfg["user"])
    child_env = {"HOME": user.pw_dir, "USER": user.pw_name, "LANG": "C.UTF-8",
                 "PATH": f"{os.path.dirname(claude_bin)}:/usr/local/bin:/usr/bin:/bin",
                 "XDG_RUNTIME_DIR": f"/run/user/{user.pw_uid}"}
    child_env["DISABLE_AUTOUPDATER"] = "1"
    if acfg.get("display"):   # 브라우저 봇: 봇 전용 가상 화면
        child_env["DISPLAY"] = f":{acfg['display']}"
        child_env["XAUTHORITY"] = os.path.join(user.pw_dir, ".Xauthority")
    child_env["TELEGRAM_STATE_DIR"] = "/nonexistent/ai-team-headless"   # 혹시 플러그인이 떠도 토큰을 못 찾고 바로 종료
    if acfg.get("env_file"):   # 봇 전용 Claude 토큰 (claude setup-token) — 이 봇 프로세스에만 전달
        token = load_env(acfg["env_file"]).get("CLAUDE_CODE_OAUTH_TOKEN") if os.path.exists(acfg["env_file"]) else None
        if not token:
            jira.transition(key, "failed")
            jira.comment(key, f"[dispatcher] {agent} 봇의 Claude 토큰이 없습니다. VM 에서 sudo ai-bot secret {agent} claude 후 Scheduled 로 바꿔 주세요.")
            log(f"{key} 실패: {agent} Claude 토큰 없음")
            return
        child_env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    docker_sock = f"/run/user/{user.pw_uid}/docker.sock"
    if os.path.exists(docker_sock):  # rootless Docker 가 있으면 봇이 쓸 수 있게
        child_env["DOCKER_HOST"] = f"unix://{docker_sock}"
    # 봇 사용자 자신의 그룹만 (root 의 보조 그룹을 물려받지 않음. ai-approve 처럼 봇에게 준 그룹은 유지)
    groups = [g for g in os.getgrouplist(user.pw_name, user.pw_gid) if g != user.pw_gid]
    child = subprocess.Popen(cmd, cwd=work_dir, env=child_env, user=user.pw_uid, group=user.pw_gid, extra_groups=groups,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)

    stopped = {"why": None}

    def halt(why):
        if stopped["why"] is None:
            stopped["why"] = why
            child.terminate()

    signal.signal(signal.SIGTERM, lambda *_: halt("창 종료"))
    watchdog = threading.Timer(cfg.get("max_runtime_minutes", 180) * 60, halt, args=("최대 실행 시간 초과",))
    watchdog.daemon = True
    watchdog.start()

    def save_session(sid):
        try:
            jira.set_fields(key, {"session_id": sid})
        except JiraError as err:
            log(f"세션 ID 기록 실패: {err}")

    session_id, result = parse_stream(child.stdout, on_session=save_session)
    stderr_tail = child.stderr.read()[-1500:]
    child.wait()
    watchdog.cancel()

    # 결과 기록
    now = dt.datetime.now(cfg["_tz"])
    cost = float((result or {}).get("total_cost_usd") or 0)
    updates = {"usage": round(prev_usage + cost, 4)}
    if session_id:
        updates["session_id"] = session_id
    if cost:
        ledger_add(cfg, key, product, agent, cost)

    report = (result or {}).get("result") or ""
    stats = f"턴 {(result or {}).get('num_turns', '-')} · 사용량 {cost:.3f}"
    resume_hint = "같은 세션에서 이어서 진행합니다."
    if stopped["why"]:
        updates["run_at"] = next_allowed_start(window, cfg, now)
        status, icon, kind = "paused", "⏸", "info"
        title = f"[{agent}] {stopped['why']}로 중단 · {updates['run_at']:%m-%d %H:%M} 이후 {resume_hint}"
        body = ""
        notice = f"{stopped['why']}로 중단, {updates['run_at']:%m-%d %H:%M} 이후 이어서 진행"
    elif result is None:
        status, icon, kind = "failed", "❌", "error"
        title = f"[{agent}] 결과 없이 종료 (exit {child.returncode})"
        body = f"```\n{stderr_tail}\n```" if stderr_tail.strip() else ""
        notice = "결과 없이 종료 (실행 오류)"
    elif looks_rate_limited(result):
        updates["run_at"] = now + dt.timedelta(minutes=cfg["budget"].get("rate_limit_backoff_minutes", 60))
        status, icon, kind = "paused", "⏸", "info"
        title = f"[{agent}] 사용량 한도 · {updates['run_at']:%H:%M} 에 다시 시도"
        body = ""
        notice = f"사용량 한도, {updates['run_at']:%H:%M} 재시도"
    elif result.get("subtype") == "error_max_turns":
        status, icon, kind = "failed", "❌", "error"
        title = (f"[{agent}] Max Turns({max_turns}) 도달 · {stats} — "
                 f"Max Turns 를 늘리고 Scheduled 로 바꾸면 {resume_hint}")
        body = report
        notice = f"Max Turns({max_turns}) 도달"
    elif result.get("permission_denials"):
        denied = "\n".join(
            f"- `{d.get('tool_name')}: {json.dumps(d.get('tool_input', {}), ensure_ascii=False)[:200].replace('`', chr(39))}`"
            for d in result["permission_denials"][:10])
        status, icon, kind = "review", "⚠️", "warning"
        title = (f"[{agent}] 종료 · {stats} · 일부 호출 권한 거부 — 보고대로 끝났는지 확인해 주세요. "
                 f"못 끝냈다면 권한을 고친 뒤 Scheduled 로 바꾸면 {resume_hint}")
        body = f"**거부된 호출**\n{denied}\n\n---\n\n{report}"
        notice = f"검토 대기 (일부 권한 거부 {len(result['permission_denials'])}건)\n{report_summary(report)}"
    elif result.get("is_error"):
        status, icon, kind = "failed", "❌", "error"
        title = f"[{agent}] 오류로 종료 · {result.get('subtype')}"
        body = report or (f"```\n{stderr_tail}\n```" if stderr_tail.strip() else "")
        notice = f"오류로 종료: {result.get('subtype')}"
    else:
        status, icon, kind = "review", "✅", "success"
        title = f"[{agent}] 완료 · {stats}"
        body = report
        notice = f"검토 대기\n{report_summary(report)}"

    jira.set_fields(key, updates)
    jira.transition(key, status)
    jira.comment(key, body, panel=(kind, title))
    notify(cfg, f"{icon} {key} · {agent}\n{fields.get('summary', '')}\n\n{notice}\n\n{issue_link(cfg, key)}")
    log(f"{key} 종료: {stopped['why'] or (result or {}).get('subtype') or 'no-result'}")

    request = extract_admin_request(report) if result else None
    if request:
        try:
            admin_key = create_admin_request(jira, cfg, request[0], request[1], key, product, agent)
            jira.comment(key, f"관리자 명령이 필요해 {admin_key} 로 승인을 요청했습니다. 승인·실행되면 이 작업이 자동으로 다시 예약됩니다.",
                         panel=("info", f"[dispatcher] 관리 요청 {admin_key}"))
        except (ValueError, JiraError) as err:
            log(f"{key} 관리 요청 생성 실패: {err}")


def _mobile_line(line):
    """마크다운 한 줄 → 휴대폰 알림용 평문 한 줄 (표 구분선은 None)."""
    t = line.strip()
    if re.fullmatch(r"\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?", t):
        return None
    if t.startswith("|") and t.endswith("|"):
        cells = [c.strip() for c in t.strip("|").split("|") if c.strip()]
        t = "• " + " — ".join(cells)
    elif re.match(r"^#{1,6}\s", t):
        t = "▪ " + re.sub(r"^#{1,6}\s+", "", t)
    elif re.match(r"^[-*]\s", t):
        t = "• " + t[2:].strip()
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)          # [제목](URL) → 제목
    t = t.replace("**", "").replace("`", "")
    return t


def report_summary(report, limit=700, max_lines=10):
    """보고서의 '## 요약' 섹션(없으면 첫 소제목 전까지)을 휴대폰 알림용 여러 줄로.
    표는 '• 칸 — 칸' 줄로 바꾸고(머리줄 제외), 줄 단위로 자른다."""
    lines = (report or "").splitlines()
    start = next((i + 1 for i, l in enumerate(lines) if re.match(r"^#{1,3}\s*요약\s*$", l.strip())), None)
    if start is None:
        start = next((i for i, l in enumerate(lines) if l.strip() and not l.strip().startswith("#")), 0)
    out, in_table, total = [], False, 0
    for line in lines[start:]:
        if re.match(r"^#{1,3}\s", line.strip()) and out:
            break
        if not line.strip():
            in_table = False
            continue
        is_row = line.strip().startswith("|")
        if is_row and not in_table:          # 표 머리줄은 건너뜀
            in_table = True
            continue
        text = _mobile_line(line)
        if not text:
            continue
        if len(out) >= max_lines or total + len(text) > limit:
            out.append("… (전체 내용은 Jira)")
            break
        out.append(text)
        total += len(text) + 1
    return "\n".join(out)


# ─────────────────────────── Telegram ───────────────────────────

def issue_link(cfg, key):
    site = cfg["jira"].get("site", "").rstrip("/")
    return f"{site}/browse/{key}" if site else key


def telegram_token(env_file):
    env = load_env(env_file)
    for name in ("TELEGRAM_BOT_TOKEN", "BOT_TOKEN", "TELEGRAM_TOKEN"):
        if env.get(name):
            return env[name]
    for name, value in env.items():
        if "TOKEN" in name.upper() and value:
            return value
    raise KeyError(f"{env_file} 에서 봇 토큰을 찾지 못했습니다")


def notify(cfg, text):
    """leader 봇 토큰으로 '보내기'만 합니다 (메시지 수신은 기존 Telegram 플러그인 담당)."""
    tg = cfg.get("telegram") or {}
    if not tg.get("chat_id") or not tg.get("env_file"):
        return
    try:
        token = telegram_token(tg["env_file"])
        data = json.dumps({"chat_id": tg["chat_id"], "text": text[:4000],
                           "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as err:  # noqa: BLE001 — 알림 실패가 작업을 막으면 안 됨
        log(f"Telegram 알림 실패: {type(err).__name__}")


# ─────────────────────────── outbox (leader → 디스패처) ───────────────────────────

MAX_REQUEST_BYTES = 20_000
WINDOWS = ("ASAP", "Night", "Weekend")


def read_request(path):
    """심볼릭 링크·특수 파일·대용량을 거부하고 JSON 을 읽습니다. (요청 파일은 봇 사용자가 쓰고 루트가 읽음)"""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not (st.st_mode & 0o170000 == 0o100000) or st.st_size > MAX_REQUEST_BYTES:
            raise ValueError("일반 파일이 아니거나 너무 큽니다")
        with os.fdopen(fd, "rb") as fh:
            fd = None
            return json.loads(fh.read().decode("utf-8"))
    finally:
        if fd is not None:
            os.close(fd)


def parse_run_at(value, cfg):
    if not value:
        return None
    when = dt.datetime.fromisoformat(str(value))
    return when if when.tzinfo else when.replace(tzinfo=cfg["_tz"])


def validate_request(req, cfg):
    """요청을 검증해 정규화된 dict 로. 문제가 있으면 ValueError."""
    if not isinstance(req, dict):
        raise ValueError("JSON 객체가 아닙니다")
    action = req.get("action")
    key_pattern = rf"^{re.escape(cfg['jira']['project_key'])}-\d+$"
    if action == "create":
        summary = str(req.get("summary") or "").strip()
        if not 1 <= len(summary) <= 200:
            raise ValueError("summary 는 1~200자")
        agent = req.get("agent")
        if agent not in cfg["agents"]:
            raise ValueError(f"agent 는 {', '.join(cfg['agents'])} 중 하나")
        product = req.get("product") or cfg["default_product"]
        if product not in cfg["products"]:
            raise ValueError(f"product 는 {', '.join(cfg['products'])} 중 하나")
        window = req.get("window") or "Night"
        if window not in WINDOWS:
            raise ValueError("window 는 ASAP / Night / Weekend")
        max_turns = int(req.get("max_turns") or 30)
        if not 1 <= max_turns <= 100:
            raise ValueError("max_turns 는 1~100")
        description = str(req.get("description") or "")[:8000]
        return {"action": action, "summary": summary, "agent": agent, "product": product, "window": window,
                "run_at": parse_run_at(req.get("run_at"), cfg), "max_turns": max_turns, "description": description}
    if action in ("approve", "reschedule"):
        key = norm_key(cfg, req.get("key"))
        if not re.match(key_pattern, key):
            raise ValueError(f"key 형식이 잘못됐습니다: {key}")
        out = {"action": action, "key": key}
        if action == "reschedule":
            window = req.get("window")
            if window and window not in WINDOWS:
                raise ValueError("window 는 ASAP / Night / Weekend")
            out["window"] = window
            out["run_at"] = parse_run_at(req.get("run_at"), cfg)
            out["note"] = str(req.get("note") or "").strip()[:1000]
        return out
    if action == "admin":
        command = str(req.get("command") or "").strip()
        if not 1 <= len(command) <= MAX_ADMIN_COMMAND:
            raise ValueError(f"command 는 1~{MAX_ADMIN_COMMAND}자")
        related = norm_key(cfg, req.get("related")) or None
        if related and not re.match(key_pattern, related):
            raise ValueError(f"related 형식이 잘못됐습니다: {related}")
        product = req.get("product") or cfg["default_product"]
        if product not in cfg["products"]:
            raise ValueError(f"product 는 {', '.join(cfg['products'])} 중 하나")
        return {"action": action, "command": command, "reason": str(req.get("reason") or "")[:300],
                "related": related, "product": product}
    raise ValueError("action 은 create / approve / reschedule / admin")


def apply_request(jira, cfg, req):
    """검증된 요청을 Jira 에 반영하고 알림 문구를 돌려줍니다."""
    f, s = jira.f, jira.s
    if req["action"] == "create":
        fields = {
            "project": {"key": jira.project},
            "issuetype": {"id": cfg["jira"]["task_type_id"]},
            "summary": req["summary"],
            "description": md_to_adf(req["description"] or req["summary"]),
            f["agent"]: {"value": req["agent"]},
            f["product"]: jira.choice("product", req["product"]),
            f["window"]: {"value": req["window"]},
            f["max_turns"]: req["max_turns"],
        }
        if req["run_at"]:
            fields[f["run_at"]] = to_jira_time(req["run_at"])
        key = jira.create(fields)
        jira.transition(key, "scheduled")
        now = dt.datetime.now(cfg["_tz"])
        start = req["run_at"] or now
        if not window_allows(req["window"], cfg, start):
            start = next_allowed_start(req["window"], cfg, start)
        when = "곧 실행" if start <= now else f"{start:%m-%d %H:%M} 이후 실행"
        return (f"📝 {key} 등록 [{req['agent']}/{req['product']}/{req['window']}] {req['summary']}\n"
                f"{when}\n{issue_link(cfg, key)}")

    if req["action"] == "admin":
        key = create_admin_request(jira, cfg, req["command"], req["reason"], req["related"], req["product"], "leader")
        return f"🔐 관리 요청 {key} 등록 — Jira 에서 명령 확인 후 Scheduled 로 옮기면 실행\n{issue_link(cfg, key)}"

    key = req["key"]
    current = jira.get(key)["fields"]["status"]["id"]
    if req["action"] == "approve":
        if current != s["review"]:
            raise ValueError(f"{key} 는 검토 중 상태가 아니라 승인할 수 없습니다")
        jira.transition(key, "done")
        jira.comment(key, "[dispatcher] Telegram 으로 승인되어 완료 처리했습니다.")
        return f"🏁 {key} 승인 → 완료\n{issue_link(cfg, key)}"

    # reschedule
    if current not in (s["paused"], s["failed"], s["review"], s["scheduled"], s["todo"]):
        raise ValueError(f"{key} 는 지금 다시 예약할 수 없는 상태입니다 (진행 중이거나 완료)")
    updates = {}
    if req.get("run_at"):
        updates["run_at"] = req["run_at"]
    if req.get("window"):
        jira.set_raw(key, {f["window"]: {"value": req["window"]}})
    if updates:
        jira.set_fields(key, updates)
    if current != s["scheduled"]:
        jira.transition(key, "scheduled")
    if req.get("note"):
        add_note(cfg, key, req["note"])
        jira.comment(key, req["note"], panel=("info", "[사용자 메모] 다시 예약 — 다음 실행 때 담당 봇에게 전달됩니다"))
        return f"🔁 {key} 다시 예약 (메모 전달 예정)\n{issue_link(cfg, key)}"
    jira.comment(key, "[dispatcher] Telegram 요청으로 다시 예약했습니다.")
    return f"🔁 {key} 다시 예약\n{issue_link(cfg, key)}"


def process_outbox(jira, cfg, dry_run):
    box = (cfg.get("telegram") or {}).get("outbox")
    if not box or not os.path.isdir(box):
        return
    names = sorted(n for n in os.listdir(box) if n.endswith(".json"))
    for name in names:
        path = os.path.join(box, name)
        try:
            req = validate_request(read_request(path), cfg)
            if dry_run:
                log(f"[dry-run] 요청 {name}: {req['action']} {req.get('key') or req.get('summary')}")
                continue
            message = apply_request(jira, cfg, req)
            dest = "processed"
        except (ValueError, KeyError, OSError, JiraError, json.JSONDecodeError) as err:
            if dry_run:
                log(f"[dry-run] 요청 {name} 거부: {err}")
                continue
            message = f"⚠️ 요청 처리 실패 ({name})\n{err}"
            dest = "failed"
        os.makedirs(os.path.join(box, dest), exist_ok=True)
        os.replace(path, os.path.join(box, dest, name))
        log(message.splitlines()[0])
        notify(cfg, message)


# ─────────────────────────── 승인형 관리 명령 ───────────────────────────
#  흐름: 봇 보고서의 '## 관리자 명령 요청' 또는 leader 의 admin 요청
#        → Jira 관리 요청 이슈(라벨 admin-request, 해야 할 일) + Telegram 알림
#        → 사용자 계정이 Scheduled 로 옮기면 승인 (봇 계정이 옮긴 건 무시)
#        → 디스패처가 root 로 실행, 결과 코멘트·알림, 관련 작업 자동 재예약

ADMIN_SECTION_RE = re.compile(r"^#{1,6}\s*관리자 명령 요청\s*$", re.M)
MAX_ADMIN_COMMAND = 2000


def extract_admin_request(report):
    """보고서 끝의 '관리자 명령 요청' 섹션에서 (명령, 이유) 를 뽑습니다. 없으면 None."""
    match = ADMIN_SECTION_RE.search(report or "")
    if not match:
        return None
    rest = report[match.end():]
    nxt = re.search(r"^#{1,6}\s", rest, re.M)
    section = rest[:nxt.start()] if nxt else rest
    code = re.search(r"```[^\n]*\n(.*?)```", section, re.S)
    if not code or not code.group(1).strip():
        return None
    reason = re.search(r"이유\s*[:：]\s*(.+)", section)
    return code.group(1).strip(), (reason.group(1).strip() if reason else "")


def admin_label(cfg):
    return (cfg.get("admin") or {}).get("label", "admin-request")


def create_admin_request(jira, cfg, command, reason, related=None, product=None, requester="leader"):
    command = command.strip()
    if not command or len(command) > MAX_ADMIN_COMMAND:
        raise ValueError(f"관리 명령은 1~{MAX_ADMIN_COMMAND}자")
    first = command.splitlines()[0][:70]
    origin = f"{related} [{requester}]" if related else f"[{requester}]"
    description = (f"**요청:** {origin}\n**이유:** {reason or '(없음)'}\n\n```bash\n{command}\n```\n\n"
                   "**승인**: 명령을 확인한 뒤 이 이슈를 **Scheduled** 로 옮기면 디스패처가 root 로 실행합니다.\n"
                   "**거절**: **완료**로 옮기세요. 사용자 계정이 아닌 다른 계정이 옮긴 경우는 무시됩니다.")
    fields = {
        "project": {"key": jira.project},
        "issuetype": {"id": cfg["jira"]["task_type_id"]},
        "summary": f"[관리 요청] {first}",
        "description": md_to_adf(description),
        "labels": [admin_label(cfg)],
        jira.f["product"]: jira.choice("product", product or cfg["default_product"]),
    }
    key = jira.create(fields)
    shown = command if len(command) <= 600 else command[:600] + "\n…"
    notify(cfg, f"🔐 관리 요청 {key} ({origin})\n이유: {reason or '-'}\n\n{shown}\n\n"
                f"명령을 꼭 확인하고, 승인하려면 Jira 에서 Scheduled 로 옮기세요.\n{issue_link(cfg, key)}")
    return key


def approval_status(jira, cfg, key):
    """마지막 상태 변경이 'Scheduled 로, 승인자 계정에 의해' 이뤄졌는지. (승인됨 여부, 설명)"""
    approver = (cfg.get("admin") or {}).get("approver_account_id")
    if not approver:
        return False, "설정에 approver_account_id 가 없습니다"
    last = None
    for history in jira.changelog(key):
        for item in history.get("items", []):
            if item.get("fieldId") == "status" or item.get("field") == "status":
                last = (history.get("author", {}), item.get("to"))
    if not last:
        return False, "상태 변경 기록이 없습니다"
    author, to_status = last
    if to_status != jira.s["scheduled"]:
        return False, "마지막 상태 변경이 Scheduled 가 아닙니다"
    if author.get("accountId") != approver:
        return False, f"승인자가 아닌 계정({author.get('displayName')})이 옮겼습니다"
    return True, author.get("displayName", "")


def first_code_block(adf):
    if not isinstance(adf, dict):
        return None
    if adf.get("type") == "codeBlock":
        return "".join(n.get("text", "") for n in adf.get("content", []))
    for child in adf.get("content", []):
        found = first_code_block(child)
        if found is not None:
            return found
    return None


def related_key(cfg, adf):
    match = re.search(rf"요청:\s*({re.escape(cfg['jira']['project_key'])}-\d+)", adf_to_text(adf))
    return match.group(1) if match else None


def admin_unit(key):
    return f"ai-admin-{key.lower()}"


def process_admin_approvals(jira, cfg, dry_run):
    s = jira.s
    jql = (f"project = {jira.project} AND labels = {admin_label(cfg)} AND status = {s['scheduled']} "
           f"ORDER BY created ASC")
    for issue in jira.search(jql, max_results=10):
        key = issue["key"]
        ok, why = approval_status(jira, cfg, key)
        if dry_run:
            log(f"[dry-run] 관리 요청 {key}: {'승인됨 → 실행' if ok else '실행 안 함 (' + why + ')'}")
            continue
        if not ok:
            jira.transition(key, "todo")
            jira.comment(key, f"승인으로 인정되지 않아 되돌렸습니다: {why}", panel=("warning", "[dispatcher] 실행 안 함"))
            notify(cfg, f"⚠️ 관리 요청 {key} 실행 안 함: {why}\n{issue_link(cfg, key)}")
            continue
        jira.transition(key, "in_progress")
        cmd = ["systemd-run", f"--unit={admin_unit(key)}", "--collect",
               f"--setenv=AI_TEAM_CONFIG={CONFIG_PATH}",
               sys.executable, os.path.abspath(__file__), "admin-run", key]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            jira.transition(key, "failed")
            jira.comment(key, result.stderr.strip()[:2000], panel=("error", "[dispatcher] 실행을 시작하지 못했습니다"))
        log(f"관리 요청 {key} 실행 시작 (승인: {why})")


def admin_run(cfg, key):
    jira = Jira(cfg)
    issue = jira.raw_issue(key, ["description", "summary", "labels", "status", jira.f["product"]])
    fields = issue["fields"]
    if admin_label(cfg) not in (fields.get("labels") or []):
        raise SystemExit(f"{key} 는 관리 요청 이슈가 아닙니다")
    command = (first_code_block(fields.get("description")) or "").strip()
    # 실행 직전 다시 확인: 승인 이후 다른 상태 변경이 끼어들지 않았는지 (진행 중은 디스패처가 바꾼 것)
    history = [(h.get("author", {}).get("accountId"), i.get("to"))
               for h in jira.changelog(key) for i in h.get("items", [])
               if i.get("fieldId") == "status" or i.get("field") == "status"]
    approver = (cfg.get("admin") or {}).get("approver_account_id")
    if len(history) < 2 or history[-2] != (approver, jira.s["scheduled"]) or history[-1][1] != jira.s["in_progress"]:
        jira.transition(key, "todo")
        jira.comment(key, "승인 기록을 다시 확인하지 못해 실행하지 않았습니다.", panel=("warning", "[dispatcher] 실행 안 함"))
        return
    if not command or len(command) > MAX_ADMIN_COMMAND:
        jira.transition(key, "failed")
        jira.comment(key, "설명에서 실행할 명령(코드 블록)을 찾지 못했습니다.", panel=("error", "[dispatcher] 실행 안 함"))
        return

    timeout = (cfg.get("admin") or {}).get("timeout_minutes", 20) * 60
    env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root",
           "LANG": "C.UTF-8", "DEBIAN_FRONTEND": "noninteractive"}
    try:
        proc = subprocess.run(["/bin/bash", "-c", command], cwd="/root", env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
        code, output = proc.returncode, proc.stdout or ""
    except subprocess.TimeoutExpired as err:
        code = "timeout"
        output = (err.stdout or b"").decode(errors="replace") if isinstance(err.stdout, bytes) else (err.stdout or "")
    tail = output[-3500:].replace("```", "'''")
    success = code == 0
    jira.comment(key, f"```\n{tail or '(출력 없음)'}\n```",
                 panel=("success" if success else "error", f"[dispatcher] root 실행 {'성공' if success else '실패'} · 종료 코드 {code}"))
    jira.transition(key, "done" if success else "failed")
    notify(cfg, f"{'✅' if success else '❌'} 관리 요청 {key} {'완료' if success else f'실패 (종료 코드 {code})'}\n"
                f"{tail[-400:]}\n{issue_link(cfg, key)}")
    log(f"관리 요청 {key} 종료: {code}")

    related = related_key(cfg, fields.get("description"))
    if success and related and (cfg.get("admin") or {}).get("resume_related", True):
        try:
            status = jira.get(related)["fields"]["status"]["id"]
            if status in (jira.s["review"], jira.s["failed"], jira.s["paused"]):
                jira.transition(related, "scheduled")
                jira.comment(related, f"관리 요청 {key} 가 실행되어 이 작업을 다시 예약했습니다.",
                             panel=("info", "[dispatcher] 다시 예약"))
                notify(cfg, f"🔁 {related} 다시 예약 (관리 요청 {key} 완료)\n{issue_link(cfg, related)}")
        except JiraError as err:
            log(f"{related} 재예약 실패: {err}")


# ─────────────────────────── 아침 요약 ───────────────────────────

def summary(cfg):
    jira = Jira(cfg)
    s, f = jira.s, jira.f
    now = dt.datetime.now(cfg["_tz"])
    issues = jira.search(f"project = {jira.project} AND updated >= -24h AND "
                         f"status in ({s['review']}, {s['failed']}, {s['paused']}, {s['done']}) ORDER BY status")
    groups = {"review": [], "failed": [], "paused": [], "done": []}
    by_id = {v: k for k, v in s.items()}
    for issue in issues:
        t = describe(issue, f)
        name = by_id.get(issue["fields"]["status"]["id"])
        if name in groups and t["agent"]:
            groups[name].append(f"  {t['key']} [{t['agent']}] {t['summary']}")
    queued = [describe(i, f) for i in jira.search(
        f"project = {jira.project} AND status = {s['scheduled']} ORDER BY priority DESC")]
    used = usage_since(cfg, now - dt.timedelta(hours=24))

    parts = [f"☀️ {now:%m월 %d일} AI 팀 아침 요약"]
    for name, title in (("review", "✅ 검토 대기"), ("failed", "❌ 실패"), ("paused", "⏸ 중단"), ("done", "🏁 완료")):
        if groups[name]:
            parts.append(f"{title} {len(groups[name])}건\n" + "\n".join(groups[name][:10]))
    if queued:
        parts.append(f"📋 대기 중 {len(queued)}건\n" + "\n".join(
            f"  {t['key']} [{t['agent']}] {t['summary']}" for t in queued[:10]))
    total = sum(used.values())
    usage_line = ", ".join(f"{p} {v:.2f}" for p, v in sorted(used.items())) or "없음"
    parts.append(f"📊 지난 24시간 사용량 {total:.2f} / 사이클 예산 {cfg['budget']['per_cycle']}\n  {usage_line}")
    if len(parts) == 2 and not queued:
        parts.insert(1, "밤사이 처리된 작업이 없습니다.")
    notify(cfg, "\n\n".join(parts))
    print("\n\n".join(parts))


# ───────────────────────────── main ─────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_tick = sub.add_parser("tick")
    p_tick.add_argument("--dry-run", action="store_true")
    p_run = sub.add_parser("run")
    p_run.add_argument("key")
    sub.add_parser("summary")
    p_admin = sub.add_parser("admin-run")
    p_admin.add_argument("key")
    sub.add_parser("notify-test")
    args = parser.parse_args()

    cfg = load_config()
    if args.cmd == "tick":
        tick(cfg, dry_run=args.dry_run or cfg.get("dry_run", False))
    elif args.cmd == "run":
        run(cfg, args.key)
    elif args.cmd == "summary":
        summary(cfg)
    elif args.cmd == "admin-run":
        admin_run(cfg, args.key)
    else:
        notify(cfg, "🔔 AI 팀 디스패처 알림 테스트입니다.")
        print("전송 시도 완료 (실패 시 위에 'Telegram 알림 실패' 로그가 찍힙니다)")


if __name__ == "__main__":
    main()
