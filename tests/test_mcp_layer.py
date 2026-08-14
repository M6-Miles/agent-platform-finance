"""
MCP 工具层专项测试
==================
对应说明书要求：
1. AkShare/Tushare 统一封装为可被主业务调用的 MCP 工具层；
2. 所有工具结果必须带 source、updated_at/timestamp 和明确错误字段；
3. 离线模式必须完全禁止外网；
4. 不得留下未被任何主链路调用的工具文件。

本文件的关键断言是 **离线硬阻断**：注册表在 `requires_network=True` 的工具
函数体执行之前就返回失败信封。测试用「哨兵函数」证明这一点——若函数体被执行，
哨兵会置位，断言即失败。这比「检查没有发出请求」更强，因为它不依赖网络探测。
"""
from __future__ import annotations

import pytest

from agent_platform.mcp.envelope import (
    REQUIRED_ENVELOPE_KEYS,
    err_envelope,
    is_ok,
    ok_envelope,
    utc_now_iso,
    validate_envelope,
)
from agent_platform.mcp.registry import (
    MCPToolNotFoundError,
    MCPToolRegistry,
    build_default_registry,
    get_registry,
)


# ═══════════════════════════════════════════════════════════════
#   一、信封结构
# ═══════════════════════════════════════════════════════════════

class TestEnvelope:
    def test_ok_envelope_has_all_required_keys(self):
        env = ok_envelope(tool="t", source="akshare/x", data={"a": 1})
        assert REQUIRED_ENVELOPE_KEYS <= set(env)

    def test_ok_envelope_is_ok_and_error_none(self):
        env = ok_envelope(tool="t", source="akshare/x", data=[1, 2])
        assert env["ok"] is True
        assert env["error"] is None
        assert env["error_type"] is None
        assert env["data"] == [1, 2]

    def test_ok_envelope_timestamp_mirrors_updated_at(self):
        """说明书写的是 updated_at/timestamp，两种取法都必须可用。"""
        env = ok_envelope(tool="t", source="akshare/x", data=None)
        assert env["updated_at"] == env["timestamp"]
        assert env["updated_at"].endswith("Z")

    def test_ok_envelope_rejects_empty_source(self):
        """缺失溯源属于开发期错误，必须立刻抛错而不是流到输出。"""
        with pytest.raises(ValueError, match="未提供 source"):
            ok_envelope(tool="t", source="", data={"a": 1})

    def test_err_envelope_data_is_always_none(self):
        """失败不得返回数据——这是「不得伪造行情」的结构性保证。"""
        env = err_envelope(
            tool="t", source="akshare", error="boom", error_type="ValueError"
        )
        assert env["ok"] is False
        assert env["data"] is None
        assert env["error"] == "boom"
        assert env["error_type"] == "ValueError"

    def test_params_are_redacted(self):
        """token/密钥不得回显到信封或日志。"""
        env = ok_envelope(
            tool="t",
            source="tushare",
            data=None,
            params={"ts_code": "600519.SH", "token": "SECRET123"},
        )
        assert env["params"]["ts_code"] == "600519.SH"
        assert env["params"]["token"] == "***"
        assert "SECRET123" not in str(env)

    def test_is_ok_rejects_non_dict(self):
        assert is_ok(None) is False
        assert is_ok("ok") is False
        assert is_ok({"ok": "true"}) is False

    def test_validate_envelope_accepts_valid(self):
        assert validate_envelope(ok_envelope(tool="t", source="s", data=1)) == []
        assert validate_envelope(
            err_envelope(tool="t", source="s", error="e", error_type="E")
        ) == []

    def test_validate_envelope_flags_fabricated_data_on_failure(self):
        bad = err_envelope(tool="t", source="s", error="e", error_type="E")
        bad["data"] = {"price": 100.0}          # 人为伪造
        problems = validate_envelope(bad)
        assert any("失败不得返回数据" in p for p in problems)

    def test_validate_envelope_flags_missing_keys(self):
        problems = validate_envelope({"ok": True})
        assert any("缺少字段" in p for p in problems)

    def test_utc_now_iso_format(self):
        ts = utc_now_iso()
        assert ts.endswith("Z")
        assert "T" in ts


