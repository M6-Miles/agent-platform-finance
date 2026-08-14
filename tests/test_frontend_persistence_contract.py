from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _frontend() -> str:
    return (ROOT / "frontend_prototype.html").read_text(encoding="utf-8")


def test_sidebar_uses_persisted_api_data_instead_of_demo_arrays() -> None:
    html = _frontend()
    assert "MOCK_SESSIONS" not in html
    assert "MOCK_RECENT" not in html
    assert "callApi('/sessions?limit=20'" in html
    assert "callApi('/analysis-history?limit=5'" in html
    assert "暂无会话" in html
    assert "暂无分析记录" in html


def test_chat_restores_and_continues_the_selected_sqlite_session() -> None:
    html = _frontend()
    assert "let currentChatSessionId = null" in html
    assert "session_id: currentChatSessionId" in html
    assert "/messages`" in html
    assert "currentChatSessionId = res.session_id" in html
    assert "prompt('会话名称：')" not in html
    assert "currentChatSessionId = null" in html


def test_user_message_html_is_escaped_before_rendering() -> None:
    html = _frontend()
    assert "${escapeHtml(text)}</div>" in html


def test_concurrent_api_errors_are_scoped_by_request_path() -> None:
    html = _frontend()
    assert "window._apiFailures.set(path" in html
    assert "function getApiFailure(path, fallback)" in html
    assert "getApiFailure('/chat'" in html
    assert "getApiFailure('/comparison'" in html
    assert "getApiFailure('/backtest'" in html


def test_paper_quote_refresh_tracks_its_own_elapsed_time() -> None:
    html = _frontend()
    start = html.index("async function pushTick")
    end = html.index("function renderTickerPanel", start)
    function_body = html[start:end]
    assert "const startedAt = performance.now()" in function_body
    assert "performance.now() - startedAt" in function_body
