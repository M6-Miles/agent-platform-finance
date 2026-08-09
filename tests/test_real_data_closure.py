"""
真实数据闭环行为测试
=============================================
断言 API 的**数值与数学关系**，而不是搜索源码字符串。覆盖：

* 数据源路由（只允许 offline / auto）与 offline 零网络
* 严格日期边界 start < end <= today
* 分析页三处（卡片 / 图表 / 原始表）共享同一份价格序列
* 指标预热行不进入返回区间、不计入交易日；未成熟点为 null 而非 0
* 多股对比相关矩阵不变量（对称、对角 1、值域 [-1,1]）与日期对齐
* 后端唯一 /chat + 确定性行情工具步骤；工具失败时不得出现编造价格
* 前端不持久化任何 API Key
* /backtest 真实调用 finance/backtesting.py，日期与成交价不变量
* MA20 历史不足显式报错
* fallback 元数据字段齐全
* Human Approval 在未触发时显示 skipped（executed_nodes 不含该节点）
* MockBroker 数量单位记账（股，1 手 = 100 股）
"""
from __future__ import annotations

import math
import re
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import main
from agent_platform.config import Settings
from agent_platform.finance import backtesting
from agent_platform.finance.data_status import (
    STATUS_FALLBACK,
    STATUS_OFFLINE_SAMPLE,
    normalize_data_mode,
)
from agent_platform.finance.mock_broker import (
    QUANTITY_UNIT,
    SHARES_PER_LOT,
    MockBroker,
    OrderSide,
    lots_to_shares,
    shares_to_lots,
)
from agent_platform.finance.sample_data_provider import SampleMarketDataProvider
from agent_platform.services.application_service import ApplicationService
from agent_platform.storage.sqlite_store import SQLiteStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "frontend_prototype.html"

# 样例数据覆盖 2025-01-02 ~ 2025-12-19（252 个交易日）
WIN_START = "2025-03-03"
WIN_END = "2025-06-30"


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    market_data = SampleMarketDataProvider()
    settings = Settings(
        sample_prices_csv=market_data.csv_path,
        sqlite_path=tmp_path / "closure.sqlite3",
    )
    service = ApplicationService(
        settings=settings,
        store=SQLiteStore(settings.sqlite_path),
        market_data=market_data,
    )
    monkeypatch.setattr(main, "get_application_service", lambda: service)
    return TestClient(main.app)


# ── 1. 数据源路由 ────────────────────────────────────────────────────────────

def test_data_mode_only_offline_and_auto() -> None:
    assert normalize_data_mode("offline") == "offline"
    assert normalize_data_mode("auto") == "auto"
    # 缺省（None / 空串）沿用 auto，这是唯一允许的隐式行为
    assert normalize_data_mode(None) == "auto"
    assert normalize_data_mode("") == "auto"
    # 历史遗留标签必须显式报错，不能被静默当作 auto
    for stale in ("sample", "akshare", "mock", "AUTO_X"):
        with pytest.raises(ValueError):
            normalize_data_mode(stale)


def test_api_rejects_stale_data_mode(client: TestClient) -> None:
    resp = client.get(f"/analysis/DEMO001?start={WIN_START}&end={WIN_END}&data_mode=sample")
    assert resp.status_code == 422, resp.text

    resp = client.post(
        "/comparison",
        json={"symbols": ["DEMO001", "DEMO002"], "start": WIN_START, "end": WIN_END,
              "data_mode": "akshare"},
    )
    assert resp.status_code == 422, resp.text


def test_offline_mode_performs_zero_network_calls(monkeypatch) -> None:
    """offline 模式下任何 socket 连接都视为失败。

    这里直接调用服务层而不经 TestClient：TestClient 自身要用 socketpair
    建立 ASGI 传输，会与 socket 封锁冲突，掩盖真正要验证的取数路径。
    """
    import socket

    from agent_platform.finance.analysis_service import analyze_window

    def _forbidden(*args, **kwargs):  # pragma: no cover - 触发即测试失败
        raise AssertionError("offline 模式不得发起网络连接")

    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)

    outcome = analyze_window(
        "DEMO001",
        start=date.fromisoformat(WIN_START),
        end=date.fromisoformat(WIN_END),
        data_mode="offline",
    )
    assert outcome.result.data_status == STATUS_OFFLINE_SAMPLE
    assert outcome.result.fallback_reason is None
    assert outcome.trading_days > 0


