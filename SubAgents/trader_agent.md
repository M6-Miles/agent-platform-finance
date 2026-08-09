# SubAgent Card — TraderAgent

## 职责
基于 SynthesisAgent 输出生成交易信号：买入/卖出/持有 + 目标价区间。
**本系统不执行真实交易，不连接任何交易所或模拟交易所接口。**

## 输入 Schema
```json
{
  "synthesis_result": "SynthesisResult.to_dict()",
  "regime_result":    "MarketRegimeResult.to_dict()"
}
```

## 输出 Schema（TRADER_SCHEMA）
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| symbol | string | ✅ | 股票代码 |
| signal | enum | ✅ | buy / sell / hold |
| target_price_low | number\|null | | 目标价下限 |
| target_price_high | number\|null | | 目标价上限 |
| stop_loss_price | number\|null | | 止损价 |
| position_pct_suggestion | number 0-100 | ✅ | 建议仓位 % |
| rationale | string | ✅ | 信号理由 |
| source | string | ✅ | "trader" |
| updated_at | string | ✅ | ISO 8601 |
| disclaimer | string | ✅ | 免责声明 |

## 信号生成规则
```
signal = synthesis_result["overall_signal"]

目标价：
  buy  → target = latest_close * (1 + confidence/100 * 0.15)
         target_low = latest_close * 1.03，target_high = target * 1.0
  sell → target_low = latest_close * 0.90，target_high = latest_close * 0.97
  hold → target_low = latest_close * 0.97，target_high = latest_close * 1.03

止损价（买入）：= latest_close * (1 - ATR/latest_close * 2)

仓位建议：
  confidence >= 70 且 regime == "bull" → min(10, 仓位上限)  %
  confidence >= 50                     → 5 %
  hold                                 → 0 %
  sell                                 → -（清仓提示）
```

## 硬约束（Rule/no_trade_without_confirmation.md）
- `position_pct_suggestion > 10` → 触发 `HumanApprovalRequired`，不自动输出
- 所有输出包含免责声明："仅供研究参考，不构成投资建议"

## Harness 配置
```python
harness = AgentHarness(
    guardrails=[
        JSONSchemaValidator(TRADER_SCHEMA),
        SourceAttributionFilter(),
        KeywordBlocker(),   # 拦截"绝对稳赚"等
    ],
    max_retries=2,
)
```

## 集成点
- `finance/securities_graph.py` → `trader_agent` 节点
  - 条件边：`synthesis.confidence > 30` 才进入
- 下游：`RiskManagerAgent` 检验仓位合规性