# ═══════════════════════════════════════════════════════════════
#   二、注册表：注册与查询
# ═══════════════════════════════════════════════════════════════

def _dummy_tool(**kwargs):
    return ok_envelope(tool="dummy", source="test/dummy", data={"got": kwargs})


class TestRegistryBasics:
    def test_register_and_call(self):
        reg = MCPToolRegistry()
        reg.register(
            "dummy", _dummy_tool,
            description="测试工具", requires_network=False,
            provider="test", category="quote",
        )
        assert reg.has("dummy")
        env = reg.call("dummy", symbol="600519")
        assert env["ok"] is True
        assert env["data"]["got"] == {"symbol": "600519"}

    def test_duplicate_registration_rejected(self):
        reg = MCPToolRegistry()
        reg.register(
            "dummy", _dummy_tool, description="d",
            requires_network=False, provider="test", category="quote",
        )
        with pytest.raises(ValueError, match="重复注册"):
            reg.register(
                "dummy", _dummy_tool, description="d",
                requires_network=False, provider="test", category="quote",
            )

    def test_unknown_tool_raises(self):
        """工具名写错是编码错误，必须暴露而不是静默降级。"""
        reg = MCPToolRegistry()
        with pytest.raises(MCPToolNotFoundError):
            reg.call("no_such_tool")

    def test_tool_exception_becomes_error_envelope(self):
        """数据源抖动不得让主链路崩，但也不得伪造数据。"""
        def boom(**kwargs):
            raise RuntimeError("数据源炸了")

        reg = MCPToolRegistry()
        reg.register(
            "boom", boom, description="d",
            requires_network=False, provider="test", category="quote",
        )
        env = reg.call("boom")
        assert env["ok"] is False
        assert env["data"] is None
        assert env["error_type"] == "RuntimeError"
        assert "数据源炸了" in env["error"]
        assert validate_envelope(env) == []


# ═══════════════════════════════════════════════════════════════
#   三、离线硬阻断（说明书硬要求）
# ═══════════════════════════════════════════════════════════════

class TestOfflineHardBlock:
    def test_offline_blocks_before_function_body_runs(self):
        """
        哨兵测试：离线模式下网络工具的函数体**一次也不能执行**。
        若被执行，sentinel 会变成 True，断言失败。
        """
        sentinel = {"executed": False}

        def net_tool(**kwargs):
            sentinel["executed"] = True          # 只要走到这里就说明阻断失效
            return ok_envelope(tool="net", source="akshare/x", data={"p": 1})

        reg = MCPToolRegistry(offline=True)
        reg.register(
            "net_tool", net_tool, description="需要联网",
            requires_network=True, provider="akshare", category="quote",
        )
        env = reg.call("net_tool", symbol="600519")

        assert sentinel["executed"] is False, "离线模式下网络工具函数体被执行了"
        assert env["ok"] is False
        assert env["error_type"] == "OfflineModeBlocked"
        assert env["data"] is None
        assert "离线模式禁止网络调用" in env["error"]

    def test_offline_allows_non_network_tools(self):
        reg = MCPToolRegistry(offline=True)
        reg.register(
            "local", _dummy_tool, description="本地",
            requires_network=False, provider="offline", category="quote",
        )
        env = reg.call("local")
        assert env["ok"] is True

    def test_online_registry_does_not_block(self):
        sentinel = {"executed": False}

        def net_tool(**kwargs):
            sentinel["executed"] = True
            return ok_envelope(tool="net", source="akshare/x", data={"p": 1})

        reg = MCPToolRegistry(offline=False)
        reg.register(
            "net_tool", net_tool, description="需要联网",
            requires_network=True, provider="akshare", category="quote",
        )
        env = reg.call("net_tool")
        assert sentinel["executed"] is True
        assert env["ok"] is True

    def test_all_network_tools_blocked_in_default_offline_registry(self):
        """
        离线默认注册表里，**每一个** requires_network 工具都必须被阻断。
        这是「离线 20 股端到端零网络调用」的注册表层证据。
        """
        reg = build_default_registry(offline=True)
        net_tools = [t["name"] for t in reg.list_tools() if t["requires_network"]]
        assert net_tools, "默认注册表里应存在网络工具"

        for name in net_tools:
            env = reg.call(name)
            assert env["ok"] is False, f"{name} 未被离线阻断"
            assert env["error_type"] == "OfflineModeBlocked", f"{name} 阻断类型错误"
            assert env["data"] is None, f"{name} 在离线模式返回了数据"

    def test_offline_block_is_audited(self):
        reg = build_default_registry(offline=True)
        reg.call("get_price_history", symbol="600519")
        stats = reg.stats()
        assert stats["blocked_offline"] == 1
        assert stats["failed"] == 1
        assert stats["offline"] is True