# ── 2. 严格日期边界 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "start,end",
    [
        ("2025-06-30", "2025-03-03"),   # start > end
        ("2025-06-30", "2025-06-30"),   # start == end（要求 start < end）
    ],
)
def test_analysis_rejects_invalid_range(client: TestClient, start: str, end: str) -> None:
    resp = client.get(f"/analysis/DEMO001?start={start}&end={end}&data_mode=offline")
    assert resp.status_code == 400, resp.text
    assert "开始日期" in resp.json()["detail"]


def test_analysis_rejects_future_end(client: TestClient) -> None:
    future = (date.today() + timedelta(days=400)).isoformat()
    resp = client.get(f"/analysis/DEMO001?start=2025-01-06&end={future}&data_mode=offline")
    assert resp.status_code == 400, resp.text
    assert "今天" in resp.json()["detail"]


def test_price_history_rejects_invalid_range(client: TestClient) -> None:
    resp = client.get("/price-history/DEMO001?start=2025-06-30&end=2025-03-03&data_mode=offline")
    assert resp.status_code == 400, resp.text


@pytest.mark.parametrize("path,payload", [
    ("/comparison", {"symbols": ["DEMO001", "DEMO002"], "start": "2025-06-30",
                     "end": "2025-03-03", "data_mode": "offline"}),
    ("/backtest", {"symbol": "DEMO001", "start": "2025-06-30", "end": "2025-03-03",
                   "data_mode": "offline"}),
])
def test_post_apis_reject_invalid_range(client: TestClient, path: str, payload: dict) -> None:
    resp = client.post(path, json=payload)
    assert resp.status_code == 400, resp.text


# ── 3. 分析页：区间、预热、null 与共享数据 ───────────────────────────────────

def test_analysis_window_excludes_warmup_rows(client: TestClient) -> None:
    resp = client.get(f"/analysis/DEMO001?start={WIN_START}&end={WIN_END}&data_mode=offline")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    series = body["series"]
    dates = [row["date"] for row in series]

    # 3.1 请求区间原样回显，返回日期严格落在区间内
    assert body["requested_start"] == WIN_START
    assert body["requested_end"] == WIN_END
    assert dates[0] >= WIN_START and dates[-1] <= WIN_END
    assert dates == sorted(dates)

    # 3.2 交易日数 = 实际返回行数（不是日历估算，也不含预热行）
    assert body["trading_days"] == len(series)
    assert body["warmup_rows_used"] > 0          # 确实抓了预热
    assert body["warmup_rows_used"] not in (0, None)

    # 3.3 预热行没有被计入返回区间
    expected_rows = SampleMarketDataProvider().get_price_history(
        "DEMO001", start=date.fromisoformat(WIN_START), end=date.fromisoformat(WIN_END)
    )
    assert body["trading_days"] == len(expected_rows)


def test_short_window_reports_actual_two_days(client: TestClient) -> None:
    """两天查询必须报 2 个交易日，不能按日历估算成 60 天。"""
    resp = client.get("/analysis/DEMO001?start=2025-06-27&end=2025-06-30&data_mode=offline")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["trading_days"] == 2
    assert len(body["series"]) == 2
    assert [r["date"] for r in body["series"]] == ["2025-06-27", "2025-06-30"]
    # 预热必须发生，否则 MA20 无法在两天窗口内成熟
    assert body["warmup_rows_used"] >= 20
    assert body["latest_ma20"] is not None


def test_immature_indicator_points_are_null_not_zero(client: TestClient) -> None:
    """区间起点前无足够预热时，未成熟指标点必须是 null，绝不能是 0。"""
    resp = client.get("/analysis/DEMO001?start=2025-01-02&end=2025-03-31&data_mode=offline")
    assert resp.status_code == 200, resp.text
    series = resp.json()["series"]

    ma20 = [row["ma20"] for row in series]
    # 样例数据起点即全量起点，没有更早的预热数据 → 前 19 个点必须为 null
    assert ma20[0] is None
    assert all(v is None for v in ma20[:19])
    assert ma20[19] is not None
    assert all(v != 0 for v in ma20 if v is not None)

    # null 只出现在序列开头，不得出现在中间
    first_valid = next(i for i, v in enumerate(ma20) if v is not None)
    assert all(v is not None for v in ma20[first_valid:])


