from pathlib import Path


HTML = Path("frontend_prototype.html").read_text(encoding="utf-8")


def test_observability_table_header_matches_seven_data_columns() -> None:
    assert 'colspan="7"' in HTML
    assert 'max-h-[420px] overflow-auto' in HTML
    assert 'min-w-[760px]' in HTML
    assert "obsState.calls.slice(0, 100)" in HTML
    assert "显示最近 ${visibleCalls.length} 条" in HTML
    for label in ("调用 ID", "Agent", "状态", "延迟", "输入/输出字符", "护栏", "时间"):
        assert f">{label}<" in HTML


def test_observability_charts_aggregate_repeated_agent_calls() -> None:
    assert "function aggregateObsCalls(calls)" in HTML
    assert "const groups = aggregateObsCalls(obsState.calls);" in HTML
    assert "avgLatency: group.latency / group.count" in HTML
    assert "successRate: group.success / group.count * 100" in HTML


def test_bar_chart_labels_are_adaptive_and_escaped() -> None:
    assert "estimatedGroupWidth < 72" in HTML
    assert "compactLabels = estimatedGroupWidth < 44" in HTML
    assert "labelAngle = compactLabels ? -55 : -35" in HTML
    assert "escapeHtml(fullLabel)" in HTML
    assert "escapeHtml(displayLabel)" in HTML


def test_observability_agent_names_have_short_chinese_labels() -> None:
    for label in ("技术分析", "基本面", "行业分析", "大盘环境", "综合研判", "交易建议", "风险管理", "质量评估"):
        assert label in HTML