# ═══════════════════════════════════════════════════════════════
#   四、默认注册表覆盖面（说明书列举的数据类别）
# ═══════════════════════════════════════════════════════════════

class TestDefaultRegistryCoverage:
    def test_registry_builds(self):
        reg = build_default_registry(offline=True)
        assert len(reg.tool_names()) >= 20

    @pytest.mark.parametrize("tool_name", [
        "get_price_history",        # 历史行情 / 日线周线
        "get_minute_bars",          # 分钟线
        "get_realtime_quote",       # 实时行情
        "get_fund_flow",            # 个股资金流向
        "get_sector_fund_flow",     # 行业资金流向
        "get_financial_statement",  # 三大报表（AkShare）
        "get_income_statement",     # 利润表（Tushare）
        "get_balance_sheet",        # 资产负债表
        "get_cash_flow",            # 现金流量表
        "get_valuation_metrics",    # PE/PB/市值
        "get_financial_indicator",  # ROE/资产负债率
        "get_daily_basic",          # PE/PB/PS（Tushare）
        "get_index_daily",          # 指数数据
        "get_industry_list",        # 行业列表
        "get_industry_spot",        # 行业行情
        "get_stock_industry",       # 个股所属行业
        # 信息类工具（任务4补充）
        "get_financial_news",           # 财经新闻
        "get_stock_announcements",      # 公司公告
        "get_research_report_summary",  # 研报摘要
        "get_macro_policy",             # 宏观政策
        "get_interest_rates",           # 利率数据
    ])
    def test_required_tool_registered(self, tool_name):
        """说明书列举的数据类别必须都有对应工具。"""
        reg = build_default_registry(offline=True)
        assert reg.has(tool_name), f"缺少 MCP 工具: {tool_name}"

    @pytest.mark.parametrize("category", [
        "history", "quote", "fundflow", "financials",
        "valuation", "index", "industry",
        # 信息类类别（任务4补充）
        "news", "announcements", "research", "macro",
    ])
    def test_category_covered(self, category):
        reg = build_default_registry(offline=True)
        cats = {t["category"] for t in reg.list_tools()}
        assert category in cats, f"缺少数据类别: {category}"

    def test_every_tool_declares_provider_and_category(self):
        reg = build_default_registry(offline=True)
        for spec in reg.list_tools():
            assert spec["provider"], f"{spec['name']} 缺 provider"
            assert spec["category"], f"{spec['name']} 缺 category"
            assert spec["description"], f"{spec['name']} 缺 description"

    def test_offline_tools_do_not_require_network(self):
        reg = build_default_registry(offline=True)
        for spec in reg.list_tools():
            if spec["provider"] == "offline":
                assert spec["requires_network"] is False


# ═══════════════════════════════════════════════════════════════
#   五、离线工具真实产出
# ═══════════════════════════════════════════════════════════════

