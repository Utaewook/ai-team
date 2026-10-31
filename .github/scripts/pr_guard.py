#!/usr/bin/env python3
"""pr_guard — PR 안전 검사 (CI 전용, 표준 라이브러리만)

  pr_guard.py sensitive <base> <head>   보안 경로(권한 경계)를 건드린 파일 → PR 코멘트용 마크다운 출력
  pr_guard.py privacy                   추적 중인 파일에서 개인정보 찾기 (있으면 exit 1)
                                        패턴: 환경변수 PRIVACY_PATTERNS (한 줄에 하나, GitHub Secret)
                                        로그에는 파일:줄 과 패턴 번호만 찍는다 — 공개 저장소라 값은 절대 출력하지 않음
"""
import fnmatch
import os
import subprocess
import sys

MARKER = "<!-- ai-team:pr-guard -->"

# root 로 실행되거나 봇 권한·승인 경계를 정하는 파일 — 바뀌면 사람이 diff 를 꼼꼼히 봐야 한다
SENSITIVE = [
    "src/ai-approve", "src/ai-bot", "src/ai-browser-guard", "src/ai-project",
    "src/dispatcher.py", "src/update-instructions.py",
    "deploy/*", "systemd/*", "install.sh", "harden.sh", "install-browser.sh",
    "bots/*/settings.json", "bots/*/bot.yaml", "bots-optional/*/settings.json", "bots-optional/*/bot.yaml",
    ".github/*",
]


def sensitive_files(paths):
    return [p for p in paths if any(fnmatch.fnmatch(p, pat) for pat in SENSITIVE)]


def changed_files(base, head, cwd=None):
    res = subprocess.run(["git", "diff", "--name-only", f"{base}...{head}"], cwd=cwd,
                         check=True, capture_output=True, text=True)
    return [line for line in res.stdout.splitlines() if line]


def comment_body(files):
    if not files:
        return f"{MARKER}\n✅ 보안 경로 변경 없음"
    lines = [f"{MARKER}", f"⚠️ **보안 경로 변경 {len(files)}개** — root 로 실행되거나 권한·승인 경계를 정하는 파일입니다. "
             "Merge 전에 diff 를 직접 확인하세요.", ""]
    return "\n".join(lines + [f"- `{f}`" for f in files])


def parse_patterns(text):
    return [s for s in (line.strip() for line in (text or "").splitlines()) if s and not s.startswith("#")]


def find_private(patterns, cwd=None):
    """[(파일, 줄 번호, 패턴 번호 1부터)] — 추적 중인 텍스트 파일만, 대소문자 무시."""
    files = subprocess.run(["git", "ls-files", "-z"], cwd=cwd, check=True, capture_output=True).stdout
    needles = [p.casefold() for p in patterns]
    hits = []
    for rel in sorted(f for f in files.decode().split("\0") if f):
        with open(os.path.join(cwd or ".", rel), "rb") as fh:
            data = fh.read()
        if b"\0" in data:
            continue
        for no, line in enumerate(data.decode("utf-8", errors="ignore").splitlines(), 1):
            low = line.casefold()
            hits += [(rel, no, i) for i, n in enumerate(needles, 1) if n in low]
    return hits


def cmd_sensitive(base, head):
    print(comment_body(sensitive_files(changed_files(base, head))))
    return 0


def cmd_privacy():
    patterns = parse_patterns(os.environ.get("PRIVACY_PATTERNS"))
    if not patterns:
        if os.environ.get("PR_FROM_FORK") == "true":
            print("::notice::포크 PR 이라 Secret 이 없어 개인정보 검사를 건너뜀")
            return 0
        print("::error::Secret PRIVACY_PATTERNS 가 비어 있습니다 (Settings → Secrets and variables → Actions)")
        return 1
    hits = find_private(patterns)
    for rel, no, idx in hits:
        print(f"::error file={rel},line={no}::개인정보 패턴 #{idx} 발견 — {rel}:{no}")
    print(f"개인정보 검사: 패턴 {len(patterns)}개, 발견 {len(hits)}건")
    return 1 if hits else 0


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["sensitive"] and len(args) == 3:
        return cmd_sensitive(args[1], args[2])
    if args == ["privacy"]:
        return cmd_privacy()
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
