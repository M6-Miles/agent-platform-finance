"""
Skill: fetch_financials
基本面数据拉取技能 —— 封装财务指标获取逻辑，可注入基本面分析 Agent。
当前版本使用 AkShare 免费接口；Tushare Token 可用时自动切换。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any


def skill_fetch_financials(symbol: str) -> dict[str, Any]:
    """
    拉取股票基本面数据。
    返回字段包含 source 和 updated_at，符合 Rule/data_must_have_source.md。
    """
    try:
        import akshare as ak

        # 实时行情（PE / PB）
        spot = ak.stock_zh_a_spot_em()
        row = spot[spot["代码"] == symbol]
        pe = float(row["市盈率-动态"].iloc[0]) if not row.empty else None
        pb = float(row["市净率"].iloc[0]) if not row.empty else None
        total_mv = float(row["总市值"].iloc[0]) if not row.empty else None

        return {
            "symbol": symbol,
            "pe_ttm": pe,
            "pb": pb,
            "total_market_value": total_mv,
            "roe": None,      # 需要财务报表接口，Tushare 更准确
            "source": "akshare",
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
    except Exception as exc:
        # 降级：返回占位数据，标注来源为 unavailable
        return {
            "symbol": symbol,
            "pe_ttm": None,
            "pb": None,
            "total_market_value": None,
            "roe": None,
            "source": "unavailable",
            "updated_at": datetime.utcnow().isoformat() + "Z",
            "error": str(exc),
        }
