"""확인 서비스: leader 의 프로젝트(Product 키) 추가 — 검증·설정 수정·권한."""
import pytest

from conftest import load_script

CONFIG = """jira:
  text_fields: [product]
  fields:
    product: customfield_1
products:
  STR: 0.6     # a
  OPS: 0.4     # b
default_product: OPS
context_dir: {proj}
"""
BRIEF = "# ME — 개인 업무\n\n이력서·구직 사이트 관리. concierge 담당.\n"


@pytest.fixture
def m(tmp_path, monkeypatch):
    etc, proj = tmp_path / "etc", tmp_path / "proj"
    (etc / "bots.d" / "leader").mkdir(parents=True)
    proj.mkdir()
    (etc / "team-config.yaml").write_text(CONFIG.format(proj=proj))
    (etc / "bots.d" / "leader" / "bot.yaml").write_text("approve: [mail.send, project.add, project.brief]\n")
    (tmp_path / "shared.yaml").write_text("# 생성됨\nproducts: [STR, OPS]\n")
    upd = tmp_path / "upd"
    upd.write_text("#!/bin/sh\nexit 0\n")
    upd.chmod(0o755)
    for k, v in {"AI_BOT_ETC": etc, "AI_APPROVE_STATE": tmp_path / "state", "AI_JIRA_CONFIG": tmp_path / "shared.yaml",
                 "AI_TEAM_UPDATE_INSTRUCTIONS": upd}.items():
        monkeypatch.setenv(k, str(v))
    mod = load_script("src/ai-approve", "approve_t")
    mod.notify = lambda text: None
    mod._paths = (etc, proj, tmp_path)
    return mod


def req(m, bot, action, payload):
    return m.handle(bot, {"op": "request", "action": action, "payload": payload})


def test_add_project_updates_config_brief_and_shared(m):
    etc, proj, tmp = m._paths
    r = req(m, "leader", "project.add", {"key": "me", "name": "개인 업무", "share": 0.2, "brief": BRIEF})
    assert r["ok"] and "텍스트 필드" in r["message"]
    text = (etc / "team-config.yaml").read_text()
    assert "  ME: 0.2     # 개인 업무" in text and text.index("ME:") < text.index("default_product")
    assert (proj / "ME.md").read_text() == BRIEF
    assert "- ME" in (tmp / "shared.yaml").read_text()


@pytest.mark.parametrize("payload", [
    {"key": "OPS", "name": "x", "brief": BRIEF},            # 중복
    {"key": "m", "name": "x", "brief": BRIEF},              # 너무 짧음
    {"key": "AB", "name": "x", "share": 5, "brief": BRIEF},  # 비율 범위
    {"key": "AB", "name": "a\nb", "brief": BRIEF},           # 여러 줄 이름
    {"key": "AB", "name": "x", "brief": "short"},            # 브리프 너무 짧음
])
def test_invalid_requests_refused(m, payload):
    with pytest.raises(m.Refuse):
        req(m, "leader", "project.add", payload)


def test_other_bots_cannot_add(m):
    with pytest.raises(m.Refuse):
        req(m, "dev", "project.add", {"key": "AB", "name": "x", "brief": BRIEF})


def test_brief_replace_keeps_title(m):
    req(m, "leader", "project.add", {"key": "ME", "name": "개인 업무", "brief": BRIEF})
    req(m, "leader", "project.brief", {"key": "ME", "brief": "새 내용 입니다. 브리프 교체 시험 문장."})
    assert m._paths[1].joinpath("ME.md").read_text().startswith("# ME — 개인 업무\n")
