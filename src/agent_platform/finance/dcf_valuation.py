"""
DCF 现金流折现估值
==================
说明书 A-02 要求基本面 Agent 输出 **可解释的 DCF 估值**：必须给出字段、公式、
输入假设、边界校验，且不得用固定常数冒充 DCF。本模块即为该要求的实现。

设计原则
--------
1. **输入全部可追溯**：本项目的行情源（AkShare 现货快照 / 离线样例）只提供
   PE_TTM、PB、总市值、ROE。本模块不虚构任何财务科目，而是从这三项按恒等式
   反推出估值所需的会计量：

       净利润(TTM) = 总市值 / PE_TTM
       净资产       = 总市值 / PB
       隐含 ROE     = 净利润 / 净资产 = PB / PE_TTM

   最后一条是恒等式，可与数据源上报的 ROE 交叉校验（见 `warnings`）。

2. **增长率不是常数**：一阶段增长用可持续增长率公式
       g = ROE × 留存率 = ROE × (1 - 分红率)
   该值随标的 ROE 变化；再按 `growth_cap` 截断，避免高 ROE 标的外推出
   不可持续的复合增长。

3. **资本口径一致**：CAPM 只计算股权成本，WACC 再按债务/股权权重加权：
       Ke = 无风险利率 + β × 股权风险溢价
       WACC = E/(D+E) × Ke + D/(D+E) × Kd × (1−税率)
   缺少可靠有息债务数据时默认债务权重为 0，并在结果中明确告警。

4. **两阶段模型**：显式预测期（默认 5 年）增长率由 g1 线性衰减至永续增长率
   g_t，避免"第 6 年增速断崖"这种典型 DCF 错误。终值用 Gordon 永续增长：
       TV = FCF_n × (1 + g_t) / (WACC − g_t)

5. **边界校验硬失败**：WACC ≤ g_t 会让终值变成负数或无穷大，这类输入必须
   判为不适用并给出原因，不能返回一个看起来正常的数字。

不适用情形（`applicable=False`，附 `reason_not_applicable`）
-----------------------------------------------------------
- PE_TTM ≤ 0（亏损企业，DCF 的盈利基数不成立）
- PB ≤ 0（净资产为负）
- 总市值 ≤ 0 或缺失
- WACC ≤ 永续增长率（终值公式退化）
- 预测年限不在 1–15 之间

免责：本模块输出的内在价值是**在给定假设下**的计算结果，假设本身的不确定性
远大于计算误差。所有结果仅供研究参考。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

# ─── 默认假设（全部可覆盖，且在输出中原样回显）──────────────────────────────
#
# 取值依据（A 股口径，2026 年前后）：
#   RISK_FREE_RATE      10 年期国债收益率约 2.5%
#   EQUITY_RISK_PREMIUM A 股长期股权风险溢价约 5.5%
#   TERMINAL_GROWTH     永续增长率不应高于长期名义 GDP 增速，取 2.5%
#   FCF_CONVERSION      净利润 → 自由现金流的转换率。A 股整体 FCF/净利润
#                       长期在 0.6–0.9 之间波动，取 0.75 为中性假设
#   PAYOUT_RATIO        A 股平均现金分红率约 35%，故留存率 65%
#   GROWTH_CAP          单阶段增长率上限。ROE 30% 的公司若全额留存，
#                       可持续增长率达 19.5%，再高则外推不可信
DEFAULT_RISK_FREE_RATE = 0.025
DEFAULT_EQUITY_RISK_PREMIUM = 0.055
DEFAULT_TERMINAL_GROWTH = 0.025
DEFAULT_FCF_CONVERSION = 0.75
DEFAULT_PAYOUT_RATIO = 0.35
DEFAULT_GROWTH_CAP = 0.20
DEFAULT_FORECAST_YEARS = 5
DEFAULT_BETA = 1.0

# WACC 与永续增长率之间要求的最小间距。等于 0 时终值分母为 0。
_MIN_WACC_SPREAD = 0.005

# 隐含 ROE 与上报 ROE 的相对偏差阈值，超过则告警（不阻断）
_ROE_RECONCILE_TOLERANCE = 0.30

# 安全边际分档（内在价值 / 市值 − 1）
_UNDERVALUED_THRESHOLD = 0.20      # 内在价值高出市值 20% 以上 → 低估
_OVERVALUED_THRESHOLD = -0.20      # 内在价值低于市值 20% 以上 → 高估


@dataclass(frozen=True, slots=True)
class DCFAssumptions:
    """DCF 输入假设。全部字段都会在结果的 `assumptions` 中原样回显。"""

    risk_free_rate: float = DEFAULT_RISK_FREE_RATE
    equity_risk_premium: float = DEFAULT_EQUITY_RISK_PREMIUM
    beta: float = DEFAULT_BETA
    debt_weight: float = 0.0
    pretax_cost_of_debt: float = 0.045
    corporate_tax_rate: float = 0.25
    terminal_growth: float = DEFAULT_TERMINAL_GROWTH
    fcf_conversion: float = DEFAULT_FCF_CONVERSION
    payout_ratio: float = DEFAULT_PAYOUT_RATIO
    growth_cap: float = DEFAULT_GROWTH_CAP
    forecast_years: int = DEFAULT_FORECAST_YEARS
    net_debt_cny: float | None = None   # None 表示未知，按 0 处理并告警

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk_free_rate": self.risk_free_rate,
            "equity_risk_premium": self.equity_risk_premium,
            "beta": self.beta,
            "debt_weight": self.debt_weight,
            "pretax_cost_of_debt": self.pretax_cost_of_debt,
            "corporate_tax_rate": self.corporate_tax_rate,
            "terminal_growth": self.terminal_growth,
            "fcf_conversion": self.fcf_conversion,
            "payout_ratio": self.payout_ratio,
            "growth_cap": self.growth_cap,
            "forecast_years": self.forecast_years,
            "net_debt_cny": self.net_debt_cny,
        }


@dataclass(frozen=True, slots=True)
class DCFResult:
    """DCF 估值结果。`applicable=False` 时除 reason 外的数值字段均为 None。"""

    applicable: bool
    reason_not_applicable: str | None
    model_type: str
    confidence_level: str
    source: str
    limitations: list[str]

    # ── 反推出的会计输入 ──
    net_income_cny: float | None
    book_value_cny: float | None
    fcf_base_cny: float | None
    implied_roe_pct: float | None

    # ── 折现与增长参数 ──
    wacc: float | None
    growth_stage1: float | None
    terminal_growth: float | None

    # ── 估值输出 ──
    pv_explicit_cny: float | None        # 显式预测期现值合计
    pv_terminal_cny: float | None        # 终值现值
    enterprise_value_cny: float | None   # 企业价值 = 上两项之和
    equity_value_cny: float | None       # 股权价值 = EV − 净债务
    market_value_cny: float | None
    margin_of_safety_pct: float | None   # (股权价值 / 市值 − 1) × 100
    valuation_signal: str                # undervalued/fairly_valued/overvalued/unknown

    # ── 可解释性 ──
    yearly_projection: list[dict[str, float]] = field(default_factory=list)
    assumptions: dict[str, Any] = field(default_factory=dict)
    formula: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "applicable": self.applicable,
            "reason_not_applicable": self.reason_not_applicable,
            "model_type": self.model_type,
            "confidence_level": self.confidence_level,
            "source": self.source,
            "limitations": list(self.limitations),
            "net_income_cny": self.net_income_cny,
            "book_value_cny": self.book_value_cny,
            "fcf_base_cny": self.fcf_base_cny,
            "implied_roe_pct": self.implied_roe_pct,
            "wacc": self.wacc,
            "growth_stage1": self.growth_stage1,
            "terminal_growth": self.terminal_growth,
            "pv_explicit_cny": self.pv_explicit_cny,
            "pv_terminal_cny": self.pv_terminal_cny,
            "enterprise_value_cny": self.enterprise_value_cny,
            "equity_value_cny": self.equity_value_cny,
            "market_value_cny": self.market_value_cny,
            "margin_of_safety_pct": self.margin_of_safety_pct,
            "valuation_signal": self.valuation_signal,
            "yearly_projection": [dict(y) for y in self.yearly_projection],
            "assumptions": dict(self.assumptions),
            "formula": self.formula,
            "warnings": list(self.warnings),
        }

    def to_markdown(self) -> str:
        if not self.applicable:
            return "\n".join([
                "**DCF 估值**：不适用",
                f"- 原因：{self.reason_not_applicable}",
            ])
        yi = 1e8  # 亿
        lines = [
            "**DCF 估值代理（两阶段 FCFF proxy 折现，低可信）**",
            f"- 模型口径：{self.model_type}；可信度：{self.confidence_level}",
            f"- 基期自由现金流：{self.fcf_base_cny / yi:.2f} 亿元"
            f"（净利润 {self.net_income_cny / yi:.2f} 亿 × 转换率 "
            f"{self.assumptions.get('fcf_conversion')}）",
            f"- 折现率 WACC：{self.wacc:.2%}"
            f"（= {self.assumptions.get('risk_free_rate'):.2%} + "
            f"{self.assumptions.get('beta')} × "
            f"{self.assumptions.get('equity_risk_premium'):.2%}）",
            f"- 一阶段增长率：{self.growth_stage1:.2%}"
            f"（= 隐含 ROE {self.implied_roe_pct:.2f}% × 留存率 "
            f"{1 - self.assumptions.get('payout_ratio', 0):.0%}，"
            f"上限 {self.assumptions.get('growth_cap'):.0%}）",
            f"- 永续增长率：{self.terminal_growth:.2%}",
            f"- 显式期（{self.assumptions.get('forecast_years')} 年）现值："
            f"{self.pv_explicit_cny / yi:.2f} 亿元",
            f"- 终值现值：{self.pv_terminal_cny / yi:.2f} 亿元",
            f"- 企业价值：{self.enterprise_value_cny / yi:.2f} 亿元",
            f"- 股权价值：{self.equity_value_cny / yi:.2f} 亿元",
            f"- 当前市值：{self.market_value_cny / yi:.2f} 亿元",
            f"- 安全边际：{self.margin_of_safety_pct:+.1f}%",
            f"- DCF 估值信号：{self.valuation_signal}",
            f"- 公式：{self.formula}",
        ]
        if self.warnings:
            lines.append("- 假设与数据告警：")
            lines.extend(f"  - {w}" for w in self.warnings)
        return "\n".join(lines)


def beta_from_volatility(
    annualized_volatility_pct: float | None,
    *,
    market_volatility_pct: float = 20.0,
    correlation: float = 0.6,
) -> float:
    """
    由个股年化波动率粗估 β。

        β = ρ × σ_stock / σ_market

    本项目没有个股与指数的日收益协方差数据（技术面 Agent 只输出个股年化波动
    率），故用一个固定的典型相关系数 ρ=0.6 代替。这是**近似**，不是真 β；
    调用方若能拿到真实协方差应直接传 β。返回值截断在 0.5–2.0，避免极端波动
    标的把 WACC 推到不合理区间。
    """
    if annualized_volatility_pct is None or annualized_volatility_pct <= 0:
        return DEFAULT_BETA
    if market_volatility_pct <= 0:
        return DEFAULT_BETA
    beta = correlation * (annualized_volatility_pct / market_volatility_pct)
    return round(max(0.5, min(2.0, beta)), 3)


def compute_cost_of_equity(assumptions: DCFAssumptions) -> float:
    """CAPM 股权成本 Ke = rf + β × ERP。"""
    return assumptions.risk_free_rate + assumptions.beta * assumptions.equity_risk_premium


def compute_wacc(assumptions: DCFAssumptions) -> float:
    """资本结构加权成本，不再把 CAPM 股权成本直接冒充 WACC。"""
    debt_weight = assumptions.debt_weight
    if not 0.0 <= debt_weight <= 1.0:
        raise ValueError(f"debt_weight 必须在 0–1，收到 {debt_weight}")
    if not 0.0 <= assumptions.corporate_tax_rate <= 1.0:
        raise ValueError("corporate_tax_rate 必须在 0–1")
    equity_weight = 1.0 - debt_weight
    return (
        equity_weight * compute_cost_of_equity(assumptions)
        + debt_weight
        * assumptions.pretax_cost_of_debt
        * (1.0 - assumptions.corporate_tax_rate)
    )


def sustainable_growth(implied_roe_pct: float, assumptions: DCFAssumptions) -> float:
    """
    可持续增长率 g = ROE × 留存率，并按 growth_cap 截断。

    负 ROE 时返回 0（不外推负增长——负增长的公司应由 PE ≤ 0 分支拦截，
    这里保留 0 作为兜底）。
    """
    retention = max(0.0, 1.0 - assumptions.payout_ratio)
    g = (implied_roe_pct / 100.0) * retention
    if g < 0:
        return 0.0
    return min(g, assumptions.growth_cap)


def _fade_growth(g1: float, g_terminal: float, year: int, total_years: int) -> float:
    """
    增长率从 g1 线性衰减到 g_terminal。

    year 从 1 计数。year=1 用 g1，year=total_years 用接近 g_terminal 的值，
    避免显式期结束时增速断崖式跳到永续增长率。
    """
    if total_years <= 1:
        return g1
    ratio = (year - 1) / (total_years - 1)
    return g1 + (g_terminal - g1) * ratio


def run_dcf(
    *,
    pe_ttm: float | None,
    pb: float | None,
    total_market_value_cny: float | None,
    roe_pct: float | None = None,
    assumptions: DCFAssumptions | None = None,
) -> DCFResult:
    """
    执行两阶段 DCF 估值。

    Parameters
    ----------
    pe_ttm, pb, total_market_value_cny
        必需。用于反推净利润与净资产，三者缺一则不适用。
    roe_pct
        数据源上报的 ROE，仅用于与隐含 ROE 交叉校验并产出告警；
        计算本身用隐含 ROE（PB/PE），因为它与市值口径自洽。
    assumptions
        输入假设，None 时用模块默认值。

    Returns
    -------
    DCFResult
        `applicable=False` 时 `reason_not_applicable` 说明原因。
    """
    a = assumptions or DCFAssumptions()
    warnings: list[str] = []

    def _not_applicable(reason: str) -> DCFResult:
        return DCFResult(
            applicable=False,
            reason_not_applicable=reason,
            model_type="earnings_to_fcff_proxy",
            confidence_level="low",
            source="PE/PB/总市值推导，非完整现金流量表",
            limitations=[
                "缺少 EBIT、税项、折旧摊销、资本开支和营运资本变动",
                "基期 FCFF 由净利润乘现金转换率近似",
            ],
            net_income_cny=None,
            book_value_cny=None,
            fcf_base_cny=None,
            implied_roe_pct=None,
            wacc=None,
            growth_stage1=None,
            terminal_growth=None,
            pv_explicit_cny=None,
            pv_terminal_cny=None,
            enterprise_value_cny=None,
            equity_value_cny=None,
            market_value_cny=total_market_value_cny,
            margin_of_safety_pct=None,
            valuation_signal="unknown",
            yearly_projection=[],
            assumptions=a.to_dict(),
            formula="",
            warnings=warnings,
        )

    # ── 边界校验 1：预测年限 ──
    if not (1 <= a.forecast_years <= 15):
        return _not_applicable(
            f"预测年限 {a.forecast_years} 超出 1–15 的可信范围"
        )

    # ── 边界校验 2：市值 ──
    if total_market_value_cny is None or total_market_value_cny <= 0:
        return _not_applicable("总市值缺失或非正，无法计算安全边际")

    # ── 边界校验 3：PE（盈利基数）──
    if pe_ttm is None:
        return _not_applicable("PE_TTM 缺失，无法反推净利润")
    if pe_ttm <= 0:
        return _not_applicable(
            f"PE_TTM={pe_ttm:.2f} 非正（企业亏损），DCF 的盈利基数不成立"
        )

    # ── 边界校验 4：PB（净资产）──
    if pb is None:
        return _not_applicable("PB 缺失，无法反推净资产与隐含 ROE")
    if pb <= 0:
        return _not_applicable(f"PB={pb:.2f} 非正（净资产为负），DCF 不适用")

    # ── 反推会计输入 ──
    net_income = total_market_value_cny / pe_ttm
    book_value = total_market_value_cny / pb
    implied_roe_pct = (net_income / book_value) * 100.0   # 恒等于 PB/PE×100

    # 与上报 ROE 交叉校验
    if roe_pct is not None and roe_pct > 0:
        rel_dev = abs(implied_roe_pct - roe_pct) / roe_pct
        if rel_dev > _ROE_RECONCILE_TOLERANCE:
            warnings.append(
                f"隐含 ROE {implied_roe_pct:.2f}%（=PB/PE）与数据源上报 ROE "
                f"{roe_pct:.2f}% 相对偏差 {rel_dev:.0%}，超过 "
                f"{_ROE_RECONCILE_TOLERANCE:.0%} 阈值；可能是 PE/PB/ROE 口径"
                f"（TTM vs 年报、扣非 vs 归母）不一致。本次计算采用隐含 ROE。"
            )

    fcf_base = net_income * a.fcf_conversion
    if fcf_base <= 0:
        return _not_applicable(
            f"基期自由现金流 {fcf_base:.0f} 非正（净利润 {net_income:.0f} × "
            f"转换率 {a.fcf_conversion}），DCF 不适用"
        )

    # ── 折现率与增长率 ──
    wacc = compute_wacc(a)
    g1 = sustainable_growth(implied_roe_pct, a)
    g_t = a.terminal_growth

    # ── 边界校验 5：WACC 必须显著大于永续增长率 ──
    if wacc <= g_t + _MIN_WACC_SPREAD:
        return _not_applicable(
            f"WACC={wacc:.2%} 未显著高于永续增长率 {g_t:.2%}"
            f"（要求至少高 {_MIN_WACC_SPREAD:.2%}），Gordon 终值公式退化，"
            f"结果无意义"
        )

    if g1 >= a.growth_cap:
        warnings.append(
            f"一阶段增长率已被上限截断至 {a.growth_cap:.0%}"
            f"（可持续增长率原值更高），估值偏保守"
        )
    if a.net_debt_cny is None:
        warnings.append(
            "净债务未知（行情快照不含资产负债表），按 0 处理；"
            "对高杠杆标的会高估股权价值"
        )
    if a.beta == DEFAULT_BETA:
        warnings.append(
            f"β 使用默认值 {DEFAULT_BETA}（未传入个股波动率），"
            f"未反映个股风险差异"
        )
    if a.debt_weight == 0.0:
        warnings.append(
            "缺少可靠的有息债务/权益权重，WACC 暂按债务权重 0 计算；"
            "该假设可能低估高杠杆公司的资本成本"
        )
    warnings.append(
        "当前为低可信估值代理：缺少 EBIT、税项、折旧摊销、资本开支和"
        "营运资本变动，FCFF 基数由净利润×现金转换率近似，不等同真实财报 FCFF"
    )

    # ── 显式预测期逐年现金流与现值 ──
    yearly: list[dict[str, float]] = []
    pv_explicit = 0.0
    fcf = fcf_base
    for year in range(1, a.forecast_years + 1):
        g_y = _fade_growth(g1, g_t, year, a.forecast_years)
        fcf = fcf * (1.0 + g_y)
        discount_factor = 1.0 / math.pow(1.0 + wacc, year)
        pv = fcf * discount_factor
        pv_explicit += pv
        yearly.append({
            "year": float(year),
            "growth_rate": round(g_y, 6),
            "fcf_cny": round(fcf, 2),
            "discount_factor": round(discount_factor, 6),
            "present_value_cny": round(pv, 2),
        })

    # ── 终值（Gordon 永续增长）──
    fcf_terminal = fcf * (1.0 + g_t)
    terminal_value = fcf_terminal / (wacc - g_t)
    pv_terminal = terminal_value / math.pow(1.0 + wacc, a.forecast_years)

    enterprise_value = pv_explicit + pv_terminal
    net_debt = a.net_debt_cny if a.net_debt_cny is not None else 0.0
    equity_value = enterprise_value - net_debt

    margin_of_safety_pct = (equity_value / total_market_value_cny - 1.0) * 100.0

    if margin_of_safety_pct >= _UNDERVALUED_THRESHOLD * 100:
        signal = "undervalued"
    elif margin_of_safety_pct <= _OVERVALUED_THRESHOLD * 100:
        signal = "overvalued"
    else:
        signal = "fairly_valued"

    # 终值占比过高是 DCF 的经典脆弱点，必须告警
    if enterprise_value > 0:
        terminal_share = pv_terminal / enterprise_value
        if terminal_share > 0.75:
            warnings.append(
                f"终值现值占企业价值 {terminal_share:.0%}（>75%），"
                f"估值高度依赖永续增长率 {g_t:.2%} 这一假设，稳健性低"
            )

    formula = (
        "EV = Σ_{t=1..N} FCF_0 × Π(1+g_t) / (1+WACC)^t "
        "+ [FCF_N × (1+g_term) / (WACC − g_term)] / (1+WACC)^N；"
        "股权价值 = EV − 净债务；"
        "Ke = rf + β×ERP；WACC = E/(D+E)×Ke + D/(D+E)×Kd×(1−Tax)；"
        "g_1 = 隐含ROE × (1−分红率)，按上限截断；"
        "g_t 由 g_1 线性衰减至 g_term"
    )

    return DCFResult(
        applicable=True,
        reason_not_applicable=None,
        model_type="earnings_to_fcff_proxy",
        confidence_level="low",
        source="PE/PB/总市值推导，非完整现金流量表",
        limitations=[
            "缺少 EBIT、税项、折旧摊销、资本开支和营运资本变动",
            "基期 FCFF 由净利润乘现金转换率近似",
        ],
        net_income_cny=round(net_income, 2),
        book_value_cny=round(book_value, 2),
        fcf_base_cny=round(fcf_base, 2),
        implied_roe_pct=round(implied_roe_pct, 4),
        wacc=round(wacc, 6),
        growth_stage1=round(g1, 6),
        terminal_growth=g_t,
        pv_explicit_cny=round(pv_explicit, 2),
        pv_terminal_cny=round(pv_terminal, 2),
        enterprise_value_cny=round(enterprise_value, 2),
        equity_value_cny=round(equity_value, 2),
        market_value_cny=round(total_market_value_cny, 2),
        margin_of_safety_pct=round(margin_of_safety_pct, 4),
        valuation_signal=signal,
        yearly_projection=yearly,
        assumptions=a.to_dict(),
        formula=formula,
        warnings=warnings,
    )
