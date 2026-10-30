"""설정 저장소 양방향 동기화 — 로컬 bare 저장소를 원격으로 써서 시험."""
import importlib.machinery
import importlib.util
import os
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(tmp_path, monkeypatch):
    etc, proj, state = tmp_path / "etc", tmp_path / "proj", tmp_path / "state"
    (etc / "bots.d" / "leader").mkdir(parents=True)
    proj.mkdir()
    monkeypatch.setenv("AI_BOT_ETC", str(etc))
    monkeypatch.setenv("AI_TEAM_PROJECTS", str(proj))
    monkeypatch.setenv("AI_TEAM_DEPLOY_STATE", str(state))
    loader = importlib.machinery.SourceFileLoader("deploy", os.path.join(ROOT, "deploy", "ai-team-deploy"))
    spec = importlib.util.spec_from_loader("deploy", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    mod.notify = lambda text: None
    return mod, etc, proj


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def env(tmp_path, monkeypatch):
    mod, etc, proj = load(tmp_path, monkeypatch)
    remote = tmp_path / "remote.git"
    git("init", "--bare", "-b", "main", str(remote))
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@t"}.items():
        monkeypatch.setenv(k, v)
    c = {"branch": "main", "src_dir": str(tmp_path / "src"), "config_repo": str(remote),
         "config_key": "/nonexistent", "require_ci": True, "restart_bots": False}
    (etc / "team-config.yaml").write_text("products:\n  OPS: 0.4\n")
    (etc / "bots.d" / "leader" / "bot.yaml").write_text("enabled: true\n")
    (etc / "jira.env").write_text("JIRA_TOKEN=secret\n")      # 비밀값 — 올라가면 안 됨
    (proj / "OPS.md").write_text("# OPS\n")
    (proj / "INDEX.md").write_text("generated\n")
    return mod, c, etc, proj, remote, tmp_path


def remote_files(remote, tmp_path, name="peek"):
    out = tmp_path / name
    subprocess.run(["rm", "-rf", str(out)], check=True)
    git("clone", "-q", str(remote), str(out))
    return out


def test_first_sync_exports_server_without_secrets(env):
    mod, c, etc, proj, remote, tmp = env
    assert mod.config_sync(c) == []
    peek = remote_files(remote, tmp)
    assert (peek / "team-config.yaml").read_text() == "products:\n  OPS: 0.4\n"
    assert (peek / "bots" / "leader" / "bot.yaml").exists()
    assert (peek / "projects" / "OPS.md").exists()
    assert not (peek / "projects" / "INDEX.md").exists()
    assert not list(peek.rglob("*.env"))


def test_repo_change_applied_and_server_change_pushed(env):
    mod, c, etc, proj, remote, tmp = env
    mod.config_sync(c)
    # 저장소 쪽(PR Merge)에서 브리프 수정
    w = remote_files(remote, tmp, "work")
    (w / "projects" / "OPS.md").write_text("# OPS\n수정됨\n")
    (w / "projects" / "ME.md").write_text("# ME\n")
    git("add", "-A", cwd=w)
    git("commit", "-qm", "brief", cwd=w)
    git("push", "-q", "origin", "HEAD:main", cwd=w)
    # 서버 쪽(ai-project 등)에서 설정 수정
    (etc / "team-config.yaml").write_text("products:\n  OPS: 0.4\n  NEW: 0.2\n")
    applied = mod.config_sync(c)
    assert str(proj / "OPS.md") in applied and str(proj / "ME.md") in applied
    assert (proj / "OPS.md").read_text() == "# OPS\n수정됨\n"
    peek = remote_files(remote, tmp)
    assert "NEW: 0.2" in (peek / "team-config.yaml").read_text()
    assert (peek / "projects" / "OPS.md").read_text() == "# OPS\n수정됨\n"
    # 다시 돌려도 아무 일 없음
    assert mod.config_sync(c) == []


def test_conflict_is_reported_not_forced(env):
    mod, c, etc, proj, remote, tmp = env
    mod.config_sync(c)
    w = remote_files(remote, tmp, "work")
    (w / "team-config.yaml").write_text("products:\n  OPS: 0.9\n")
    git("commit", "-qam", "repo edit", cwd=w)
    git("push", "-q", "origin", "HEAD:main", cwd=w)
    (etc / "team-config.yaml").write_text("products:\n  OPS: 0.1\n")
    with pytest.raises(mod.Fail):
        mod.config_sync(c)
    assert (etc / "team-config.yaml").read_text() == "products:\n  OPS: 0.1\n"   # 서버 값은 그대로
