"""
离线样例数据模块
================
为 DEMO001-DEMO004 和 TEST001-TEST020 提供确定性的四维分析样例数据。
保证 force_offline=True 时零网络调用，且相同输入返回相同结果。
"""
from __future__ import annotations

from typing import Any

# ═══════════════════════════════════════════════════════════════
#   基本面样例数据（Fundamental）
# ═══════════════════════════════════════════════════════════════

SAMPLE_FUNDAMENTAL_DATA: dict[str, dict[str, Any]] = {
    "DEMO001": {
        "name": "创新科技股份",
        "pe_ttm": 25.6,
        "pb": 3.2,
        "total_market_value_cny": 1250000000.0,  # 12.5亿
        "roe_pct": 18.5,
        "debt_to_asset_pct": 32.5,
        "valuation_signal": "fairly_valued",
        "valuation_note": "PE=25.6（处于合理区间15–40x）；PB=3.2（处于合理区间1–5x）",
    },
    "DEMO002": {
        "name": "稳健制造",
        "pe_ttm": 12.3,
        "pb": 1.8,
        "total_market_value_cny": 850000000.0,  # 8.5亿
        "roe_pct": 15.2,
        "debt_to_asset_pct": 45.8,
        "valuation_signal": "undervalued",
        "valuation_note": "PE=12.3（低于合理区间下限15x）；PB=1.8（处于合理区间1–5x）",
    },
    "DEMO003": {
        "name": "高成长科技",
        "pe_ttm": 52.8,
        "pb": 6.5,
        "total_market_value_cny": 3200000000.0,  # 32亿
        "roe_pct": 22.3,
        "debt_to_asset_pct": 28.4,
        "valuation_signal": "overvalued",
        "valuation_note": "PE=52.8（高于历史均值40x）；PB=6.5（高于历史均值5x）",
    },
    "DEMO004": {
        "name": "传统能源",
        "pe_ttm": 8.5,
        "pb": 0.9,
        "total_market_value_cny": 520000000.0,  # 5.2亿
        "roe_pct": 8.7,
        "debt_to_asset_pct": 58.6,
        "valuation_signal": "undervalued",
        "valuation_note": "PE=8.5（低于合理区间下限15x）；PB=0.9（低于净资产）",
    },
    "000001": {
        "name": "平安银行",
        "pe_ttm": 6.8,
        "pb": 0.72,
        "total_market_value_cny": 210000000000.0,  # 2100亿
        "roe_pct": 10.5,
        "debt_to_asset_pct": 91.8,
        "valuation_signal": "undervalued",
        "valuation_note": "PE=6.8（低于合理区间下限15x）；PB=0.72（低于净资产）",
    },
    "600519": {
        "name": "贵州茅台",
        "pe_ttm": 35.2,
        "pb": 12.8,
        "total_market_value_cny": 2100000000000.0,  # 2.1万亿
        "roe_pct": 32.5,
        "debt_to_asset_pct": 21.5,
        "valuation_signal": "fairly_valued",
        "valuation_note": "PE=35.2（处于合理区间15–40x）；PB=12.8（高于历史均值5x，但白酒龙头溢价）",
    },
}

# ═══════════════════════════════════════════════════════════════
#   行业分析样例数据（Industry）
# ═══════════════════════════════════════════════════════════════