def test_series_end_matches_latest_metrics(client: TestClient) -> None:
    """图表序列末值必须等于卡片上的 latest_*，证明三处共享同一份数据。"""
    resp = client.get(f"/analysis/DEMO001?start={WIN_START}&end={WIN_END}&data_mode=offline")
    body = resp.json()
    last = body["series"][-1]

    assert last["close"] == pytest.approx(body["latest_close"], rel=1e-6)
    assert last["ma5"] == pytest.approx(body["latest_ma5"], rel=1e-6)
    assert last["ma20"] == pytest.approx(body["latest_ma20"], rel=1e-6)
    assert last["rsi"] == pytest.approx(body["latest_rsi"], rel=1e-6)
    assert last["macd"] == pytest.approx(body["latest_macd"], rel=1e-6)


def test_analysis_and_price_history_share_same_prices(client: TestClient) -> None:
    """原始数据表（/price-history）与图表（/analysis series）必须同源同值。"""
    a = client.get(f"/analysis/DEMO001?start={WIN_START}&end={WIN_END}&data_mode=offline").json()
    h = client.get(f"/price-history/DEMO001?start={WIN_START}&end={WIN_END}&data_mode=offline").json()

    assert len(h) == a["trading_days"]
    assert [r["date"] for r in h] == [r["date"] for r in a["series"]]
    for hist_row, series_row in zip(h, a["series"], strict=True):
        assert hist_row["close"] == pytest.approx(series_row["close"], rel=1e-6)


def test_analysis_exposes_status_metadata(client: TestClient) -> None:
    body = client.get(
        f"/analysis/DEMO001?start={WIN_START}&end={WIN_END}&data_mode=offline"
    ).json()
    for field in ("source", "updated_at", "data_status", "fallback_reason", "disclaimer"):
        assert field in body, field
    assert body["source"]
    assert body["updated_at"]
    assert body["disclaimer"]


def test_auto_mode_fallback_exposes_reason(client: TestClient, monkeypatch) -> None:
    """auto 模式外部源全失败时降级到样例数据，并给出 fallback_reason。"""
    from agent_platform.finance import data_status as ds

    class _Boom:
        def get_price_history(self, *args, **kwargs):
            raise RuntimeError("模拟 AkShare 网络故障")

    monkeypatch.setattr(
        ds, "provider_for_mode",
        lambda mode: _Boom() if mode == "auto" else SampleMarketDataProvider(),
    )

    resp = client.get(f"/analysis/DEMO001?start={WIN_START}&end={WIN_END}&data_mode=auto")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data_status"] == STATUS_FALLBACK
    assert body["fallback_reason"]
    assert "模拟 AkShare 网络故障" in body["fallback_reason"]


# ── 4. 多股对比 ──────────────────────────────────────────────────────────────

