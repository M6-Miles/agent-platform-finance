# SubAgent Card — SynthesisAgent

## 职责
汇总 TechnicalAgent / FundamentalAgent / IndustryAgent / MarketRegimeAgent 的输出，
通过 Bull/Bear 辩论机制生成最终置信度（0–100）与综合信号。

## 输入 Schema
```json
{
  "technical_result":    "TechnicalAnalysisResult.to_dict()",
  "fundamental_result":  "FundamentalResult.to_dict()",
  "industry_result":     "IndustryResult.to_dict()",
  "regime_result":       "MarketRegimeResult.to_dict()"
}
```

## 输出 Schema（SYNTHESIS_SCHEMA，见 Scripts/validate_schema.py）
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| symbol | string | ✅ | 股票代码 |
| overall_signal | enum | ✅ | buy / sell / hold |
| confidence | integer 0-100 | ✅ | 综合置信度 |
| bull_arguments | array[string] | ✅ | 多方论据 |
| bear_arguments | array[string] | ✅ | 空方论据 |
| synthesis_note | string | ✅ | 综合说明 |
| source | string | ✅ | "synthesis" |
| updated_at | string | ✅ | ISO 8601 |
| disclaimer | string | ✅ | 免责声明 |

## Bull/Bear 辩论规则
```python
score = 0
# +20：MACD 金叉 / RSI < 30 超卖 / 均线多头排列
# +15：valuation_signal == "undervalued"
# +10：prosperity_signal == "booming"
# +10：regime == "bull"
# -20：MACD 死叉 / RSI > 70 超买
# -15：valuation_signal == "overvalued"
# -10：prosperity_signal == "sluggish"
# -15：regime == "bear"

confidence = max(0, min(100, 50 + score))
overall_signal:
  confidence >= 60 → buy
  confidence <= 40 → sell
  otherwise        → hold
```

## Harness 配置
```python
harness = AgentHarness(
    guardrails=[
        JSONSchemaValidator(SYNTHESIS_SCHEMA),
        SourceAttributionFilter(),
        KeywordBlocker(),
        CrossValidator(ground_truth={"confidence": [0, 100]}),
    ],
    max_retries=2,
)
```

## 反幻觉设计
- 置信度由规则打分计算，LLM 仅提供 bull_arguments / bear_arguments 文本
- CrossValidator 验证 confidence 在 [0,100] 范围内
- disclaimer 硬编码于 SynthesisResult

## 集成点
- `finance/securities_graph.py` → `synthesis_agent` 节点（等待四路 specialist 汇合）
- 条件边：`confidence > 0.3`（即30）→ `trader` 节点