SAMPLE_INDUSTRY_DATA: dict[str, dict[str, Any]] = {
    "DEMO001": {
        "industry_name": "软件服务",
        "prosperity_signal": "booming",
        "prosperity_note": "3日资金净流入 8.5 亿，行业景气",
        "fund_flow_3d_cny": 850000000.0,
        "top_stocks": [
            {"rank": 1, "code": "300750", "name": "宁德时代", "change_pct": 5.2},
            {"rank": 2, "code": "688981", "name": "中芯国际", "change_pct": 4.8},
            {"rank": 3, "code": "300059", "name": "东方财富", "change_pct": 3.6},
            {"rank": 4, "code": "002475", "name": "立讯精密", "change_pct": 2.9},
            {"rank": 5, "code": "601888", "name": "中国中免", "change_pct": 2.3},
        ],
    },
    "DEMO002": {
        "industry_name": "机械制造",
        "prosperity_signal": "normal",
        "prosperity_note": "3日资金净流入 1.2 亿，行业中性",
        "fund_flow_3d_cny": 120000000.0,
        "top_stocks": [
            {"rank": 1, "code": "601766", "name": "中国中车", "change_pct": 1.5},
            {"rank": 2, "code": "000425", "name": "徐工机械", "change_pct": 1.2},
            {"rank": 3, "code": "002415", "name": "海康威视", "change_pct": 0.8},
            {"rank": 4, "code": "600031", "name": "三一重工", "change_pct": 0.5},
            {"rank": 5, "code": "002594", "name": "比亚迪", "change_pct": 0.3},
        ],
    },
    "DEMO003": {
        "industry_name": "半导体",
        "prosperity_signal": "booming",
        "prosperity_note": "3日资金净流入 15.3 亿，行业景气",
        "fund_flow_3d_cny": 1530000000.0,
        "top_stocks": [
            {"rank": 1, "code": "688981", "name": "中芯国际", "change_pct": 8.5},
            {"rank": 2, "code": "688396", "name": "华润微", "change_pct": 7.2},
            {"rank": 3, "code": "603501", "name": "韦尔股份", "change_pct": 6.8},
            {"rank": 4, "code": "002371", "name": "北方华创", "change_pct": 5.5},
            {"rank": 5, "code": "688223", "name": "晶科能源", "change_pct": 4.9},
        ],
    },
    "DEMO004": {
        "industry_name": "煤炭开采",
        "prosperity_signal": "sluggish",
        "prosperity_note": "3日资金净流出 6.8 亿，行业低迷",
        "fund_flow_3d_cny": -680000000.0,
        "top_stocks": [
            {"rank": 1, "code": "601088", "name": "中国神华", "change_pct": -1.2},
            {"rank": 2, "code": "601225", "name": "陕西煤业", "change_pct": -1.5},
            {"rank": 3, "code": "600188", "name": "兖矿能源", "change_pct": -2.1},
            {"rank": 4, "code": "601898", "name": "中煤能源", "change_pct": -2.5},
            {"rank": 5, "code": "600123", "name": "兰花科创", "change_pct": -3.2},
        ],
    },
    "000001": {
        "industry_name": "银行",
        "prosperity_signal": "normal",
        "prosperity_note": "3日资金净流入 2.8 亿，行业中性",
        "fund_flow_3d_cny": 280000000.0,
        "top_stocks": [
            {"rank": 1, "code": "601398", "name": "工商银行", "change_pct": 0.8},
            {"rank": 2, "code": "601939", "name": "建设银行", "change_pct": 0.7},
            {"rank": 3, "code": "601288", "name": "农业银行", "change_pct": 0.5},
            {"rank": 4, "code": "601328", "name": "交通银行", "change_pct": 0.3},
            {"rank": 5, "code": "000001", "name": "平安银行", "change_pct": 0.2},
        ],
    },
    "600519": {
        "industry_name": "白酒",
        "prosperity_signal": "booming",
        "prosperity_note": "3日资金净流入 12.5 亿，行业景气",
        "fund_flow_3d_cny": 1250000000.0,
        "top_stocks": [
            {"rank": 1, "code": "600519", "name": "贵州茅台", "change_pct": 3.5},
            {"rank": 2, "code": "000858", "name": "五粮液", "change_pct": 2.8},
            {"rank": 3, "code": "000596", "name": "古井贡酒", "change_pct": 2.5},
            {"rank": 4, "code": "002304", "name": "洋河股份", "change_pct": 2.1},
            {"rank": 5, "code": "600809", "name": "山西汾酒", "change_pct": 1.9},
        ],
    },
}

# ═══════════════════════════════════════════════════════════════
#   市场状态样例数据（Market Regime）
# ═══════════════════════════════════════════════════════════════

