# SubAgent Card — IndustryAgent

## 职责
识别股票所属申万行业，判断行业景气度（资金流入/流出），输出龙头排序。

## 输入 Schema
```json
{ "symbol": "string（6位股票代码）" }
```

## 输出 Schema（INDUSTRY_SCHEMA）
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| symbol | string | ✅ | 股票代码 |
| industry_name | string | ✅ | 所属行业 |
| source | string | ✅ | 数据来源 |
| updated_at | string | ✅ | ISO 8601 时间戳 |
| prosperity_signal | enum | ✅ | booming / normal / sluggish / unknown |
| prosperity_note | string | | 判断依据 |
| top_stocks | array | | 行业龙头排序（涨幅） |
| fund_flow_3d_cny | number\|null | | 3日资金净流入（元） |
| disclaimer | string | | 免责声明 |

## 景气度规则（_prosperity_from_fund_flow）
```
净流入 > 5亿  → booming
净流出 > 5亿  → sluggish
其余          → normal
数据缺失      → unknown
```

## 数据源
- 行业识别：AkShare `stock_individual_info_em()`
- 行业资金流向：AkShare `stock_sector_fund_flow_rank()`
- 行业成分股排名：AkShare `stock_sector_spot_em()`
- 降级：`source="sample"`，signal="unknown"，top_stocks=[]

## Harness 配置
```python
harness = AgentHarness(
    guardrails=[JSONSchemaValidator(INDUSTRY_SCHEMA), SourceAttributionFilter(), KeywordBlocker()],
    max_retries=2,
)
```

## 反幻觉设计
- 景气度判断由净流入金额数值驱动，非 LLM 生成
- `top_stocks` 直接来自 AkShare DataFrame，按涨幅排序
- 降级路径明确：任意 AkShare 异常 → source="sample"

## 集成点
- `finance/securities_graph.py` → `industry_agent` 节点（并行 specialist 层）
- 下游：`SynthesisAgent` 消费 `prosperity_signal`