class TestOfflineTools:
    def test_offline_fundamental_returns_full_metrics(self):
        reg = build_default_registry(offline=True)
        env = reg.call("get_offline_fundamental", symbol="DEMO001")
        assert env["ok"] is True
        d = env["data"]
        # 说明书要求的现有指标
        for key in ("pe_ttm", "pb", "roe_pct", "debt_to_asset_pct",
                    "total_market_value_cny"):
            assert d.get(key) is not None, f"离线基本面缺字段 {key}"

    def test_offline_fundamental_marks_sample(self):
        """不得把样例数据标成实时数据。"""
        reg = build_default_registry(offline=True)
        env = reg.call("get_offline_fundamental", symbol="DEMO001")
        assert env["data"]["is_sample"] is True
        assert "offline_sample" in env["source"]

    def test_offline_fundamental_deterministic(self):
        reg = build_default_registry(offline=True)
        a = reg.call("get_offline_fundamental", symbol="DEMO002")["data"]
        b = reg.call("get_offline_fundamental", symbol="DEMO002")["data"]
        assert a == b

    def test_offline_industry_and_regime(self):
        reg = build_default_registry(offline=True)
        ind = reg.call("get_offline_industry", symbol="DEMO001")
        reg_env = reg.call("get_offline_market_regime", index_code="sh000001")
        assert ind["ok"] is True
        assert ind["data"]["industry_name"]
        assert reg_env["ok"] is True

    def test_all_offline_tools_return_valid_envelopes(self):
        reg = build_default_registry(offline=True)
        for spec in reg.list_tools():
            if spec["requires_network"]:
                continue
            env = reg.call(spec["name"])
            assert validate_envelope(env) == [], f"{spec['name']} 信封不合规: {env}"


# ═══════════════════════════════════════════════════════════════
#   六、审计与共享实例
# ═══════════════════════════════════════════════════════════════

class TestAudit:
    def test_stats_empty(self):
        reg = MCPToolRegistry()
        s = reg.stats()
        assert s["total"] == 0
        assert s["success_rate"] == 0.0

    def test_stats_counts_and_by_tool(self):
        reg = build_default_registry(offline=True)
        reg.call("get_offline_fundamental", symbol="DEMO001")
        reg.call("get_offline_fundamental", symbol="DEMO002")
        reg.call("get_price_history", symbol="600519")     # 被阻断
        s = reg.stats()
        assert s["total"] == 3
        assert s["ok"] == 2
        assert s["failed"] == 1
        assert s["by_tool"]["get_offline_fundamental"] == 2

    def test_reset_log(self):
        reg = build_default_registry(offline=True)
        reg.call("get_offline_fundamental", symbol="DEMO001")
        reg.reset_log()
        assert reg.stats()["total"] == 0

    def test_call_record_serializable(self):
        reg = build_default_registry(offline=True)
        reg.call("get_offline_fundamental", symbol="DEMO001")
        rec = reg.call_log[0].to_dict()
        assert rec["tool"] == "get_offline_fundamental"
        assert rec["ok"] is True
        assert rec["blocked_offline"] is False

    def test_get_registry_caches_per_mode(self):
        a = get_registry(offline=True)
        b = get_registry(offline=True)
        c = get_registry(offline=False)
        assert a is b
        assert a is not c
        assert a.offline is True
        assert c.offline is False


# ═══════════════════════════════════════════════════════════════
#   七、主链路确实经过 MCP（反「孤立工具文件」）
# ═══════════════════════════════════════════════════════════════

class TestMainChainUsesMCP:
    def test_fundamental_agent_offline_goes_through_mcp(self):
        """
        基本面 Agent 离线取数必须经由 MCP 工具，而不是直读样例模块。
        断言 source 里带 MCP 工具名。
        """
        from agent_platform.finance.fundamental_agent import analyze_fundamental

        result = analyze_fundamental("DEMO001", force_offline=True)
        assert "MCP:" in result.source, f"未经过 MCP 层: {result.source}"
        assert result.data_status == "offline_sample"
        assert result.fallback_reason is None

    def test_fundamental_agent_offline_has_debt_ratio_and_dcf(self):
        from agent_platform.finance.fundamental_agent import analyze_fundamental

        result = analyze_fundamental("DEMO001", force_offline=True)
        assert result.debt_to_asset_pct is not None
        assert result.dcf is not None
        assert result.dcf["applicable"] is True

    def test_root_mcp_shims_delegate_to_package(self):
        """
        根目录 MCP/*.py 必须委托到 src/agent_platform/mcp，
        不能是与主链路无关的孤立实现。

        注意：本测试只证明 shim **可被导入且文本里提到统一包**，
        不证明每个 shim 函数内写的工具名是真的。后者由
        :class:`TestRootShimsActuallyRoute` 逐个实调用证明。
        """
        import importlib.util
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "MCP"
        assert root.is_dir()

        for fname in ("akshare_tools.py", "tushare_tools.py"):
            path = root / fname
            assert path.is_file(), f"缺少 {fname}"
            text = path.read_text(encoding="utf-8")
            assert "agent_platform.mcp" in text, f"{fname} 未委托到统一 MCP 包"
            # 确认可被真实导入（语法与依赖正确）
            spec = importlib.util.spec_from_file_location(f"_mcp_shim_{fname}", path)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)