# 市场状态为全局数据，不区分个股，使用指数代码作为 key
SAMPLE_MARKET_REGIME_DATA: dict[str, dict[str, Any]] = {
    "sh000001": {  # 上证指数
        "regime": "consolidation",
        "risk_appetite": "medium",
        "index_code": "sh000001",
        "index_close": 3245.67,
        "index_change_pct_5d": 1.25,
        "northbound_flow_cny": 320000000.0,  # 3.2亿
        "regime_note": "5日涨跌幅 +1.25%；北向净流入 3.2 亿",
    },
    # 为不同场景提供多个预设状态
    "bull_market": {
        "regime": "bull",
        "risk_appetite": "high",
        "index_code": "sh000001",
        "index_close": 3452.18,
        "index_change_pct_5d": 5.8,
        "northbound_flow_cny": 1850000000.0,  # 18.5亿
        "regime_note": "5日涨跌幅 +5.80%；北向净流入 18.5 亿",
    },
    "bear_market": {
        "regime": "bear",
        "risk_appetite": "low",
        "index_code": "sh000001",
        "index_close": 2998.45,
        "index_change_pct_5d": -4.2,
        "northbound_flow_cny": -980000000.0,  # -9.8亿
        "regime_note": "5日涨跌幅 -4.20%；北向净流出 9.8 亿",
    },
}


# ═══════════════════════════════════════════════════════════════
#   默认数据（当 symbol 不在上述字典中时使用）
# ═══════════════════════════════════════════════════════════════

def get_sample_fundamental(symbol: str) -> dict[str, Any]:
    """获取离线基本面样例数据。"""
    if symbol in SAMPLE_FUNDAMENTAL_DATA:
        return SAMPLE_FUNDAMENTAL_DATA[symbol].copy()

    # 默认数据（用于 TEST001-TEST020 等）
    return {
        "name": f"样例公司{symbol}",
        "pe_ttm": 20.0,
        "pb": 2.5,
        "total_market_value_cny": 1000000000.0,  # 10亿
        "roe_pct": 12.0,
        "debt_to_asset_pct": 42.0,
        "valuation_signal": "fairly_valued",
        "valuation_note": "PE=20.0（处于合理区间15–40x）；PB=2.5（处于合理区间1–5x）",
    }


def get_sample_industry(symbol: str) -> dict[str, Any]:
    """获取离线行业样例数据。"""
    if symbol in SAMPLE_INDUSTRY_DATA:
        return SAMPLE_INDUSTRY_DATA[symbol].copy()

    # 默认数据
    return {
        "industry_name": "综合行业",
        "prosperity_signal": "normal",
        "prosperity_note": "3日资金净流入 0.5 亿，行业中性",
        "fund_flow_3d_cny": 50000000.0,
        "top_stocks": [
            {"rank": 1, "code": "000001", "name": "平安银行", "change_pct": 0.5},
            {"rank": 2, "code": "600519", "name": "贵州茅台", "change_pct": 0.3},
            {"rank": 3, "code": "600036", "name": "招商银行", "change_pct": 0.2},
        ],
    }


def get_sample_market_regime(index_code: str = "sh000001", scenario: str = "default") -> dict[str, Any]:
    """获取离线市场状态样例数据。

    Parameters
    ----------
    index_code : str
        指数代码，默认 sh000001（上证指数）
    scenario : str
        场景：default（震荡）、bull_market（牛市）、bear_market（熊市）
    """
    if scenario == "bull_market":
        return SAMPLE_MARKET_REGIME_DATA["bull_market"].copy()
    elif scenario == "bear_market":
        return SAMPLE_MARKET_REGIME_DATA["bear_market"].copy()
    elif index_code in SAMPLE_MARKET_REGIME_DATA:
        return SAMPLE_MARKET_REGIME_DATA[index_code].copy()
    else:
        return SAMPLE_MARKET_REGIME_DATA["sh000001"].copy()
