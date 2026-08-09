# Rule: 数据必须携带来源信息

**ID**: rule-002  
**优先级**: HIGH  
**适用范围**: 所有数据获取工具和分析 Agent

## 规则内容

所有 Agent 输出和工具返回的数据，**必须** 包含 `source` 和 `updated_at` 字段，否则被 `SourceAttributionFilter` 拦截。

## 必填字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `source` | str | 数据来源（如 `"akshare"`, `"tushare"`, `"sample"`） |
| `updated_at` | str | ISO 8601 时间戳（如 `"2026-07-31T10:00:00Z"`） |

## 违规示例

```python
# ❌ 缺少 source —— 被 SourceAttributionFilter 拦截
{"symbol": "600519", "close": 1800.0}

# ✅ 合规
{"symbol": "600519", "close": 1800.0, "source": "akshare", "updated_at": "2026-07-31T10:00:00Z"}
```

## 执行动作

`SourceAttributionFilter.validate_output()` 检测到缺失时：
1. 返回 `GuardrailViolation("缺少 source 或 updated_at 字段")`
2. 写入审计日志
3. 不阻断流程，但在报告中标记 `⚠️ 数据来源待核实`

## 说明

此规则源自 Harness 防幻觉设计——LLM 无法凭空编造带时间戳的真实数据源，强制来源字段可有效识别幻觉数据。
