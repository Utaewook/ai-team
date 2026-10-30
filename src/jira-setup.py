#!/usr/bin/env python3
"""Jira 연결 확인 + team-config.yaml 에 넣을 ID 찾기 (root 로 실행, 읽기만 함)

  sudo python3 /opt/ai-team/jira-setup.py --approver-email 나@example.com

1) /etc/ai-team/jira.env 로 인증 확인 (Cloud ID 가 비어 있으면 찾아서 알려 줌)
2) 프로젝트·작업 유형·상태·커스텀 필드 ID 를 이름으로 찾음
3) 관리 요청 승인자(나)의 계정 ID 를 이메일로 찾음
4) team-config.yaml 의 jira / admin 부분에 붙여넣을 내용을 출력
"""
import argparse
import base64
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

FIELD_NAMES = {"agent": "Agent", "product": "Product", "window": "Window", "run_at": "Run At",
               "max_turns": "Max Turns", "usage": "Usage", "session_id": "Session ID"}
STATUS_NAMES = {"todo": ["해야 할 일", "To Do", "Backlog"], "scheduled": ["Scheduled"],
                "in_progress": ["진행 중", "In Progress"], "paused": ["Paused"],
                "review": ["검토 중", "In Review", "Review"], "done": ["완료", "Done"], "failed": ["Failed"]}
TASK_TYPES = ["작업", "Task"]


def load_env(path):
    env = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.split("#", 1)[0].strip()
    return env


def http_json(url, auth=None):
    headers = {"Accept": "application/json"}
    if auth:
        headers["Authorization"] = auth
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as err:
        body = err.read().decode(errors="replace")[:300]
        raise SystemExit(f"❌ {url} → HTTP {err.code}\n{body}") from None


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--env", default="/etc/ai-team/jira.env")
    p.add_argument("--project", default="TEAM")
    p.add_argument("--approver-email", help="관리 요청을 승인할 내 Jira 계정 이메일 (봇 계정 아님)")
    a = p.parse_args(argv)

    env = load_env(a.env)
    site = env["JIRA_URL"].rstrip("/")
    scoped = env.get("JIRA_TOKEN_SCOPED", "").lower() in ("1", "true", "yes")
    cloud_id = env.get("JIRA_CLOUD_ID")
    if scoped and not cloud_id:
        cloud_id = http_json(f"{site}/_edge/tenant_info")["cloudId"]
        print(f"ℹ️ Cloud ID: {cloud_id}  → jira.env 의 JIRA_CLOUD_ID= 에 넣어 주세요")
    base = f"https://api.atlassian.com/ex/jira/{cloud_id}" if scoped else site
    auth = "Basic " + base64.b64encode(f"{env['JIRA_EMAIL']}:{env['JIRA_TOKEN']}".encode()).decode()
    get = lambda path: http_json(base + path, auth)  # noqa: E731

    me = get("/rest/api/3/myself")
    print(f"✅ 인증: {me.get('displayName')} ({me.get('emailAddress') or env['JIRA_EMAIL']})")

    proj = get(f"/rest/api/3/project/{urllib.parse.quote(a.project)}")
    print(f"✅ 프로젝트: {proj['key']} — {proj['name']}")
    task_type = next((t for t in proj.get("issueTypes", []) if t["name"] in TASK_TYPES), None)
    problems = []
    if not task_type:
        problems.append(f"작업 유형({'/'.join(TASK_TYPES)})을 찾지 못함: {[t['name'] for t in proj.get('issueTypes', [])]}")

    statuses = {}
    for it in get(f"/rest/api/3/project/{urllib.parse.quote(a.project)}/statuses"):
        if task_type and it["id"] != task_type["id"]:
            continue
        for st in it["statuses"]:
            for key, names in STATUS_NAMES.items():
                if st["name"] in names and key not in statuses:
                    statuses[key] = st["id"]
    for key in STATUS_NAMES:
        if key not in statuses:
            problems.append(f"상태 '{STATUS_NAMES[key][0]}' 가 워크플로에 없음")

    fields = {}
    by_name = {}
    for f in get("/rest/api/3/field"):
        if f.get("custom"):
            by_name.setdefault(f["name"], []).append(f["id"])
    for key, name in FIELD_NAMES.items():
        ids = by_name.get(name, [])
        if len(ids) == 1:
            fields[key] = ids[0]
        else:
            problems.append(f"커스텀 필드 '{name}' " + ("가 없음" if not ids else f"가 여러 개: {ids}"))

    approver = None
    if a.approver_email:
        users = get(f"/rest/api/3/user/search?query={urllib.parse.quote(a.approver_email)}")
        humans = [u for u in users if u.get("accountType") == "atlassian"]
        if len(humans) == 1:
            approver = humans[0]["accountId"]
            if approver == me.get("accountId"):
                problems.append("승인자가 봇 계정과 같습니다. 봇 계정이 아닌 내 계정 이메일을 넣어 주세요")
        else:
            problems.append(f"승인자 계정을 하나로 찾지 못함 ({len(humans)}명). 이메일을 확인해 주세요")

    print("\n── team-config.yaml 에 붙여넣기 ──")
    print("jira:")
    print(f"  task_type_id: \"{task_type['id'] if task_type else '???'}\"")
    print("  fields:")
    for key in FIELD_NAMES:
        print(f"    {key + ':':11} {fields.get(key, '???')}")
    print("  statuses:")
    for key in STATUS_NAMES:
        print(f"    {key + ':':12} \"{statuses.get(key, '???')}\"")
    print("admin:")
    print(f"  approver_account_id: \"{approver or '??? (--approver-email 로 찾기)'}\"")
    if problems:
        print("\n⚠️ 확인할 것:")
        for pr in problems:
            print(f"  - {pr}")
        return 1
    print("\n✅ 모두 찾았습니다")
    return 0


if __name__ == "__main__":
    sys.exit(main())
