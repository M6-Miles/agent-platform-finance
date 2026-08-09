# 天气分析 Demo — 非金融领域接入演示

> 演示通用 Agent 平台（Agent = Model + Harness）在非金融领域的零改动接入能力。

## 快速启动

```bash
# 从项目根目录运行
python examples/weather_analysis/run_demo.py
```

预期输出：

```
=================================================================
  通用 Agent 平台 — 天气分析 Demo（非金融领域接入演示）
=================================================================

► 本 Demo 使用与证券分析 Agent 完全相同的 Harness 机制：
  JSONSchemaValidator + SourceAttributionFilter + KeywordBlocker

📍 北京  (30天)
   均温  15.2°C  最高  30.0°C  最低   2.0°C  温差 28.0°C  σ=10.12°C
   趋势 📈 warming  |  北京 近 30 天气温分析：均温 15.2°C…

...

✅  成功分析 5/5 个城市
► 可移植性验证：全部通过
```

## 文件说明

| 文件 | 说明 |
|------|------|
| [weather_agent.py](weather_agent.py) | 天气分析 Agent 核心，含 Schema、Guardrail 注册、分析逻辑 |
| [run_demo.py](run_demo.py) | 命令行 Demo，内置5城市样例数据 |

## 平台可移植性验证

与金融 Agent 对比，接入新领域只需替换：

| 层 | 金融领域 | 天气领域 | 改动量 |
|----|---------|---------|--------|
| Guardrail 机制 | `JSONSchemaValidator` + `KeywordBlocker` | 完全相同 | **0行** |
| 输出 Schema | `ANALYSIS_SCHEMA`（股票指标） | `WEATHER_REPORT_SCHEMA` | 新建 |
| 领域计算 | MA/RSI/MACD 等指标计算 | 均温/温差/趋势计算 | 新建 |
| 数据来源 | AkShare / 样例数据 | 自定义 temps 列表 | 新建 |
| AgentHarness 框架 | 不变 | 不变 | **0行** |

**结论：接入新领域所需工作 ≤ 2 天（主要是 Schema 定义 + 领域计算逻辑）。**

## 架构说明

```
WeatherAnalysisAgent
    └── guardrails: list[Guardrail]
            ├── JSONSchemaValidator(WEATHER_REPORT_SCHEMA)   # 结构校验
            ├── SourceAttributionFilter(["source","updated_at"])  # 数据溯源
            └── KeywordBlocker(["100%准确", "绝对不会", …])       # 违规词过滤

analyze(city, temps, source) → WeatherReport(frozen dataclass)
```

## 输出字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `city` | str | 城市名称 |
| `period_days` | int | 分析天数 |
| `avg_temp_c` | float | 均温(°C) |
| `max_temp_c` | float | 最高温(°C) |
| `min_temp_c` | float | 最低温(°C) |
| `temp_range_c` | float | 温差(°C) |
| `trend` | str | `warming`/`cooling`/`stable` |
| `volatility_c` | float | 温度标准差(°C) |
| `summary` | str | 中文摘要 |
| `source` | str | 数据来源 |
| `updated_at` | str | 分析日期(ISO格式) |
| `disclaimer` | str | 免责声明 |

## 硬性约束

- 所有输出均包含 `disclaimer` 免责声明
- 输出必须通过 `JSONSchemaValidator` Schema 校验
- 禁止出现"100%准确"等绝对化表述（`KeywordBlocker`）
