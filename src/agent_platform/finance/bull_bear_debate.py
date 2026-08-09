"""
多轮 Bull/Bear 结构化辩论
=========================
说明书要求把原先的「简单打分」升级为**至少两轮**结构化辩论，每轮遵循
Claim → Evidence → Reasoning → Rebuttal，并增加一致性检查与偏见检测。

为什么单独成模块（而不是塞进 synthesis_agent）
----------------------------------------------
`synthesize()` 产出的 signal/confidence 是回测 E-01 的输入。若把辩论结论
反馈进总分，回测数字会随辩论措辞变化而漂移，且极易变成「调辩论权重去凑
Sharpe 阈值」——那是说明书明令禁止的。因此本模块：

1. **只读**四路 Agent 的 to_dict() 输出，不修改任何打分；
2. 辩论结论以独立字段挂在 SynthesisResult 上，signal/confidence 逐字节不变；
3. 发现矛盾或一边倒证据时**标记**（blocked / passed=False）并写入
   `blocking_reasons`，由调用方决定是否拦截；本模块不静默改信号。

这样「辩论」是可审计的推理记录，而不是一个能被用来粉饰回测的旋钮。

三个环节
--------
- Round 1「开场陈述」：多空各自 Claim + Evidence + Reasoning
- Round 2「交叉反驳」：各自针对对方 Round 1 的 Claim 提出 Rebuttal
- Synthesis「综合裁定」：按证据质量裁定优势方，并输出一致性检查与偏见检测

证据可溯源
----------
每条 Evidence 必带 metric / value / source / updated_at / data_status。
data_status 为 fallback / unavailable / offline_sample 的证据会被降权，
并在偏见检测里计入「弱证据」——避免用样例数据充当实盘论据。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# 证据权重：实盘数据 1.0，样例/降级数据显著降权
_STATUS_WEIGHT: dict[str, float] = {
    "live": 1.0,
    "offline_sample": 0.5,
    "fallback": 0.4,
    "unavailable": 0.0,
}
_DEFAULT_STATUS_WEIGHT = 0.5

# 一边倒判定阈值：某方证据占比超过此值且对方证据数为 0 → 一边倒
_ONE_SIDED_RATIO = 0.85
# 弱证据占比超过此值 → 标记证据质量不足
_WEAK_EVIDENCE_RATIO = 0.60


# ═══════════════════════════════════════════════════════════════
#   Schema（说明书要求「定义每轮的输入输出 Schema」）
# ═══════════════════════════════════════════════════════════════

EVIDENCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["metric", "value", "source", "updated_at", "data_status"],
    "properties": {
        "metric": {"type": "string"},
        "value": {},
        "source": {"type": "string"},
        "updated_at": {"type": "string"},
        "data_status": {
            "type": "string",
            "enum": ["live", "offline_sample", "fallback", "unavailable"],
        },
        "weight": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "direction": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
        "origin_agent": {"type": "string"},
    },
}

DEBATE_TURN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["side", "claim", "evidence", "reasoning"],
    "properties": {
        "side": {"type": "string", "enum": ["bull", "bear"]},
        "claim": {"type": "string"},
        "evidence": {"type": "array", "items": EVIDENCE_SCHEMA},
        "reasoning": {"type": "string"},
        "rebuttal": {"type": ["string", "null"]},
        "rebuts_claim": {"type": ["string", "null"]},
        "evidence_weight": {"type": "number", "minimum": 0.0},
    },
}

DEBATE_ROUND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["round_index", "round_name", "bull", "bear"],
    "properties": {
        "round_index": {"type": "integer", "minimum": 1},
        "round_name": {"type": "string"},
        "bull": DEBATE_TURN_SCHEMA,
        "bear": DEBATE_TURN_SCHEMA,
    },
}

DEBATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "symbol", "rounds", "synthesis", "consistency_check",
        "bias_report", "blocked", "source", "updated_at",
    ],
    "properties": {
        "symbol": {"type": "string"},
        "rounds": {"type": "array", "items": DEBATE_ROUND_SCHEMA, "minItems": 2},
        "synthesis": {"type": "object"},
        "consistency_check": {"type": "object"},
        "bias_report": {"type": "object"},
        "blocked": {"type": "boolean"},
        "blocking_reasons": {"type": "array", "items": {"type": "string"}},
        # 数据质量类标记：必须显著展示，但不构成阻断
        "warnings": {"type": "array", "items": {"type": "string"}},
        "source": {"type": "string"},
        "updated_at": {"type": "string"},
        "disclaimer": {"type": "string"},
    },
}


# ═══════════════════════════════════════════════════════════════
#   数据结构
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class Evidence:
    """一条可溯源证据。"""

    metric: str
    value: Any
    source: str
    updated_at: str
    data_status: str
    direction: str            # bullish / bearish / neutral
    origin_agent: str

    @property
    def weight(self) -> float:
        return _STATUS_WEIGHT.get(self.data_status, _DEFAULT_STATUS_WEIGHT)

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "value": self.value,
            "source": self.source,
            "updated_at": self.updated_at,
            "data_status": self.data_status,
            "weight": self.weight,
            "direction": self.direction,
            "origin_agent": self.origin_agent,
        }


@dataclass(frozen=True, slots=True)
class DebateTurn:
    """一方在某一轮的发言。"""

    side: str                        # bull / bear
    claim: str
    evidence: list[Evidence]
    reasoning: str
    rebuttal: str | None = None
    rebuts_claim: str | None = None

    @property
    def evidence_weight(self) -> float:
        return round(sum(e.weight for e in self.evidence), 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "claim": self.claim,
            "evidence": [e.to_dict() for e in self.evidence],
            "reasoning": self.reasoning,
            "rebuttal": self.rebuttal,
            "rebuts_claim": self.rebuts_claim,
            "evidence_weight": self.evidence_weight,
        }


@dataclass(frozen=True, slots=True)
class DebateRound:
    round_index: int
    round_name: str
    bull: DebateTurn
    bear: DebateTurn

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_index": self.round_index,
            "round_name": self.round_name,
            "bull": self.bull.to_dict(),
            "bear": self.bear.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class DebateResult:
    symbol: str
    rounds: list[DebateRound]
    synthesis: dict[str, Any]
    consistency_check: dict[str, Any]
    bias_report: dict[str, Any]
    blocked: bool
    blocking_reasons: list[str]
    source: str
    updated_at: str
    disclaimer: str
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "rounds": [r.to_dict() for r in self.rounds],
            "synthesis": dict(self.synthesis),
            "consistency_check": dict(self.consistency_check),
            "bias_report": dict(self.bias_report),
            "blocked": self.blocked,
            "blocking_reasons": list(self.blocking_reasons),
            "source": self.source,
            "updated_at": self.updated_at,
            "disclaimer": self.disclaimer,
            "warnings": list(self.warnings),
        }

    def to_markdown(self) -> str:
        lines = [f"### {self.symbol} 多空辩论（{len(self.rounds)} 轮）", ""]
        for r in self.rounds:
            lines.append(f"**第 {r.round_index} 轮 · {r.round_name}**")
            for turn in (r.bull, r.bear):
                tag = "🐮 多方" if turn.side == "bull" else "🐻 空方"
                lines.append(f"- {tag} 主张：{turn.claim}")
                if turn.evidence:
                    for e in turn.evidence:
                        lines.append(
                            f"    · 证据 {e.metric}={e.value}"
                            f"（{e.data_status}，权重 {e.weight}，来源 {e.source}）"
                        )
                else:
                    lines.append("    · 证据：（无）")
                lines.append(f"    · 推理：{turn.reasoning}")
                if turn.rebuttal:
                    lines.append(f"    · 反驳：{turn.rebuttal}")
            lines.append("")
        s = self.synthesis
        lines += [
            "**综合裁定**",
            f"- 优势方：{s.get('advantage')}（多方权重 {s.get('bull_weight')} / "
            f"空方权重 {s.get('bear_weight')}）",
            f"- 裁定说明：{s.get('verdict')}",
            "",
            f"**一致性检查**：{'通过' if self.consistency_check.get('passed') else '未通过'}",
        ]
        for issue in self.consistency_check.get("issues", []):
            lines.append(f"  - {issue}")
        lines += ["", f"**偏见检测**：{'通过' if self.bias_report.get('passed') else '未通过'}"]
        for issue in self.bias_report.get("issues", []):
            lines.append(f"  - {issue}")
        if self.blocked:
            lines += ["", "**⛔ 已标记阻断**"]
            for reason in self.blocking_reasons:
                lines.append(f"  - {reason}")
        lines += ["", f"> ⚠️ {self.disclaimer}"]
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#   证据抽取（只读各 Agent 的 to_dict()）
# ═══════════════════════════════════════════════════════════════

def _meta(d: dict[str, Any], agent: str) -> tuple[str, str, str]:
    """取出 source / updated_at / data_status，缺失时给出显式占位而非编造。"""
    return (
        str(d.get("source") or f"{agent}(source缺失)"),
        str(d.get("updated_at") or ""),
        str(d.get("data_status") or "unavailable"),
    )


def _ev(
    d: dict[str, Any], agent: str, metric: str, value: Any, direction: str
) -> Evidence:
    src, upd, status = _meta(d, agent)
    return Evidence(
        metric=metric, value=value, source=src, updated_at=upd,
        data_status=status, direction=direction, origin_agent=agent,
    )


def extract_evidence(
    technical: dict[str, Any],
    fundamental: dict[str, Any],
    industry: dict[str, Any],
    regime: dict[str, Any],
) -> tuple[list[Evidence], list[Evidence]]:
    """
    从四路结果抽取多空双方证据。

    判定方向使用与 synthesis_agent 一致的经验阈值，保证辩论与打分口径不冲突
    （口径不一致本身就会被一致性检查抓出来）。
    """
    bull: list[Evidence] = []
    bear: list[Evidence] = []

    # ── 技术面 ──
    #
    # 字段名与阈值必须与 synthesis_agent._score_technical 完全一致：
    # TechnicalResult.to_dict() 的实际键名是 latest_rsi / latest_macd /
    # latest_macd_signal / latest_ma5 / latest_ma20 / latest_bb_position_pct，
    # 不存在 rsi14 / macd_hist / ma20。用错名字不会报错，只会静默抽不到证据，
    # 导致 agent_coverage 缺少 technical——这类"静默漏证据"必须靠对齐打分函数避免。
    rsi = technical.get("latest_rsi")
    if isinstance(rsi, (int, float)):
        if rsi < 30:
            bull.append(_ev(technical, "technical", "latest_rsi", round(rsi, 2), "bullish"))
        elif rsi > 70:
            bear.append(_ev(technical, "technical", "latest_rsi", round(rsi, 2), "bearish"))

    # MACD 柱 = DIF - DEA，由两个真实字段现算，不臆造 macd_hist 字段。
    macd = technical.get("latest_macd")
    macd_signal = technical.get("latest_macd_signal")
    if isinstance(macd, (int, float)) and isinstance(macd_signal, (int, float)):
        hist = round(macd - macd_signal, 4)
        if hist != 0:
            bullish = hist > 0
            bull_or_bear = bull if bullish else bear
            bull_or_bear.append(_ev(
                technical, "technical", "macd_histogram", hist,
                "bullish" if bullish else "bearish",
            ))

    # 均线排列：与打分函数同样要求 close/ma5/ma20 三者同向，避免口径分歧。
    close = technical.get("latest_close")
    ma5 = technical.get("latest_ma5")
    ma20 = technical.get("latest_ma20")
    if all(isinstance(v, (int, float)) and v for v in (close, ma5, ma20)):
        if close > ma5 > ma20:
            bull.append(_ev(
                technical, "technical", "ma_alignment",
                f"close({close:.2f})>MA5({ma5:.2f})>MA20({ma20:.2f})", "bullish",
            ))
        elif close < ma5 < ma20:
            bear.append(_ev(
                technical, "technical", "ma_alignment",
                f"close({close:.2f})<MA5({ma5:.2f})<MA20({ma20:.2f})", "bearish",
            ))

    bb_pos = technical.get("latest_bb_position_pct")
    if isinstance(bb_pos, (int, float)):
        if bb_pos < 20:
            bull.append(_ev(
                technical, "technical", "latest_bb_position_pct",
                round(bb_pos, 2), "bullish",
            ))
        elif bb_pos > 80:
            bear.append(_ev(
                technical, "technical", "latest_bb_position_pct",
                round(bb_pos, 2), "bearish",
            ))

    # ── 基本面 ──
    vs = fundamental.get("valuation_signal")
    if vs == "undervalued":
        bull.append(_ev(fundamental, "fundamental", "valuation_signal", vs, "bullish"))
    elif vs == "overvalued":
        bear.append(_ev(fundamental, "fundamental", "valuation_signal", vs, "bearish"))

    roe = fundamental.get("roe_pct")
    if isinstance(roe, (int, float)):
        if roe >= 15:
            bull.append(_ev(fundamental, "fundamental", "roe_pct", roe, "bullish"))
        elif roe < 5:
            bear.append(_ev(fundamental, "fundamental", "roe_pct", roe, "bearish"))

    debt = fundamental.get("debt_to_asset_pct")
    if isinstance(debt, (int, float)) and debt > 70:
        bear.append(_ev(fundamental, "fundamental", "debt_to_asset_pct", debt, "bearish"))

    dcf = fundamental.get("dcf")
    if isinstance(dcf, dict) and dcf.get("applicable"):
        mos = dcf.get("margin_of_safety_pct")
        if isinstance(mos, (int, float)):
            (bull if mos > 0 else bear).append(_ev(
                fundamental, "fundamental", "dcf_margin_of_safety_pct",
                round(mos, 2), "bullish" if mos > 0 else "bearish",
            ))

    # ── 行业面 ──
    ps = industry.get("prosperity_signal")
    if ps == "booming":
        bull.append(_ev(industry, "industry", "prosperity_signal", ps, "bullish"))
    elif ps == "sluggish":
        bear.append(_ev(industry, "industry", "prosperity_signal", ps, "bearish"))

    flow = industry.get("fund_flow_3d_cny")
    if isinstance(flow, (int, float)) and flow:
        (bull if flow > 0 else bear).append(_ev(
            industry, "industry", "fund_flow_3d_cny", flow,
            "bullish" if flow > 0 else "bearish",
        ))

    # ── 市场面 / 宏观 ──
    #
    # regime 取值为 bull / bear / consolidation / unknown。consolidation 与 unknown
    # 不构成方向性证据，按中性跳过（与 _score_regime 给 0 分一致）。
    rt = regime.get("regime")
    if rt == "bull":
        bull.append(_ev(regime, "market_regime", "regime", rt, "bullish"))
    elif rt == "bear":
        bear.append(_ev(regime, "market_regime", "regime", rt, "bearish"))

    # 以下三项 synthesis 打分未使用（_score_regime 只看 regime 字段），
    # 但说明书要求辩论体现市场/宏观维度的引用证据。辩论结论不参与
    # signal/confidence 计算，因此这里比打分函数取证更宽不会造成口径冲突。
    appetite = regime.get("risk_appetite")
    if appetite == "high":
        bull.append(_ev(regime, "market_regime", "risk_appetite", appetite, "bullish"))
    elif appetite == "low":
        bear.append(_ev(regime, "market_regime", "risk_appetite", appetite, "bearish"))

    # ±1% 以内视为噪声，不计入证据，避免用无意义的小波动凑论据。
    chg5 = regime.get("index_change_pct_5d")
    if isinstance(chg5, (int, float)):
        if chg5 >= 1.0:
            bull.append(_ev(
                regime, "market_regime", "index_change_pct_5d", round(chg5, 2), "bullish",
            ))
        elif chg5 <= -1.0:
            bear.append(_ev(
                regime, "market_regime", "index_change_pct_5d", round(chg5, 2), "bearish",
            ))

    north = regime.get("northbound_flow_cny")
    if isinstance(north, (int, float)) and north:
        bullish = north > 0
        (bull if bullish else bear).append(_ev(
            regime, "market_regime", "northbound_flow_cny", north,
            "bullish" if bullish else "bearish",
        ))

    return bull, bear


# ═══════════════════════════════════════════════════════════════
#   两轮辩论构造
# ═══════════════════════════════════════════════════════════════

def _claim(side: str, evidence: list[Evidence]) -> str:
    if not evidence:
        return (
            "当前无支持看多的可溯源证据" if side == "bull"
            else "当前无支持看空的可溯源证据"
        )
    metrics = "、".join(e.metric for e in evidence[:4])
    if side == "bull":
        return f"该标的具备上行基础，主要依据 {metrics} 等 {len(evidence)} 项指标"
    return f"该标的存在下行风险，主要依据 {metrics} 等 {len(evidence)} 项指标"


def _reasoning(side: str, evidence: list[Evidence]) -> str:
    if not evidence:
        return "无证据可用，本方在本轮不作实质主张，以避免无依据推断。"
    weight = round(sum(e.weight for e in evidence), 2)
    weak = [e.metric for e in evidence if e.data_status != "live"]
    text = (
        f"共 {len(evidence)} 项证据，加权强度 {weight}。"
        f"{'多头' if side == 'bull' else '空头'}逻辑链："
        + " → ".join(f"{e.metric}={e.value}" for e in evidence[:3])
    )
    if weak:
        text += f"。注意其中 {len(weak)} 项非实时数据（{'、'.join(weak[:3])}），结论强度相应下调"
    return text + "。"


def _rebuttal(side: str, own: list[Evidence], opponent: list[Evidence]) -> str:
    """针对对方证据的反驳。无对方证据时明确说明，不虚构对手论点。"""
    if not opponent:
        return "对方本轮未提出可溯源证据，无需反驳。"

    opp_weak = [e for e in opponent if e.data_status != "live"]
    own_weight = round(sum(e.weight for e in own), 2)
    opp_weight = round(sum(e.weight for e in opponent), 2)

    parts: list[str] = []
    if opp_weak:
        parts.append(
            f"对方 {len(opponent)} 项证据中有 {len(opp_weak)} 项来自"
            f"非实时数据（{'、'.join(e.metric for e in opp_weak[:3])}），"
            f"其推断不足以支撑实盘结论"
        )
    if own_weight > opp_weight:
        parts.append(f"本方加权强度 {own_weight} 高于对方 {opp_weight}")
    elif own_weight < opp_weight:
        parts.append(
            f"本方加权强度 {own_weight} 低于对方 {opp_weight}，"
            f"承认对方在证据量上占优，但仍主张风险不可忽视"
            if side == "bear"
            else f"本方加权强度 {own_weight} 低于对方 {opp_weight}，承认证据量劣势"
        )
    else:
        parts.append(f"双方加权强度相当（各 {own_weight}），单凭证据量无法定论")

    overlap = {e.metric for e in own} & {e.metric for e in opponent}
    if overlap:
        parts.append(
            f"另需指出：{'、'.join(sorted(overlap))} 被双方同时引用且方向相反，"
            f"该指标不应作为决定性依据"
        )
    return "；".join(parts) + "。"


def _build_rounds(
    bull_ev: list[Evidence], bear_ev: list[Evidence]
) -> list[DebateRound]:
    """第 1 轮开场陈述，第 2 轮交叉反驳。"""
    r1_bull = DebateTurn(
        side="bull", claim=_claim("bull", bull_ev), evidence=bull_ev,
        reasoning=_reasoning("bull", bull_ev),
    )
    r1_bear = DebateTurn(
        side="bear", claim=_claim("bear", bear_ev), evidence=bear_ev,
        reasoning=_reasoning("bear", bear_ev),
    )

    r2_bull = DebateTurn(
        side="bull", claim=f"维持看多主张，并回应空方质疑（{len(bear_ev)} 项）",
        evidence=bull_ev, reasoning=_reasoning("bull", bull_ev),
        rebuttal=_rebuttal("bull", bull_ev, bear_ev),
        rebuts_claim=r1_bear.claim,
    )
    r2_bear = DebateTurn(
        side="bear", claim=f"维持看空主张，并回应多方质疑（{len(bull_ev)} 项）",
        evidence=bear_ev, reasoning=_reasoning("bear", bear_ev),
        rebuttal=_rebuttal("bear", bear_ev, bull_ev),
        rebuts_claim=r1_bull.claim,
    )

    return [
        DebateRound(1, "开场陈述（Claim + Evidence + Reasoning）", r1_bull, r1_bear),
        DebateRound(2, "交叉反驳（Rebuttal）", r2_bull, r2_bear),
    ]


# ═══════════════════════════════════════════════════════════════
#   一致性检查 / 偏见检测
# ═══════════════════════════════════════════════════════════════

def consistency_check(
    bull_ev: list[Evidence],
    bear_ev: list[Evidence],
    fundamental: dict[str, Any],
) -> dict[str, Any]:
    """
    引用矛盾检查。命中任一项 → passed=False。

    检查项：
    1. 同一 metric 被双方同时引用（同一数据点推出相反结论）
    2. 基本面 valuation_signal 与 DCF 安全边际方向冲突
    3. 证据的 data_status 与 source 自相矛盾（声称 live 却来自样例数据）
    """
    issues: list[str] = []

    bull_metrics = {e.metric for e in bull_ev}
    bear_metrics = {e.metric for e in bear_ev}
    overlap = bull_metrics & bear_metrics
    if overlap:
        issues.append(
            f"引用矛盾：指标 {sorted(overlap)} 同时被多空双方引用，"
            f"同一数据点被用于推出相反结论"
        )

    vs = fundamental.get("valuation_signal")
    dcf = fundamental.get("dcf")
    if isinstance(dcf, dict) and dcf.get("applicable"):
        mos = dcf.get("margin_of_safety_pct")
        if isinstance(mos, (int, float)):
            if vs == "undervalued" and mos < -20:
                issues.append(
                    f"估值结论冲突：倍数法判定 undervalued，"
                    f"但 DCF 安全边际 {mos:.1f}%（显著高估）"
                )
            elif vs == "overvalued" and mos > 20:
                issues.append(
                    f"估值结论冲突：倍数法判定 overvalued，"
                    f"但 DCF 安全边际 +{mos:.1f}%（显著低估）"
                )

    for e in bull_ev + bear_ev:
        if e.data_status == "live" and ("样例" in e.source or "sample" in e.source.lower()):
            issues.append(
                f"溯源矛盾：证据 {e.metric} 声称 live，但 source 指向样例数据（{e.source}）"
            )

    return {
        "passed": not issues,
        "issues": issues,
        "checked_items": ["metric_overlap", "valuation_vs_dcf", "status_vs_source"],
    }


def bias_detector(
    bull_ev: list[Evidence], bear_ev: list[Evidence]
) -> dict[str, Any]:
    """
    偏见检测。区分**阻断级**与**提示级**两类问题。

    说明书的要求是「发现引用矛盾或一边倒证据时必须标记或阻断」，据此分级：

    阻断级（blocking_issues → passed=False，调用方应拦截后再使用）：
    1. 无任何可溯源证据
    2. 一边倒证据（一方为 0 且另一方权重占比超阈值）

    提示级（warnings → 必须显著标记，但不阻断）：
    3. 证据来源集中（全部证据来自单一 Agent）
    4. 弱证据占比过高（非 live 数据占比超阈值）

    第 3、4 项是数据质量问题，不是逻辑偏见。离线样本模式下弱证据占比恒为
    100%，若按阻断处理会使离线验收的每一只股票全部阻断 —— 阻断信号失去
    区分度，反而会掩盖真正的引用矛盾。故降级为标记。
    """
    blocking: list[str] = []
    warn_items: list[str] = []
    all_ev = bull_ev + bear_ev
    total = len(all_ev)

    bull_w = round(sum(e.weight for e in bull_ev), 4)
    bear_w = round(sum(e.weight for e in bear_ev), 4)
    total_w = round(bull_w + bear_w, 4)

    if total == 0:
        blocking.append("无任何可溯源证据，辩论结论不可用")
        return {
            "passed": False, "issues": list(blocking),
            "blocking_issues": list(blocking), "warnings": [],
            "bull_weight": 0.0, "bear_weight": 0.0,
            "one_sided": True, "weak_evidence_ratio": 1.0,
            "agent_coverage": [],
        }

    one_sided = False
    if total_w > 0:
        if not bear_ev and bull_w / total_w >= _ONE_SIDED_RATIO:
            one_sided = True
            blocking.append(
                f"一边倒证据：空方 0 项，多方权重占比 "
                f"{bull_w / total_w:.0%} ≥ {_ONE_SIDED_RATIO:.0%}，缺少反面验证"
            )
        elif not bull_ev and bear_w / total_w >= _ONE_SIDED_RATIO:
            one_sided = True
            blocking.append(
                f"一边倒证据：多方 0 项，空方权重占比 "
                f"{bear_w / total_w:.0%} ≥ {_ONE_SIDED_RATIO:.0%}，缺少反面验证"
            )

    agents = sorted({e.origin_agent for e in all_ev})
    if len(agents) == 1:
        warn_items.append(
            f"证据来源集中：全部 {total} 项证据均来自 {agents[0]}，维度覆盖不足"
        )

    weak = [e for e in all_ev if e.data_status != "live"]
    weak_ratio = round(len(weak) / total, 4)
    if weak_ratio >= _WEAK_EVIDENCE_RATIO:
        warn_items.append(
            f"证据质量不足：{len(weak)}/{total}（{weak_ratio:.0%}）为非实时数据，"
            f"不足以支撑实盘决策"
        )

    return {
        # passed 只看阻断级问题；提示级问题另见 warnings，不得因此判定失败
        "passed": not blocking,
        "issues": blocking + warn_items,      # 完整问题清单（阻断项在前）
        "blocking_issues": blocking,
        "warnings": warn_items,
        "bull_weight": bull_w,
        "bear_weight": bear_w,
        "one_sided": one_sided,
        "weak_evidence_ratio": weak_ratio,
        "agent_coverage": agents,
    }


def _synthesize_verdict(
    bull_ev: list[Evidence], bear_ev: list[Evidence]
) -> dict[str, Any]:
    """按加权证据强度裁定优势方。不产生 signal —— signal 归 synthesize() 所有。"""
    bull_w = round(sum(e.weight for e in bull_ev), 4)
    bear_w = round(sum(e.weight for e in bear_ev), 4)

    if bull_w > bear_w:
        advantage, verdict = "bull", f"多方证据加权 {bull_w} 高于空方 {bear_w}，多方占优"
    elif bear_w > bull_w:
        advantage, verdict = "bear", f"空方证据加权 {bear_w} 高于多方 {bull_w}，空方占优"
    else:
        advantage, verdict = "tie", f"双方加权强度相等（各 {bull_w}），辩论未分出优势方"

    return {
        "advantage": advantage,
        "verdict": verdict,
        "bull_weight": bull_w,
        "bear_weight": bear_w,
        "bull_evidence_count": len(bull_ev),
        "bear_evidence_count": len(bear_ev),
        "note": "本裁定为独立推理记录，不参与 signal/confidence 计算",
    }


# ═══════════════════════════════════════════════════════════════
#   对外入口
# ═══════════════════════════════════════════════════════════════

def run_debate(
    symbol: str,
    technical: dict[str, Any],
    fundamental: dict[str, Any],
    industry: dict[str, Any],
    regime: dict[str, Any],
) -> DebateResult:
    """
    执行两轮结构化多空辩论。

    Returns
    -------
    DebateResult
        blocked=True 仅在**引用矛盾**（一致性检查未通过）或**一边倒证据**
        （某一方 0 项证据）时置位，调用方应当拦截或显著标记后再使用。

        数据质量类问题（证据来源集中、非实时数据占比过高）走 warnings，
        必须显著标记但不阻断 —— 离线样本模式下弱证据占比恒为 100%，若
        按阻断处理会使每一只股票全部阻断，阻断信号将失去区分度。

        本函数**不**因阻断或标记修改任何交易信号：signal/confidence 始终
        归 synthesize() 所有。
    """
    from agent_platform.finance.constants import DISCLAIMER

    updated_at = datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z"

    bull_ev, bear_ev = extract_evidence(technical, fundamental, industry, regime)
    rounds = _build_rounds(bull_ev, bear_ev)
    verdict = _synthesize_verdict(bull_ev, bear_ev)
    cc = consistency_check(bull_ev, bear_ev, fundamental)
    bias = bias_detector(bull_ev, bear_ev)

    # 阻断只由「引用矛盾（一致性检查）」和「一边倒证据」触发；
    # 数据质量类问题走 warnings，显著标记但不拦截。
    blocking_reasons: list[str] = []
    if not cc["passed"]:
        blocking_reasons += [f"一致性检查：{i}" for i in cc["issues"]]
    blocking_reasons += [f"偏见检测：{i}" for i in bias.get("blocking_issues", [])]

    # 变量名避开标准库 warnings 模块，防止后续在本模块 import warnings 时被遮蔽
    warn_marks: list[str] = [f"偏见检测：{w}" for w in bias.get("warnings", [])]

    result = DebateResult(
        symbol=symbol,
        rounds=rounds,
        synthesis=verdict,
        consistency_check=cc,
        bias_report=bias,
        blocked=bool(blocking_reasons),
        blocking_reasons=blocking_reasons,
        source="bull_bear_debate（基于四路 Agent 输出，两轮结构化辩论）",
        updated_at=updated_at,
        disclaimer=DISCLAIMER,
        warnings=warn_marks,
    )
    if result.blocked:
        logger.info(
            "[Debate] %s 已标记阻断，共 %d 条原因", symbol, len(blocking_reasons)
        )
    elif warn_marks:
        logger.info("[Debate] %s 有 %d 条数据质量标记（未阻断）", symbol, len(warn_marks))
    return result
