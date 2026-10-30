"""디스패처: 휴대폰 알림 요약, Product 텍스트 필드."""
from conftest import load_script

d = load_script("src/dispatcher.py", "dispatcher_t")


def test_summary_prefers_summary_section():
    r = "## 요약\n- 이력서 **1건**, 마지막 수정 2026.10.20\n- 조회만 함\n## 상세\n| a | b |\n|---|---|\n| x | y |"
    assert d.report_summary(r) == "• 이력서 1건, 마지막 수정 2026.10.20\n• 조회만 함"


def test_summary_tables_become_lines():
    r = "총 1건입니다.\n\n| 제목 | 수정일 |\n|---|---|\n| 이력서 A | 2026.10.20 |\n\n## 변경 사항\n없음"
    assert d.report_summary(r) == "총 1건입니다.\n• 이력서 A — 2026.10.20"


def test_summary_is_cut_by_lines():
    r = "## 요약\n" + "\n".join(f"- 줄 {i}" for i in range(30))
    out = d.report_summary(r).splitlines()
    assert len(out) == 11 and out[-1].startswith("…")


def test_product_key_normalized():
    assert d.product_key({"value": "STR"}) == "STR"
    assert d.product_key(" me ") == "ME"
    assert d.product_key("") is None and d.product_key(None) is None
