# SubAgent Card — MarketRegimeAgent

## 职责
分析当前大盘市场状态（Market Regime），输出 bull/bear/consolidation 及风险偏好。

## 输入 Schema
```json
{ "index_code": "string（可选，默认 sh000001）" }
```

## 输出 Schema（MARKET_REGIME_SCHEMA）
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| regime | enum | ✅ | bull / bear / consolidation / unknown |
| risk_appetite | enum | ✅ | high / medium / low / unknown |
| source | string | ✅ | 数据来源 |
| updated_at | string | ✅ | ISO 8601 时间戳 |
| index_code | string | | 指数代码 |
| index_close | number\|null | | 最新收盘价 |
| index_change_pct_5d | number\|null | | 5日涨跌幅 % |
| northbound_flow_cny | number\|null | | 北向资金净流入（元） |
| regime_note | string | | 判断依据 |
| disclaimer | string | | 免责声明 |

## Regime 判断规则（_determine_regime）
```
5日涨跌幅 > +3%  → bull
5日涨跌幅 < -3%  → bear
其余              → consolidation
5日数据缺失       → unknown

风险偏好（结合北向资金）：
  北向净流入 > 5亿 → high
  北向净流出 > 5亿 → low
  其余             → medium
```

## 数据源
- 大盘日K：AkShare `stock_zh_index_daily()`
- 北向资金：AkShare `stock_em_hsgt_north_acc_flow_in_one()`
- 降级：`source="sample"`，regime/risk_appetite 由降级逻辑决定

## 集成点
- `finance/securities_graph.py` → `market_regime_agent` 节点（并行 specialist 层）
- SynthesisAgent 用 `regime` 调整置信度权重：bear 市降低多头信号权重
- RiskManagerAgent 用 `risk_appetite` 决定仓位上限
