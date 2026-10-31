"""PR 안전 검사(.github/scripts/pr_guard.py) — 보안 경로 표시·개인정보 검사."""
import subprocess

import pytest

from conftest import load_script

guard = load_script(".github/scripts/pr_guard.py", "pr_guard")


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@t"}.items():
        monkeypatch.setenv(k, v)
    git("init", "-q", "-b", "main", cwd=tmp_path)
    return tmp_path


def commit(repo, files):
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text) if isinstance(text, bytes) else p.write_text(text, encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "c", cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo).strip()


# ── 보안 경로 ──

@pytest.mark.parametrize("path", [
    "src/ai-approve", "src/ai-bot", "src/ai-browser-guard", "src/dispatcher.py", "src/ai-project",
    "src/update-instructions.py", "deploy/ai-team-deploy", "deploy/manifest.tsv", "systemd/ai-team-dispatch.service",
    "install.sh", "harden.sh", "install-browser.sh", "bots/dev/settings.json", "bots/dev/bot.yaml",
    "bots-optional/concierge/settings.json", ".github/workflows/ci.yml", ".github/scripts/pr_guard.py",
])
def test_sensitive_paths_flagged(path):
    assert guard.sensitive_files([path]) == [path]


@pytest.mark.parametrize("path", [
    "README.md", "docs/SETUP.md", "tests/test_dispatcher.py", "bots/dev/CLAUDE.md", "src/ai-jira",
    "config/team-config.example.yaml",
])
def test_ordinary_paths_not_flagged(path):
    assert guard.sensitive_files([path]) == []


def test_changed_files_between_base_and_head(repo):
    base = commit(repo, {"README.md": "a", "src/ai-bot": "x"})
    git("checkout", "-q", "-b", "feat", cwd=repo)
    head = commit(repo, {"src/ai-bot": "y", "docs/new.md": "n"})
    assert sorted(guard.changed_files(base, head, cwd=repo)) == ["docs/new.md", "src/ai-bot"]


def test_comment_body_lists_sensitive_files():
    body = guard.comment_body(["src/ai-approve", "deploy/manifest.tsv"])
    assert body.startswith(guard.MARKER)
    assert "⚠️" in body and "`src/ai-approve`" in body and "`deploy/manifest.tsv`" in body


def test_comment_body_when_clean():
    body = guard.comment_body([])
    assert body.startswith(guard.MARKER) and "⚠️" not in body


# ── 개인정보 ──

def test_privacy_finds_file_and_line_case_insensitive(repo):
    commit(repo, {"a.md": "첫 줄\n담당: 홍길동\n", "b.txt": "mail: Hong.GilDong@Example.com\n", "c.md": "깨끗함\n"})
    hits = guard.find_private(["홍길동", "hong.gildong@example.com"], cwd=repo)
    assert hits == [("a.md", 2, 1), ("b.txt", 1, 2)]


def test_privacy_skips_binary_and_untracked(repo):
    commit(repo, {"img.png": b"\x89PNG\x00\x01hong"})
    (repo / "untracked.md").write_text("hong", encoding="utf-8")
    assert guard.find_private(["hong"], cwd=repo) == []


def test_parse_patterns_ignores_blank_and_comment_lines():
    assert guard.parse_patterns("# 이름\n홍길동\n\n  12345678  \n") == ["홍길동", "12345678"]


def test_privacy_command_fails_without_printing_pattern(repo, monkeypatch, capsys):
    commit(repo, {"a.md": "chat 987654321\n"})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PRIVACY_PATTERNS", "987654321")
    assert guard.main(["privacy"]) == 1
    out = capsys.readouterr()
    assert "a.md:1" in out.out
    assert "987654321" not in out.out + out.err      # 공개 CI 로그에 값이 찍히면 안 됨


def test_privacy_command_passes_when_clean(repo, monkeypatch):
    commit(repo, {"a.md": "nothing\n"})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PRIVACY_PATTERNS", "홍길동")
    assert guard.main(["privacy"]) == 0


def test_privacy_command_fails_when_secret_missing(repo, monkeypatch):
    monkeypatch.chdir(repo)
    monkeypatch.delenv("PRIVACY_PATTERNS", raising=False)
    monkeypatch.setenv("PR_FROM_FORK", "false")
    assert guard.main(["privacy"]) == 1


def test_privacy_command_skips_for_fork_pr_without_secret(repo, monkeypatch):
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PRIVACY_PATTERNS", "")
    monkeypatch.setenv("PR_FROM_FORK", "true")
    assert guard.main(["privacy"]) == 0