# ═══════════════════════════════════════════════════════════════
#   八、根目录 shim 的每个函数都真的能路由到已注册工具
# ═══════════════════════════════════════════════════════════════

class TestRootShimsActuallyRoute:
    """
    上面 `test_root_mcp_shims_delegate_to_package` 只证明 shim **能被导入**，
    不证明 shim 内部写的工具名是对的 —— 把 "get_fund_flow" 写成 "get_fundflow"
    也照样能导入通过。那样的 shim 是"看起来有能力，一调就炸"。

    本类逐个**真正调用**全部 13 个 shim 函数（offline=True），断言：

    * 不抛 :class:`MCPToolNotFoundError` —— 证明工具名确实已注册；
    * 返回信封的 ``tool`` 等于预期工具名 —— 证明路由到了正确的工具；
    * ``error_type == "OfflineModeBlocked"`` —— 证明走的是注册表级离线硬阻断，
      函数体未执行、未发出任何网络请求。

    离线模式在这里同时充当"零网络"保证和"工具名拼写"探针：名字错了抛异常，
    名字对了必然被阻断。整个用例不需要联网。
    """

    _AK_CASES = (
        ("mcp_get_price_history",   {"symbol": "600519"},        "get_price_history"),
        ("mcp_get_realtime_quote",  {"symbol": "600519"},        "get_realtime_quote"),
        ("mcp_get_index_daily",     {},                          "get_index_daily"),
        ("mcp_get_industry_list",   {},                          "get_industry_list"),
        ("mcp_get_minute_bars",     {"symbol": "600519"},        "get_minute_bars"),
        ("mcp_get_fund_flow",       {"symbol": "600519"},        "get_fund_flow"),
        ("mcp_get_sector_fund_flow", {},                         "get_sector_fund_flow"),
    )

    _TS_CASES = (
        ("mcp_get_income_statement", {"ts_code": "600519.SH"},   "get_income_statement"),
        ("mcp_get_balance_sheet",    {"ts_code": "600519.SH"},   "get_balance_sheet"),
        ("mcp_get_cash_flow",        {"ts_code": "600519.SH"},   "get_cash_flow"),
        ("mcp_get_daily_basic",      {"ts_code": "600519.SH"},   "get_daily_basic"),
        ("mcp_get_fina_indicator",   {"ts_code": "600519.SH"},   "get_fina_indicator"),
        ("mcp_get_index_daily_ts",   {},                         "get_index_daily_ts"),
    )

    @staticmethod
    def _load(fname: str):
        """按文件路径加载根目录 shim（它不在 src 包树内，不能直接 import）。"""
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / "MCP" / fname
        assert path.is_file(), f"缺少 {fname}"
        spec = importlib.util.spec_from_file_location(f"_mcp_shim_call_{fname}", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @pytest.mark.parametrize(("func_name", "kwargs", "expected_tool"), _AK_CASES)
    def test_akshare_shim_routes_to_registered_tool(self, func_name, kwargs, expected_tool):
        module = self._load("akshare_tools.py")
        func = getattr(module, func_name, None)
        assert func is not None, f"akshare shim 缺少函数 {func_name}"

        # 工具名拼错会在这里抛 MCPToolNotFoundError
        env = func(offline=True, **kwargs)

        assert env["tool"] == expected_tool, f"{func_name} 路由到了 {env['tool']}"
        assert env["ok"] is False
        assert env["data"] is None
        assert env["error_type"] == "OfflineModeBlocked", (
            f"{func_name} 未被离线阻断，可能真的发出了网络请求：{env['error_type']}"
        )

    @pytest.mark.parametrize(("func_name", "kwargs", "expected_tool"), _TS_CASES)
    def test_tushare_shim_routes_to_registered_tool(self, func_name, kwargs, expected_tool):
        module = self._load("tushare_tools.py")
        func = getattr(module, func_name, None)
        assert func is not None, f"tushare shim 缺少函数 {func_name}"

        env = func(offline=True, **kwargs)

        assert env["tool"] == expected_tool, f"{func_name} 路由到了 {env['tool']}"
        assert env["ok"] is False
        assert env["data"] is None
        assert env["error_type"] == "OfflineModeBlocked", (
            f"{func_name} 未被离线阻断，可能真的发出了网络请求：{env['error_type']}"
        )

    def test_unknown_tool_name_would_raise(self):
        """
        反证：本类的断言之所以有效，是因为工具名写错**确实会抛错**。
        若注册表对未知工具静默返回失败信封，上面的用例就成了空转。
        """
        from agent_platform.mcp.registry import get_registry

        with pytest.raises(MCPToolNotFoundError):
            get_registry(offline=True).call("get_fundflow_typo", symbol="600519")

    def test_shim_list_available_tools_is_not_empty(self):
        """shim 自报的能力集必须非空，且条目带工具名与网络需求标记。"""
        module = self._load("akshare_tools.py")
        tools = module.list_available_tools(offline=True)
        assert isinstance(tools, list) and tools
        names = {t["name"] for t in tools}
        for expected in ("get_price_history", "get_realtime_quote", "get_fund_flow"):
            assert expected in names, f"能力集缺少 {expected}"


# ═══════════════════════════════════════════════════════════════
#   九、信息工具（新闻/公告/研报/宏观/利率）离线阻断与语义（任务4）
# ═══════════════════════════════════════════════════════════════

_INFO_TOOLS = (
    "get_financial_news",
    "get_stock_announcements",
    "get_research_report_summary",
    "get_macro_policy",
    "get_interest_rates",
)


class TestInfoToolsCoverage:
    """info_tools.py 的注册与离线语义测试。"""

    @pytest.mark.parametrize("tool_name", _INFO_TOOLS)
    def test_info_tool_registered_in_default_registry(self, tool_name: str) -> None:
        """每个信息工具必须出现在默认注册表中。"""
        reg = build_default_registry(offline=True)
        assert reg.has(tool_name), f"默认注册表缺少信息工具 {tool_name}"

    @pytest.mark.parametrize("tool_name", _INFO_TOOLS)
    def test_info_tool_blocked_in_offline_mode(self, tool_name: str) -> None:
        """离线模式下所有信息工具必须被阻断（函数体不执行）。"""
        reg = build_default_registry(offline=True)
        env = reg.call(tool_name, symbol="600519")
        assert env["ok"] is False
        assert env["error_type"] == "OfflineModeBlocked"
        assert env["data"] is None, f"{tool_name} 离线阻断时不得返回数据"

    @pytest.mark.parametrize("tool_name", _INFO_TOOLS)
    def test_info_tool_declares_network_required(self, tool_name: str) -> None:
        """信息工具必须声明 requires_network=True，以保证离线阻断生效。"""
        reg = build_default_registry(offline=False)
        spec = next(t for t in reg.list_tools() if t["name"] == tool_name)
        assert spec["requires_network"] is True, (
            f"{tool_name} 未声明 requires_network=True，离线阻断不会生效"
        )

    def test_info_tools_cover_all_required_categories(self) -> None:
        """信息工具的 category 集合必须包含 news/announcements/research/macro。"""
        reg = build_default_registry(offline=True)
        cats = {t["category"] for t in reg.list_tools() if t["name"] in _INFO_TOOLS}
        for expected in ("news", "announcements", "research", "macro"):
            assert expected in cats, f"信息工具未覆盖类别 {expected!r}"

    def test_offline_block_never_fabricates_data(self) -> None:
        """离线阻断返回的信封 data 字段必须为 None，不得包含伪造内容。"""
        reg = build_default_registry(offline=True)
        for name in _INFO_TOOLS:
            env = reg.call(name, symbol="600519")
            assert env.get("data") is None, (
                f"{name} 离线信封 data 不为 None，可能伪造了内容: {env.get('data')}"
            )
