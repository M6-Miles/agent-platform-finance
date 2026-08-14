"""
行业资金流向 (get_sector_fund_flow) 版本兼容性测试（任务2）
==========================================================
测试三阶段渐进试探策略：
  1. 旧版参数 sector_type="行业资金流向" 成功 → 不重试
  2. 旧版参数抛 KeyError → 退到无参再试 → 成功
  3. 列名版本差异（行业/板块名称）→ 归一化为 "名称"
  4. 上游返回空 DataFrame → error_type="EmptyResult"
  5. 全部3次尝试失败 → error_type="UpstreamCompatibilityError"，不抛异常

所有测试通过 monkeypatch 拦截 akshare，不发出真实网络请求。
"""
from __future__ import annotations

import pandas as pd
import pytest

from agent_platform.mcp.akshare_tools import get_sector_fund_flow


# ─────────────────────────────────────────────────────────────────────────────
# 辅助：构造带指定列名的假 DataFrame
# ─────────────────────────────────────────────────────────────────────────────

def _make_df(name_col: str = "名称") -> pd.DataFrame:
    return pd.DataFrame({
        name_col: ["电子", "银行", "医药"],
        "今日涨跌幅": [1.2, -0.3, 0.8],
        "今日主力净流入净额": [1e8, -5e7, 2e8],
    })


# ─────────────────────────────────────────────────────────────────────────────
# 测试类
# ─────────────────────────────────────────────────────────────────────────────

class TestSectorFundFlowCompat:

    def test_old_api_with_sector_type_succeeds_on_first_try(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """旧版 sector_type='行业资金流向' 可用时一次就成功，不触发重试。"""
        call_count = {"n": 0}

        def fake_rank(**kwargs: object) -> pd.DataFrame:
            call_count["n"] += 1
            if kwargs.get("sector_type") == "行业资金流向":
                return _make_df("名称")
            raise KeyError(f"unexpected sector_type: {kwargs.get('sector_type')!r}")

        import akshare as ak
        monkeypatch.setattr(ak, "stock_sector_fund_flow_rank", fake_rank)

        env = get_sector_fund_flow(indicator="今日")
        assert env["ok"] is True
        assert call_count["n"] == 1, "旧版API成功时不应有重试"
        assert env["data"]["records"], "结果不应为空"

    def test_keyerror_on_sector_type_falls_back_to_no_param(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """旧版参数 sector_type 抛 KeyError 时退到无参调用并成功。"""
        attempts: list[str] = []

        def fake_rank(**kwargs: object) -> pd.DataFrame:
            if "sector_type" in kwargs:
                attempts.append("with_sector_type")
                raise KeyError("sector_type not accepted in this version")
            attempts.append("no_param")
            return _make_df("名称")

        import akshare as ak
        monkeypatch.setattr(ak, "stock_sector_fund_flow_rank", fake_rank)

        env = get_sector_fund_flow(indicator="今日")
        assert env["ok"] is True
        assert "with_sector_type" in attempts
        assert "no_param" in attempts

    def test_column_name_行业_normalized_to_名称(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """上游返回列名 '行业' 时，records 中的键必须被归一化为 '名称'。"""
        def fake_rank(**kwargs: object) -> pd.DataFrame:
            return _make_df("行业")   # 列名用 "行业"，不是 "名称"

        import akshare as ak
        monkeypatch.setattr(ak, "stock_sector_fund_flow_rank", fake_rank)

        env = get_sector_fund_flow(indicator="今日")
        assert env["ok"] is True
        records = env["data"]["records"]
        assert records, "结果不应为空"
        assert "名称" in records[0], (
            f"列名未被归一化为 '名称'，实际键: {list(records[0].keys())}"
        )

    def test_empty_dataframe_returns_empty_result_error_type(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """上游返回空 DataFrame 时，返回 error_type='EmptyResult'，data=None。"""
        def fake_rank(**kwargs: object) -> pd.DataFrame:
            return pd.DataFrame()   # 空表

        import akshare as ak
        monkeypatch.setattr(ak, "stock_sector_fund_flow_rank", fake_rank)

        env = get_sector_fund_flow(indicator="今日")
        assert env["ok"] is False
        assert env["data"] is None
        assert env["error_type"] == "EmptyResult"

    def test_all_attempts_fail_returns_upstream_compat_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """全部3次参数变体都失败时，返回 error_type='UpstreamCompatibilityError'，不向外抛异常。"""
        def fake_rank(**kwargs: object) -> pd.DataFrame:
            raise KeyError("version totally unknown")

        import akshare as ak
        monkeypatch.setattr(ak, "stock_sector_fund_flow_rank", fake_rank)

        env = get_sector_fund_flow(indicator="今日")   # 不应抛异常
        assert env["ok"] is False
        assert env["data"] is None
        assert env["error_type"] == "UpstreamCompatibilityError"

    def test_upstream_typeerror_also_caught_in_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TypeError（不只是 KeyError）也被渐进试探捕获。"""
        def fake_rank(**kwargs: object) -> pd.DataFrame:
            raise TypeError("unexpected keyword argument 'sector_type'")

        import akshare as ak
        monkeypatch.setattr(ak, "stock_sector_fund_flow_rank", fake_rank)

        env = get_sector_fund_flow(indicator="今日")
        assert env["ok"] is False
        assert env["error_type"] == "UpstreamCompatibilityError"
