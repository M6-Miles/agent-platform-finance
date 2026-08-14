"""
综合研判 Agent（SynthesisAgent）
================================
汇总 Technical / Fundamental / Industry / MarketRegime 四路输出，
通过 Bull/Bear 辩论打分机制生成最终信号（buy/sell/hold）与置信度（0.0–1.0）。

输出符合 Scripts/validate_schema.py :: SYNTHESIS_SCHEMA
Harness：JSONSchemaValidator + SourceAttributionFilter + KeywordBlocker + CrossValidator
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

SYNTHESIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["symbol", "signal", "confidence", "reasoning", "source", "updated_at"],
    "properties": {
        "symbol": {"type": "string"},
        "signal": {"type": "string", "enum": ["buy", "sell", "hold", "watch"]},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "target_price_low": {},
        "target_price_high": {},
        "bull_arguments": {"type": "array"},
        "bear_arguments": {"type": "array"},
        "reasoning": {"type": "string"},
        "source": {"type": "string"},
        "updated_at": {"type": "string"},
        "disclaimer": {"type": "string"},
        # ─── 多轮辩论字段（仅 with_debate=True 时非空）─────────────────────
        # 故意不加入 required：默认关闭辩论时三者为 None/False/[]，
        # 既有 Schema 校验与既有调用方行为完全不变。
        "debate": {},
        "debate_blocked": {"type": "boolean"},
        "debate_warnings": {"type": "array"},
    },
}


@dataclass(frozen=True, slots=True)
class SynthesisResult:
    symbol: str
    signal: str             # buy / sell / hold / watch
    confidence: float       # 0.0–1.0
    target_price_low: float | None
    target_price_high: float | None
    bull_arguments: list[str]
    bear_arguments: list[str]
    reasoning: str
    source: str
    updated_at: str
    disclaimer: str
    # ─── 以下三个字段带默认值 ────────────────────────────────────────────────
    # 保证既有调用方（含全部现存测试）按原 11 个位置参数构造时不报错。
    # debate 为 DebateResult.to_dict()，仅 synthesize(with_debate=True) 时填充。
    debate: dict[str, Any] | None = None
    # 一致性检查发现引用矛盾、或证据一边倒时为 True，调用方应拦截或显著标记。
    debate_blocked: bool = False
    # 数据质量类标记（如证据多为非实时数据）：必须显著展示，但不阻断。
    debate_warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "signal": self.signal,
            "confidence": self.confidence,
            "target_price_low": self.target_price_low,
            "target_price_high": self.target_price_high,
            "bull_arguments": list(self.bull_arguments),
            "bear_arguments": list(self.bear_arguments),
            "reasoning": self.reasoning,
            "source": self.source,
            "updated_at": self.updated_at,
            "disclaimer": self.disclaimer,
            "debate": dict(self.debate) if self.debate else None,
            "debate_blocked": self.debate_blocked,
            "debate_warnings": list(self.debate_warnings),
        }

    def to_markdown(self) -> str:
        signal_emoji = {"buy": "🟢 买入", "sell": "🔴 卖出", "hold": "🟡 持有", "watch": "👀 观察"}
        bulls = "\n".join(f"  - {a}" for a in self.bull_arguments) or "  （无）"
        bears = "\n".join(f"  - {a}" for a in self.bear_arguments) or "  （无）"
        tp_str = (
            f"{self.target_price_low:.2f} – {self.target_price_high:.2f}"
            if self.target_price_low is not None
            else "N/A"
        )
        lines = [
            f"### {self.symbol} 综合研判结果",
            f"- 数据来源：{self.source}，更新时间：{self.updated_at}",
            "",
            "**综合信号**",
            f"- 信号：{signal_emoji.get(self.signal, self.signal)}",
            f"- 置信度：{self.confidence:.0%}",
            f"- 目标价区间：{tp_str}",
            "",
            "**多方论据**",
            bulls,
            "",
            "**空方论据**",
            bears,
            "",
            f"**综合判断**：{self.reasoning}",
        ]

        # 阻断与数据质量标记必须显著展示，不能只留在 debate 字典里等调用方自己去翻
        if self.debate_blocked:
            lines += ["", "**⛔ 多空辩论已标记阻断，结论需人工复核**"]
            for reason in (self.debate or {}).get("blocking_reasons", []):
                lines.append(f"  - {reason}")
        if self.debate_warnings:
            lines += ["", "**⚠️ 数据质量标记（不阻断）**"]
            for warn in self.debate_warnings:
                lines.append(f"  - {warn}")
        if self.debate:
            n_rounds = len(self.debate.get("rounds", []))
            adv = self.debate.get("synthesis", {}).get("advantage", "unknown")
            lines += [
                "",
                f"**多空辩论**：{n_rounds} 轮结构化辩论，优势方 {adv}"
                f"（仅作推理记录，不参与 signal/confidence 计算）",
            ]

        lines += ["", f"> ⚠️ {self.disclaimer}"]
        return "\n".join(lines)


# ─── Bull/Bear 打分规则 ───────────────────────────────────────────────────────

def _score_technical(tech: dict[str, Any]) -> tuple[int, list[str], list[str]]:
    """技术面打分，返回 (分数, bull论据, bear论据)。"""
    score = 0
    bulls: list[str] = []
    bears: list[str] = []

    rsi = tech.get("latest_rsi")
    macd = tech.get("latest_macd")
    macd_signal = tech.get("latest_macd_signal")
    close = tech.get("latest_close")
    ma5 = tech.get("latest_ma5")
    ma20 = tech.get("latest_ma20")
    bb_pos = tech.get("latest_bb_position_pct")   # 0–100，50 = 中轨

    if rsi is not None:
        if rsi < 30:
            score += 20
            bulls.append(f"RSI={rsi:.1f} 超卖区域，可能反弹")
        elif rsi > 70:
            score -= 20
            bears.append(f"RSI={rsi:.1f} 超买区域，注意回调风险")

    if macd is not None and macd_signal is not None:
        if macd > macd_signal and macd > 0:
            score += 20
            bulls.append("MACD 金叉且在零轴上方，动能向好")
        elif macd < macd_signal and macd < 0:
            score -= 20
            bears.append("MACD 死叉且在零轴下方，动能偏弱")
        elif macd > macd_signal:
            score += 10
            bulls.append("MACD 金叉（零轴下方）")
        elif macd < macd_signal:
            score -= 10
            bears.append("MACD 死叉（零轴上方）")

    if close and ma5 and ma20:
        if close > ma5 > ma20:
            score += 15
            bulls.append("价格 > MA5 > MA20，均线多头排列")
        elif close < ma5 < ma20:
            score -= 15
            bears.append("价格 < MA5 < MA20，均线空头排列")

    if bb_pos is not None:
        if bb_pos < 20:
            score += 10
            bulls.append(f"价格接近布林下轨（位置={bb_pos:.0f}%），可能超卖")
        elif bb_pos > 80:
            score -= 10
            bears.append(f"价格接近布林上轨（位置={bb_pos:.0f}%），注意压力")

    return score, bulls, bears


def _score_fundamental(fund: dict[str, Any]) -> tuple[int, list[str], list[str]]:
    score = 0
    bulls: list[str] = []
    bears: list[str] = []

    sig = fund.get("valuation_signal", "unknown")
    note = fund.get("valuation_note", "")
    if sig == "undervalued":
        score += 15
        bulls.append(f"基本面低估：{note}")
    elif sig == "overvalued":
        score -= 15
        bears.append(f"基本面高估：{note}")

    return score, bulls, bears


def _score_industry(ind: dict[str, Any]) -> tuple[int, list[str], list[str]]:
    score = 0
    bulls: list[str] = []
    bears: list[str] = []

    sig = ind.get("prosperity_signal", "unknown")
    name = ind.get("industry_name", "未知行业")
    if sig == "booming":
        score += 10
        bulls.append(f"{name} 行业资金净流入，景气向好")
    elif sig == "sluggish":
        score -= 10
        bears.append(f"{name} 行业资金净流出，景气偏弱")

    return score, bulls, bears


def _score_regime(regime: dict[str, Any]) -> tuple[int, list[str], list[str]]:
    score = 0
    bulls: list[str] = []
    bears: list[str] = []

    r = regime.get("regime", "unknown")
    note = regime.get("regime_note", "")
    if r == "bull":
        score += 10
        bulls.append(f"大盘处于牛市状态：{note}")
    elif r == "bear":
        score -= 15
        bears.append(f"大盘处于熊市状态：{note}")

    return score, bulls, bears


# ─── 情感面权重上限（2026-08-05 实测校准，勿随意放大）──────────────────────────
#
# SentimentAgent 原始输出为 ±10 分。直接计入总分会使舆情话语权过大：
#   ±10 分 / 180 量纲 = ±5.56pp 置信度位移，
#   而 buy/sell 判定带宽仅 20pp（0.40–0.60），
#   即单靠关键词匹配就能翻转约 28% 的决策区间。
# 关键词舆情是本系统最不可靠的信号（无消歧、无来源加权、无时效衰减），
# 不应具备这种影响力。故按比例压缩到 ±4 分（±2.22pp，占判定带宽 11%）。
#
# 实测依据应记录在对应的策略实验报告中，避免依赖个人进度日志：
# 注入常量分值（信息量为零）即可把均值夏普从 -0.377 拉到 +0.445，
# 该现象是阈值平移导致的偏多，不是舆情有预测力。
_SENTIMENT_RAW_ABS = 10      # SentimentAgent 的原始量纲
_SENTIMENT_MAX_ABS = 4       # 计入总分的上限
_SENTIMENT_MIN_HEADLINES = 5  # 样本不足时不予采信（仅当该字段存在时生效）


def _score_sentiment(sentiment: dict[str, Any]) -> tuple[int, list[str], list[str]]:
    """
    情感面打分，接受 SentimentResult.to_dict() 的输出。

    入参 score 为 ±10 量纲（SentimentAgent 保证），
    计入总分时按比例压缩至 ±_SENTIMENT_MAX_ABS，理由见上方常量注释。

    headline_count 若存在且低于 _SENTIMENT_MIN_HEADLINES，视为样本不足、
    分值归零（但仍产出说明性论据）。该字段缺失时不做限制，
    以兼容直接构造 dict 的调用方。
    """
    raw = int(sentiment.get("score", 0))
    raw = max(-_SENTIMENT_RAW_ABS, min(_SENTIMENT_RAW_ABS, raw))

    bulls: list[str] = []
    bears: list[str] = []
    keywords: list[str] = sentiment.get("keywords_found", [])
    kw_str = "、".join(str(k) for k in keywords[:3]) if keywords else ""

    # 样本量门控：仅在调用方明确给出 headline_count 时生效
    n_head = sentiment.get("headline_count")
    if n_head is not None and int(n_head) < _SENTIMENT_MIN_HEADLINES:
        if raw != 0:
            bears.append(
                f"舆情样本不足（仅 {int(n_head)} 条新闻，"
                f"低于 {_SENTIMENT_MIN_HEADLINES} 条门槛），本次不计入评分"
            )
        return 0, bulls, bears

    # 按比例压缩到 ±_SENTIMENT_MAX_ABS
    scaled = int(round(raw * _SENTIMENT_MAX_ABS / _SENTIMENT_RAW_ABS))
    scaled = max(-_SENTIMENT_MAX_ABS, min(_SENTIMENT_MAX_ABS, scaled))

    # 论据仍按原始强度描述（阈值沿用 ±3），但计分用压缩后的值
    if raw > 3:
        bulls.append(
            "舆情偏正面（原始 {:+d} → 计入 {:+d}）{}".format(
                raw, scaled, f"，关键词：{kw_str}" if kw_str else ""
            )
        )
    elif raw < -3:
        bears.append(
            "舆情偏负面（原始 {:+d} → 计入 {:+d}）{}".format(
                raw, scaled, f"，关键词：{kw_str}" if kw_str else ""
            )
        )

    return scaled, bulls, bears


def synthesize(
    symbol: str,
    technical: dict[str, Any],
    fundamental: dict[str, Any],
    industry: dict[str, Any],
    regime: dict[str, Any],
    *,
    regime_aware: bool = False,     # 启用后根据市场状态调整各维度权重
    sentiment: dict[str, Any] | None = None,  # SentimentResult.to_dict()，可选
    with_debate: bool = False,      # 附加两轮结构化多空辩论（不影响 signal/confidence）
) -> SynthesisResult:
    """
    四路（+可选情感面）信号汇总 → Bull/Bear 打分 → signal + confidence。
    所有输入均为各 Agent 的 to_dict() 输出。

    regime_aware=True 时：
      - 牛市：技术面权重 ×1.2，基本面权重 ×0.8（动量主导）
      - 熊市：技术面权重 ×0.8，基本面权重 ×1.2（防御主导）

    with_debate=True 时附加 bull_bear_debate.run_debate() 的两轮辩论记录、
    一致性检查与偏见检测结果。**该开关不改变 signal / confidence / 目标价**：
    辩论在三者算完之后才执行，只负责补充可审计的推理链与风险标记。

    默认关闭的原因：回测脚本（run_backtest.py / measure_real_10y.py /
    measure_optimizations.py）会按 bar 循环调用本函数，辩论对每次调用都要重新
    抽取证据并构造两轮对话，属于纯开销；且 E-01 夏普口径必须与历史结果可复现，
    不能因为新增推理链而产生任何数值漂移。主链（securities_graph）显式传 True。
    """
    from agent_platform.finance.constants import DISCLAIMER

    updated_at = datetime.utcnow().isoformat() + "Z"

    # 各维度独立打分
    tech_s, tech_b, tech_br   = _score_technical(technical)
    fund_s, fund_b, fund_br   = _score_fundamental(fundamental)
    ind_s,  ind_b,  ind_br    = _score_industry(industry)
    regime_s, regime_b, regime_br = _score_regime(regime)

    # Regime-aware 权重调整（可选；不影响默认行为）
    if regime_aware:
        r_type = regime.get("regime", "unknown")
        if r_type == "bull":
            tech_s = int(round(tech_s * 1.2))
            fund_s = int(round(fund_s * 0.8))
        elif r_type == "bear":
            tech_s = int(round(tech_s * 0.8))
            fund_s = int(round(fund_s * 1.2))
        # consolidation / unknown：保持原始权重

    total_score = tech_s + fund_s + ind_s + regime_s
    all_bulls: list[str] = tech_b + fund_b + ind_b + regime_b
    all_bears: list[str] = tech_br + fund_br + ind_br + regime_br

    # 情感面（可选增益，得分范围 ±10）
    if sentiment is not None:
        sent_s, sent_b, sent_br = _score_sentiment(sentiment)
        total_score += sent_s
        all_bulls.extend(sent_b)
        all_bears.extend(sent_br)

    # 置信度：score 范围约 -90 ~ +90，映射到 0.0–1.0
    raw_conf = (total_score + 90) / 180.0
    confidence = max(0.0, min(1.0, round(raw_conf, 3)))

    # 信号
    if confidence >= 0.60:
        signal = "buy"
    elif confidence <= 0.40:
        signal = "sell"
    else:
        signal = "hold"

    # 目标价（基于技术面收盘价，简单估算）
    close = technical.get("latest_close")
    target_low: float | None = None
    target_high: float | None = None
    if close:
        if signal == "buy":
            target_low = round(close * 1.03, 2)
            target_high = round(close * (1 + confidence * 0.15), 2)
        elif signal == "sell":
            target_low = round(close * 0.90, 2)
            target_high = round(close * 0.97, 2)
        else:
            target_low = round(close * 0.97, 2)
            target_high = round(close * 1.03, 2)

    # 综合说明
    bull_cnt = len(all_bulls)
    bear_cnt = len(all_bears)
    reasoning = (
        f"综合技术、基本面、行业及市场状态共 {bull_cnt + bear_cnt} 项因子评估，"
        f"其中多方 {bull_cnt} 项、空方 {bear_cnt} 项，"
        f"得分 {total_score:+d}，置信度 {confidence:.0%}，综合信号 {signal}。"
    )

    # ── 两轮结构化多空辩论（可选，纯附加）────────────────────────────────────
    # 位置刻意放在 signal / confidence / target_price / reasoning 全部算完之后：
    # 辩论只读四路输入，不回写上述任何变量，因此 with_debate 开与关的
    # signal、confidence、目标价、reasoning 完全一致（见
    # tests/test_bull_bear_debate.py::test_with_debate_does_not_change_signal）。
    debate_dict: dict[str, Any] | None = None
    debate_blocked = False
    debate_marks: tuple[str, ...] = ()
    if with_debate:
        from agent_platform.finance.bull_bear_debate import run_debate

        # 此处不做 try/except：run_debate 是无网络的纯字典运算，
        # 抛异常即为真实缺陷，必须暴露而不是伪装成「辩论缺失」。
        dbt = run_debate(
            symbol=symbol,
            technical=technical,
            fundamental=fundamental,
            industry=industry,
            regime=regime,
        )
        debate_dict = dbt.to_dict()
        debate_blocked = dbt.blocked
        debate_marks = tuple(dbt.warnings)

    return SynthesisResult(
        symbol=symbol,
        signal=signal,
        confidence=confidence,
        target_price_low=target_low,
        target_price_high=target_high,
        bull_arguments=all_bulls,
        bear_arguments=all_bears,
        reasoning=reasoning,
        source="synthesis",
        updated_at=updated_at,
        disclaimer=DISCLAIMER,
        debate=debate_dict,
        debate_blocked=debate_blocked,
        debate_warnings=debate_marks,
    )