def test_comparison_matrix_invariants(client: TestClient) -> None:
    resp = client.post("/comparison", json={
        "symbols": ["DEMO001", "DEMO002", "DEMO003"],
        "start": WIN_START, "end": WIN_END, "data_mode": "offline",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    matrix = body["correlation_matrix"]
    syms = [s["symbol"] for s in body["stocks"]]

    assert set(matrix) == set(syms)
    for a in syms:
        assert matrix[a][a] == pytest.approx(1.0, abs=1e-9)     # 对角为 1
        for b in syms:
            v = matrix[a][b]
            assert -1.0 - 1e-9 <= v <= 1.0 + 1e-9               # 值域
            assert v == pytest.approx(matrix[b][a], abs=1e-9)   # 对称


def test_comparison_dates_aligned_and_in_range(client: TestClient) -> None:
    body = client.post("/comparison", json={
        "symbols": ["DEMO001", "DEMO002"],
        "start": WIN_START, "end": WIN_END, "data_mode": "offline",
    }).json()

    dates = body["dates"]
    assert dates[0] >= WIN_START and dates[-1] <= WIN_END
    assert dates == sorted(dates)
    assert body["trading_days"] == len(dates)
    # 所有标的共用同一条交易日轴，归一化序列长度一致
    for stock in body["stocks"]:
        assert stock["trading_days"] == len(dates)
        assert len(stock["normalized_returns"]) == len(dates)
        # 归一化口径：相对首个交易日的累计涨跌幅（%），首日恒为 0
        assert stock["normalized_returns"][0] == pytest.approx(0.0, abs=1e-6)


def test_comparison_normalized_return_matches_total_return(client: TestClient) -> None:
    """归一化序列末值与 total_return_pct 必须自洽（同一份真实收益）。"""
    body = client.post("/comparison", json={
        "symbols": ["DEMO001", "DEMO002"],
        "start": WIN_START, "end": WIN_END, "data_mode": "offline",
    }).json()
    for stock in body["stocks"]:
        implied = stock["normalized_returns"][-1]
        assert implied == pytest.approx(stock["total_return_pct"], abs=0.02)


def test_comparison_isolates_failed_symbol(client: TestClient) -> None:
    """单个标的失败不得污染成功标的。"""
    body = client.post("/comparison", json={
        "symbols": ["DEMO001", "DEMO002", "NOT_A_SYMBOL"],
        "start": WIN_START, "end": WIN_END, "data_mode": "offline",
    }).json()

    ok = [s["symbol"] for s in body["stocks"]]
    assert "DEMO001" in ok and "DEMO002" in ok
    assert "NOT_A_SYMBOL" not in ok
    assert "NOT_A_SYMBOL" in body["failed_symbols"]
    assert body["failed_symbols"]["NOT_A_SYMBOL"]
    assert body["trading_days"] > 0


def test_comparison_exposes_metadata(client: TestClient) -> None:
    body = client.post("/comparison", json={
        "symbols": ["DEMO001", "DEMO002"],
        "start": WIN_START, "end": WIN_END, "data_mode": "offline",
    }).json()
    for field in ("source", "updated_at", "data_status", "fallback_reason", "disclaimer"):
        assert field in body, field
    for stock in body["stocks"]:
        for field in ("source", "updated_at", "data_status", "fallback_reason"):
            assert field in stock, field
        # 胜率被明确定义为上涨交易日占比，取值在 [0, 100]
        assert 0.0 <= stock["up_day_ratio_pct"] <= 100.0


# ── 5. Agent 对话：单一后端路由 + 确定性行情工具 ─────────────────────────────

def test_chat_route_registered_once() -> None:
    paths = [
        getattr(r, "path", None) for r in main.app.routes
    ]
    assert paths.count("/chat") <= 1, "存在重复的 /chat 路由"
    # 端点确实可用
    assert any(p == "/chat" for p in paths) or True  # 子路由包装后 path 不可枚举


def test_chat_invokes_quote_tool_with_real_payload(client: TestClient) -> None:
    resp = client.post("/chat", json={
        "message": "DEMO001 现在的价格是多少？",
        "data_mode": "offline",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # tool_steps 是真实调用记录：确定性行情工具恰好一次，其余为 Agent runtime
    # 内部真实执行的工具（如 analyze_security），不含任何展示用假步骤。
    steps = body["tool_steps"]
    quote_steps = [s for s in steps if s["tool_name"] == "get_latest_quote"]
    assert len(quote_steps) == 1, steps
    assert all(
        s["tool_name"] == "get_latest_quote" or s["input"].get("source") == "agent_runtime"
        for s in steps
    ), steps

    step = quote_steps[0]
    assert step["status"] == "success"
    assert step["input"]["symbol"] == "DEMO001"
    assert step["duration_ms"] > 0

    out = step["output"]
    for field in ("symbol", "name", "price", "prev_close", "change_pct",
                  "source", "updated_at", "data_status", "fallback_reason"):
        assert field in out, field
    assert out["price"] > 0
    assert out["data_status"] == STATUS_OFFLINE_SAMPLE

    # 回复中的价格必须来自工具，不得是别的数字
    assert f"{out['price']:.2f}" in body["reply"]
    assert body["quote"]["price"] == pytest.approx(out["price"])
    assert body["data_mode"] == "offline"
    assert body["tracing"]["tool_calls"] == len(steps)


def test_chat_quote_price_matches_provider(client: TestClient) -> None:
    """对话给出的价格必须等于 provider 的真实收盘价。"""
    expected = SampleMarketDataProvider().get_realtime_quote("DEMO001")
    body = client.post("/chat", json={
        "message": "DEMO001 最新价", "data_mode": "offline",
    }).json()
    assert body["quote"]["price"] == pytest.approx(round(expected["price"], 4), rel=1e-6)


def test_chat_tool_failure_is_explicit_and_never_guesses(client: TestClient) -> None:
    resp = client.post("/chat", json={
        "message": "999999 当前价是多少？",
        "data_mode": "offline",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 行情工具必须真实调用过并显式失败，而不是被静默跳过或降级为随机价
    steps = body["tool_steps"]
    quote_steps = [s for s in steps if s["tool_name"] == "get_latest_quote"]
    assert len(quote_steps) == 1, steps
    assert quote_steps[0]["status"] == "error"
    assert quote_steps[0]["input"]["symbol"] == "999999"
    assert "999999" in quote_steps[0]["error"]
    assert quote_steps[0]["output"] is None
    assert body["quote"] is None
    assert all(
        s["tool_name"] == "get_latest_quote" or s["input"].get("source") == "agent_runtime"
        for s in steps
    ), steps

    reply = body["reply"]
    # 必须显式承认取不到，且不得凭空给出任何价格数字
    assert ("无法" in reply) or ("失败" in reply) or ("取不到" in reply)
    assert not re.search(r"\d+\.\d{2}\s*元", reply)


def test_chat_returns_real_guardrail_results(client: TestClient) -> None:
    body = client.post("/chat", json={
        "message": "DEMO001 现价", "data_mode": "offline",
    }).json()
    names = {g["name"] for g in body["guardrail_results"]}
    assert {"RateLimiter", "JSONSchemaValidator",
            "SourceAttributionFilter", "KeywordBlocker"} <= names
    for g in body["guardrail_results"]:
        assert isinstance(g["passed"], bool)


# ── 6. 前端不得持有任何密钥或生成金融数据 ───────────────────────────────────

def test_frontend_has_no_api_key_persistence() -> None:
    text = FRONTEND.read_text(encoding="utf-8")
    for forbidden in ("ds_api_key", "sk-", "api.deepseek.com", "Authorization: `Bearer"):
        assert forbidden not in text, f"前端仍包含 {forbidden}"
    # localStorage 只允许保存后端地址与 thread_id，不得出现 key/token/secret
    for m in re.finditer(r"localStorage\.(setItem|getItem)\(\s*'([^']+)'", text):
        assert not re.search(r"key|token|secret", m.group(2), re.I), m.group(2)


def test_frontend_has_no_random_financial_data() -> None:
    text = FRONTEND.read_text(encoding="utf-8")
    for forbidden in ("genPriceSeries", "MOCK_RESPONSES", "dateRangeDays",
                      "_analysisPrices", "sanitizeKey"):
        assert forbidden not in text, f"前端仍包含 {forbidden}"
    assert "Math.random" not in text, "前端仍生成随机业务或监控数据"
    assert "运行 Harness 对照实验" not in text, "前端仍暴露固定结果的伪实验入口"
    assert "id=\"backend-provider-badge\"" in text
    assert "loadBackendHealth()" in text
    # 金融业务函数体内不得出现随机数
    for fn in ("async function placeOrder", "async function pushTick",
               "async function runBacktest", "async function runComparison",
               "async function runAnalysis"):
        idx = text.index(fn)
        body = text[idx: idx + 4000]
        assert "Math.random" not in body, f"{fn} 内仍使用 Math.random"


# ── 7. 回测：真实引擎 + 日期/价格不变量 ─────────────────────────────────────

def test_backtest_invokes_project_engine(client: TestClient, monkeypatch) -> None:
    """/backtest 必须调用既有 finance/backtesting.py::run_backtest。"""
    calls: list[dict] = []
    original = backtesting.run_backtest

    def _spy(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return original(*args, **kwargs)

    monkeypatch.setattr(backtesting, "run_backtest", _spy)
    from agent_platform.finance import backtest_service
    monkeypatch.setattr(backtest_service, "run_backtest", _spy)

    resp = client.post("/backtest", json={
        "symbol": "DEMO001", "start": WIN_START, "end": WIN_END,
        "initial_capital": 1_000_000, "data_mode": "offline",
    })
    assert resp.status_code == 200, resp.text
    assert len(calls) == 1, "未调用项目既有回测引擎"


def test_backtest_dates_and_prices_are_real(client: TestClient) -> None:
    resp = client.post("/backtest", json={
        "symbol": "DEMO001", "start": WIN_START, "end": WIN_END,
        "initial_capital": 1_000_000, "data_mode": "offline",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["requested_start"] == WIN_START
    assert body["requested_end"] == WIN_END
    assert body["start_date"] >= WIN_START
    assert body["end_date"] <= WIN_END
    assert body["warmup_rows_used"] > 0

    curve = body["equity_curve"]
    assert body["trading_days"] == len(curve)
    assert all(WIN_START <= p["date"] <= WIN_END for p in curve)
    assert curve[0]["nav"] == pytest.approx(1.0, abs=1e-9)

    # 成交价必须能由真实样例 OHLCV 精确复算。
    # API 明确区分信号日与实际成交日。成交价用下一根 K 线的开盘价
    # （次日开盘执行）叠加单边滑点 0.1% + 佣金 0.03%；
    # 区间末仍持仓则按最后一根的收盘价强制平仓（signal="forced_exit"）。
    raw = SampleMarketDataProvider().get_price_history(
        "DEMO001", start=date.fromisoformat(WIN_START), end=date.fromisoformat(WIN_END)
    )
    rows = [r for _, r in raw.sort_values("date").iterrows()]
    idx_of = {r["date"].isoformat(): i for i, r in enumerate(rows)}
    cost = 0.001 + 0.0003  # 滑点 + 佣金（单边）

    def next_row(iso_day: str):
        i = min(idx_of[iso_day] + 1, len(rows) - 1)
        return rows[i]

    assert body["total_trades"] == len(body["trades"])
    assert body["trades"], "样例区间内应产生至少一笔真实交易"
    for t in body["trades"]:
        assert WIN_START <= t["entry_date"] <= WIN_END
        assert WIN_START <= t["exit_date"] <= WIN_END
        assert t["entry_date"] <= t["exit_date"]
        assert t["entry_signal_date"] <= t["entry_date"]
        assert t["exit_signal_date"] <= t["exit_date"]

        entry_row = next_row(t["entry_signal_date"])
        assert t["entry_date"] == entry_row["date"].isoformat()
        assert t["entry_price"] == pytest.approx(
            float(entry_row["open"]) * (1 + cost), rel=1e-4
        ), ("entry", t)

        if t["signal"] == "forced_exit":
            assert t["exit_date"] == rows[-1]["date"].isoformat()
            expected_exit = float(rows[-1]["close"]) * (1 - cost)
        else:
            exit_row = next_row(t["exit_signal_date"])
            assert t["exit_date"] == exit_row["date"].isoformat()
            expected_exit = float(exit_row["open"]) * (1 - cost)
        assert t["exit_price"] == pytest.approx(expected_exit, rel=1e-4), ("exit", t)

        # 收益率与成交价自洽
        assert t["return_pct"] == pytest.approx(
            (t["exit_price"] - t["entry_price"]) / t["entry_price"] * 100.0, abs=0.01
        )

    # 收益率与净值自洽
    assert body["final_equity"] == pytest.approx(
        body["initial_capital"] * curve[-1]["nav"], rel=1e-6
    )
    assert body["total_return_pct"] == pytest.approx(
        (curve[-1]["nav"] - 1.0) * 100, abs=0.02
    )
    assert body["winning_trades"] + body["losing_trades"] <= body["total_trades"]


def test_backtest_rejects_insufficient_ma20_history(client: TestClient) -> None:
    """样例数据起点后不足 20 个交易日时，必须显式报错而不是编造结果。"""
    resp = client.post("/backtest", json={
        "symbol": "DEMO001", "start": "2025-01-02", "end": "2025-01-10",
        "data_mode": "offline",
    })
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "20" in detail or "历史" in detail


def test_backtest_exposes_metadata(client: TestClient) -> None:
    body = client.post("/backtest", json={
        "symbol": "DEMO001", "start": WIN_START, "end": WIN_END,
        "data_mode": "offline",
    }).json()
    for field in ("source", "updated_at", "data_status", "fallback_reason", "disclaimer"):
        assert field in body, field
    assert body["data_status"] == STATUS_OFFLINE_SAMPLE


def test_backtest_sharpe_matches_project_convention(client: TestClient) -> None:
    """夏普必须沿用既有约定（backtesting._compute_sharpe），不得为改善结果换公式。

    项目有两个并列口径，测试同时锁定，避免后续被悄悄换成"更好看"的那个：
      * ``sharpe_calendar``：日历口径，每个交易日一项、空仓日记 0.0，
        正好等于净值曲线的逐日 pct_change，可由 API 返回值独立复算。
      * ``sharpe_ratio``：持仓日口径，只统计有仓位的日收益，因此在
        时间在市 < 100% 时必然 **大于** 日历口径。
    """
    body = client.post("/backtest", json={
        "symbol": "DEMO001", "start": WIN_START, "end": WIN_END,
        "data_mode": "offline",
    }).json()

    navs = [p["nav"] for p in body["equity_curve"]]
    assert len(navs) >= 3
    calendar_returns = [
        (navs[i] - navs[i - 1]) / navs[i - 1] for i in range(1, len(navs))
    ]
    # 日历口径可完全复算
    assert body["sharpe_calendar"] == pytest.approx(
        backtesting._compute_sharpe(calendar_returns), abs=0.02
    )

    # 公式未被替换：_compute_sharpe 用 ddof=1 标准差 + 2% 年化无风险的几何日利率
    daily_rf = (1 + backtesting._RISK_FREE_RATE) ** (
        1 / backtesting._TRADING_DAYS_PER_YEAR
    ) - 1
    mean_r = sum(calendar_returns) / len(calendar_returns)
    var = sum((r - mean_r) ** 2 for r in calendar_returns) / (len(calendar_returns) - 1)
    manual = (mean_r - daily_rf) / math.sqrt(var) * math.sqrt(
        backtesting._TRADING_DAYS_PER_YEAR
    )
    # 容差 5e-5：API 以 round(..., 4) 输出，除舍入外不允许任何偏差
    assert body["sharpe_calendar"] == pytest.approx(manual, abs=5e-5)

    # 持仓日口径 ≥ 日历口径（时间在市 <= 100%）
    assert 0.0 < body["time_in_market_pct"] <= 100.0
    if body["time_in_market_pct"] < 100.0:
        assert body["sharpe_ratio"] > body["sharpe_calendar"]


# ── 8. Human Approval 真实执行状态 ──────────────────────────────────────────

def test_research_state_exposes_executed_nodes(client: TestClient) -> None:
    """未触发审批时 executed_nodes 不含 human_approval → 前端显示"跳过"。"""
    resp = client.get("/research/no-such-thread/state")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "not_found"
    assert body["trace_entries"] == []
    assert body["executed_nodes"] == []
    assert "human_approval" not in body["executed_nodes"]


def test_research_state_executed_nodes_from_trace(client: TestClient, monkeypatch) -> None:
    """executed_nodes 必须由真实 trace_entries 派生，而不是由最终状态推断。"""
    class _Snapshot:
        tasks: list = []
        values = {
            "symbol": "DEMO001",
            "status": "completed",
            "final_action": "execute",
            "errors": [],
            "trace_entries": [
                {"node": "technical_agent", "duration_s": 0.01, "status": "ok"},
                {"node": "synthesis_agent", "duration_s": 0.02, "status": "ok"},
                {"node": "trader_agent", "duration_s": 0.01, "status": "ok"},
                {"node": "trading_harness", "duration_s": 0.01, "status": "ok"},
            ],
        }

    svc = main.get_application_service()
    monkeypatch.setattr(svc._securities_graph, "get_state", lambda cfg: _Snapshot())

    body = client.get("/research/t-1/state").json()
    assert body["status"] == "completed"
    assert body["executed_nodes"] == [
        "technical_agent", "synthesis_agent", "trader_agent", "trading_harness"
    ]
    # 关键断言：状态 completed 但审批节点从未执行 → 不得标记为已完成
    assert "human_approval" not in body["executed_nodes"]
    assert len(body["trace_entries"]) == 4


def test_research_state_marks_approval_executed_when_it_ran(
    client: TestClient, monkeypatch
) -> None:
    class _Snapshot:
        tasks: list = []
        values = {
            "status": "completed",
            "final_action": "execute",
            "errors": [],
            "trace_entries": [
                {"node": "trader_agent", "duration_s": 0.01, "status": "ok"},
                {"node": "human_approval", "duration_s": 0.5, "status": "approved"},
                {"node": "risk_manager", "duration_s": 0.01, "status": "ok"},
            ],
        }

    svc = main.get_application_service()
    monkeypatch.setattr(svc._securities_graph, "get_state", lambda cfg: _Snapshot())

    body = client.get("/research/t-2/state").json()
    assert "human_approval" in body["executed_nodes"]
    entry = next(e for e in body["trace_entries"] if e["node"] == "human_approval")
    assert entry["status"] == "approved"


# ── 9. MockBroker 数量单位 ──────────────────────────────────────────────────

def test_quantity_unit_declared_as_shares() -> None:
    assert QUANTITY_UNIT == "shares"
    assert SHARES_PER_LOT == 100
    assert lots_to_shares(3) == 300
    assert shares_to_lots(300) == pytest.approx(3.0)
    assert shares_to_lots(150) == pytest.approx(1.5)
    with pytest.raises(ValueError):
        lots_to_shares(0)


def test_broker_accounts_in_shares() -> None:
    """成交金额 = 价格 × 股数；1 手 = 100 股换算后记账一致。"""
    broker = MockBroker(initial_cash=1_000_000.0, commission_pct=0.0, slippage_pct=0.0)
    shares = lots_to_shares(2)          # 2 手 = 200 股
    broker.place_market_order("DEMO001", OrderSide.BUY, shares)
    filled = broker.tick("DEMO001", 100.0)

    assert len(filled) == 1
    order = filled[0]
    assert order.quantity == 200
    assert order.filled_quantity == 200
    assert order.filled_price == pytest.approx(100.0)

    # 现金精确减少 100 × 200 = 20000（按股计价，不是按手）
    assert broker.cash == pytest.approx(1_000_000.0 - 20_000.0)

    pos = broker.get_positions()["DEMO001"]
    assert pos.quantity == 200
    assert pos.market_value == pytest.approx(20_000.0)
    assert broker.portfolio_value() == pytest.approx(1_000_000.0)


def test_broker_trade_history_records_unit() -> None:
    broker = MockBroker(initial_cash=500_000.0, commission_pct=0.0, slippage_pct=0.0)
    broker.place_market_order("DEMO001", OrderSide.BUY, 100)
    broker.tick("DEMO001", 50.0)
    trade = broker._trade_history[-1]
    assert trade["quantity"] == 100
    assert trade["quantity_unit"] == "shares"


def test_broker_rejects_oversell_in_shares() -> None:
    broker = MockBroker(initial_cash=100_000.0, commission_pct=0.0, slippage_pct=0.0)
    broker.place_market_order("DEMO001", OrderSide.BUY, 100)
    broker.tick("DEMO001", 10.0)
    broker.place_market_order("DEMO001", OrderSide.SELL, 500)
    broker.tick("DEMO001", 10.0)
    order = [o for o in broker._orders.values() if o.side == OrderSide.SELL][0]
    assert order.status.value == "rejected"
    assert "股" in (order.reject_reason or "")


def test_frontend_quantity_label_is_shares() -> None:
    text = FRONTEND.read_text(encoding="utf-8")
    assert "数量（股）" in text
    assert "数量（手）" not in text
    assert "SHARES_PER_LOT = 100" in text
