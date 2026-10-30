"""브라우저 안전장치: 저장·제출성 동작은 승인(ask), 임의 코드 실행은 거부(deny)."""
from conftest import load_script

g = load_script("src/ai-browser-guard", "guard")
ALLOW = ["saramin.co.kr"]
T = "mcp__playwright__"


def d(name, **inp):
    r = g.decide(T + name, inp, ALLOW)
    return r[0] if r else None


def test_risky_clicks_ask():
    for label in ("'저장' 버튼", "이력서 제출", "Apply now", "삭제", "결제하기"):
        assert d("browser_click", element=label) == "ask", label


def test_plain_clicks_pass():
    assert d("browser_click", element="'이력서 관리' 메뉴") is None


def test_code_execution_denied():
    for name in ("browser_evaluate", "browser_run_code_unsafe", "browser_drop"):
        assert d(name) == "deny"


def test_enter_and_submit_ask():
    assert d("browser_press_key", key="Enter") == "ask"
    assert d("browser_type", text="x", submit=True) == "ask"
    assert d("browser_type", text="x") is None


def test_navigation_allowlist():
    assert d("browser_navigate", url="https://www.saramin.co.kr/zf_user/") is None
    assert d("browser_navigate", url="https://evil.example/") == "ask"
    assert d("browser_navigate", url="https://saramin.co.kr.evil.example/") == "ask"
