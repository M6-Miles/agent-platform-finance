# SubAgent Card — FundamentalAgent

## 职责
对单支 A 股进行基本面分析：PE/PB/总市值/ROE，输出结构化估值信号。

## 输入 Schema
```json
{
  "symbol": "string（6位股票代码）",
  "name":   "string（可选，公司名称）"
}
```

## 输出 Schema（FUNDAMENTAL_SCHEMA）
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| symbol | string | ✅ | 股票代码 |
| name | string | | 公司名 |
| source | string | ✅ | 数据来源 |
| updated_at | string | ✅ | ISO 8601 时间戳 |
| pe_ttm | number\|null | | 市盈率 TTM |
| pb | number\|null | | 市净率 |
| total_market_value_cny | number\|null | | 总市值（元） |
| roe_pct | number\|null | | ROE % |
| valuation_signal | enum | ✅ | undervalued / fairly_valued / overvalued / unknown |
| valuation_note | string | | 判断依据 |
| disclaimer | string | | 免责声明 |

## 估值规则（_valuation_signal）
```
PE < 15 且 PB < 2  → undervalued
PE > 40 或 PB > 5  → overvalued
PE 或 PB 均 None   → unknown（单指标可用则仍判断）
其余               → fairly_valued
多数表决：各指标投票，多数胜出
```

## 数据源
- 主：AkShare `stock_zh_a_spot_em()`（PE/PB/总市值）
- 降级：`source="sample"`，所有数值为 None，signal="unknown"

## Harness 配置
```python
harness = AgentHarness(
    agent=...,
    guardrails=[
        JSONSchemaValidator(FUNDAMENTAL_SCHEMA),
        SourceAttributionFilter(),
        KeywordBlocker(),          # 拦截"绝对稳赚"等违禁词
    ],
    max_retries=2,
)
```

## 反幻觉设计
- 所有 PE/PB 直接取自 AkShare DataFrame，无 LLM 推断数值
- `_valuation_signal()` 是纯函数，逻辑确定性
- `disclaimer` 硬编码，不经过 LLM
- `source` + `updated_at` 强制要求，SourceAttributionFilter 保障

## 集成点
- `finance/securities_graph.py` → `fundamental_agent` 节点（并行 specialist 层）
- 下游：`SynthesisAgent` 消费 `valuation_signal`
