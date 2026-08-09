"""
情感分析 Agent（SentimentAgent）
================================
基于关键词匹配对市场舆情进行快速评分，
输出 SentimentResult（score: −10 ~ +10）。

Harness：SourceAttributionFilter + KeywordBlocker
离线测试不依赖 AkShare（通过 _sample_headlines 注入）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# ─── 关键词词库 ───────────────────────────────────────────────────────────────

# 正面关键词（每个命中 +2 分）
_POSITIVE_KEYWORDS: list[str] = [
    "利好", "增长", "创新高", "超预期", "业绩好", "扩张", "回购",
    "分红", "订单增加", "提升", "强劲", "新高", "突破", "上调评级",
    "高增长", "净利润增加", "营收增长", "战略合作", "获得订单",
]

# 负面关键词（每个命中 -2 分）
_NEGATIVE_KEYWORDS: list[str] = [
    "利空", "下跌", "亏损", "违规", "处罚", "减持", "跌停",
    "暴雷", "风险", "警告", "监管", "亏损扩大", "营收下滑",
    "股东减持", "被罚", "诉讼", "债务", "违约", "退市风险",
]


# ─── 数据类 ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class SentimentResult:
    symbol: str
    score: int              # -10 ~ +10
    sentiment: str          # "positive" / "negative" / "neutral"
    keywords_found: list[str]
    headline_count: int     # 实际分析的新闻条数
    source: str
    updated_at: str
    disclaimer: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "score": self.score,
            "sentiment": self.sentiment,
            "keywords_found": list(self.keywords_found),
            "headline_count": self.headline_count,
            "source": self.source,
            "updated_at": self.updated_at,
            "disclaimer": self.disclaimer,
        }


# ─── 内部工具函数 ─────────────────────────────────────────────────────────────

def _classify_sentiment(score: int) -> str:
    """将分数映射到情感标签。"""
    if score > 3:
        return "positive"
    if score < -3:
        return "negative"
    return "neutral"


def _score_headlines(headlines: list[str]) -> tuple[int, list[str]]:
    """
    对新闻标题列表进行关键词匹配打分。
    返回 (score, keywords_found)，score 已限制到 [-10, +10]。
    """
    raw = 0
    found: list[str] = []
    text = " ".join(headlines)

    for kw in _POSITIVE_KEYWORDS:
        if kw in text:
            raw += 2
            found.append(kw)
    for kw in _NEGATIVE_KEYWORDS:
        if kw in text:
            raw -= 2
            found.append(kw)

    return max(-10, min(10, raw)), found


# ─── 公开 API ─────────────────────────────────────────────────────────────────

def analyze_sentiment(
    symbol: str,
    stock_name: str = "",
    *,
    _sample_headlines: list[str] | None = None,  # 离线测试/单元测试用注入点
) -> SentimentResult:
    """
    获取股票相关新闻并进行舆情评分。

    数据来源优先级：
      1. _sample_headlines（测试注入，优先）
      2. AkShare stock_news_em（在线）
      3. 空列表（离线降级，得分为0）

    返回 SentimentResult，score 范围 -10~+10，
    disclaimer 固定为「仅供研究参考，不构成投资建议」。
    """
    try:
        from agent_platform.finance.constants import DISCLAIMER as _DISC
    except ImportError:
        _DISC = "仅供研究参考，不构成投资建议"

    updated_at = datetime.utcnow().isoformat() + "Z"
    headlines: list[str] = []
    source = "sentiment"

    if _sample_headlines is not None:
        # 测试注入路径（最高优先级）
        headlines = list(_sample_headlines)
        source = "sample"
    else:
        # 尝试通过 AkShare 获取个股新闻
        try:
            import akshare as ak  # type: ignore[import]
            news_df = ak.stock_news_em(symbol=symbol)
            if news_df is not None and not news_df.empty and "新闻标题" in news_df.columns:
                headlines = news_df["新闻标题"].head(30).tolist()
                source = f"akshare/stock_news_em/{symbol}"
            else:
                logger.warning("AkShare stock_news_em 返回空数据: symbol=%s", symbol)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("AkShare 新闻获取失败，离线降级: %s", exc)
            headlines = []

    score, keywords = _score_headlines(headlines)
    sentiment_label = _classify_sentiment(score)

    return SentimentResult(
        symbol=symbol,
        score=score,
        sentiment=sentiment_label,
        keywords_found=keywords,
        headline_count=len(headlines),
        source=source,
        updated_at=updated_at,
        disclaimer=_DISC,
    )
