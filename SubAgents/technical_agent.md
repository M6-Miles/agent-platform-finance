# SubAgent 定义卡片: 技术分析 Agent

**ID**: technical-analysis-agent  
**阶段**: 阶段二  
**类型**: Specialist Agent（Loop 自治 + Harness 包裹）

## 职责

对单只股票进行完整技术面分析，输出结构化报告。

## 输入

| 字段 | 类型 | 说明 |
|------|------|------|
| `symbol` | str | 股票代码（如 `600519`、`DEMO001`） |
| `start_date` | str | 分析开始日期（ISO 8601） |
| `end_date` | str | 分析结束日期（可选） |

## 工具集（Loop 调用）

- `analyze_security(symbol, start, end)` — 计算全套技术指标

## 输出 Schema

```json
{
  "symbol": "string",
  "name": "string",
  "source": "string",
  "updated_at": "string",
  "latest_close": "number",
  "latest_ma5": "number",
  "latest_ma20": "number",
  "latest_ema12": "number",
  "latest_ema26": "number",
  "latest_macd": "number",
  "latest_rsi": "number",
  "latest_kdj_k": "number",
  "latest_kdj_d": "number",
  "latest_kdj_j": "number",
  "latest_bb_position_pct": "number",
  "latest_atr": "number",
  "latest_cci": "number",
  "total_return_pct": "number",
  "max_drawdown_pct": "number",
  "signal": "bullish|bearish|neutral",
  "markdown_report": "string"
}
```

## Harness 配置

```python
guardrails = [
    JSONSchemaValidator(TECHNICAL_ANALYSIS_SCHEMA),
    SourceAttributionFilter(required=["source", "updated_at"]),
    CrossValidator(tool="calculate_indicators", tolerance=0.01),
    KeywordBlocker(["绝对", "一定", "肯定涨停"]),
]
```

## 反幻觉关键设计

所有技术指标由 `indicators.py`（pandas 代码）计算，`CrossValidator` 自动比对 LLM 输出与代码计算结果，误差 > 1% 时重新生成。
