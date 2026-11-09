from app.engine.agents.stage_2.researcher_factory import _compact_context_report


def test_compact_context_report_keeps_head_and_tail():
    content = "HEAD" + ("x" * 6000) + "TAIL"

    compact = _compact_context_report(content, limit=1000)

    assert compact.startswith("HEAD")
    assert compact.endswith("TAIL")
    assert "已压缩" in compact
    assert len(compact) < 1200
