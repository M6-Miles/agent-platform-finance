# AGENTS.md — 项目结构索引（dev-map）

> 每次新会话开始时，先读此文件，快速对齐项目全局状态。

## 项目定位

**通用 Agent 平台 + 证券金融分析应用**  
范式：Harness ⊂ Loop ⊂ Graph（V2.0）  
技术栈：Python 3.11 · FastAPI · Streamlit · SQLite · LangGraph · AkShare

---

## 目录结构

```
project/
├── SPEC.md                    # 项目成功标准（验收 checklist）
├── AGENTS.md                  # 本文件，项目结构索引
├── progress.txt               # 进度日志（人工 + 自动追加）
├── checklist.json             # 功能清单（结构化状态追踪）
│
├── Rule/                      # 行为边界规则
│   ├── no_trade_without_confirmation.md
│   ├── data_must_have_source.md
│   └── no_absolute_profit_claims.md
│
├── Skill/                     # 可复用技能脚本（可注入 Agent 上下文）
│   ├── calculate_indicators.py    # 技术指标计算（MA/EMA/MACD/RSI/KDJ/...）
│   └── fetch_financials.py        # 基本面数据拉取（PE/PB/ROE/三大报表）
│
├── Scripts/                   # 自动化验收脚本（共 15 个 py/js）
│   ├── validate_schema.py         # 输出 Schema 校验
│   ├── validate_deliverables.py   # 三项交付物验收（A/B/C）
│   ├── run_agent_loop_demo.py     # Loop 五要素可运行演示（自带审计，离线零网络）
│   ├── check_frontend_syntax.js   # 前端内联脚本语法检查（Node）
│   └── run_backtest.py            # 回测执行脚本
│
├── Workflow/                  # 工作流声明式定义（非空，4 个文件）
│   ├── workflow.schema.json           # 工作流 JSON Schema
│   ├── securities_analysis.workflow.json  # 证券投研主工作流
│   ├── weather_analysis.workflow.json     # 天气 Demo（P-05 可移植性证据）
│   └── README.md
│
├── MCP/                       # 外部工具标准化封装（薄委托层 / shim）
│   ├── akshare_tools.py           # 7 个 mcp_get_* → agent_platform.mcp 注册表
│   └── tushare_tools.py           # 6 个 mcp_get_* → agent_platform.mcp 注册表
│
├── SubAgents/                 # 各 Agent 定义卡片
│   ├── technical_agent.md
│   ├── fundamental_agent.md
│   ├── industry_agent.md
│   ├── market_regime_agent.md
│   ├── synthesis_agent.md
│   └── trader_agent.md
│
├── data/
│   ├── app.sqlite3            # 主数据库（sessions / messages / analysis_records）
│   └── sample/prices.csv      # 离线样例数据（DEMO001-003）
│
├── src/agent_platform/
│   ├── core/
│   │   ├── agent_runtime.py       # 早期 Loop 运行时（ReAct，max_steps=4，仍在用）
│   │   ├── agent_loop.py          # Loop 五要素显式实现（规划/工具调用/观察/反思/继续或结束）
│   │   ├── loop_memory.py         # Loop 可持久化记忆（InMemory / SQLite 双实现）
│   │   ├── scheduler.py           # 心跳/定时（可注入时钟，失败隔离且不静默）
│   │   ├── event_hooks.py         # 事件钩子总线（10 个事件常量，监听者故障不拖垮主流程）
│   │   ├── harness.py             # AgentHarness SDK + 5 个 Guardrail
│   │   ├── observability.py       # Agent 与 LangGraph 工作流运行指标
│   │   ├── llm_provider.py        # LLMProvider 协议 + ChatMessage / ModelReply
│   │   ├── mock_llm_provider.py   # 离线 Mock（无需 API Key）
│   │   ├── deepseek_llm_provider.py
│   │   ├── claude_llm_provider.py
│   │   └── tools.py               # ToolRegistry + ToolDescription
│   │
│   ├── finance/
│   │   ├── securities_graph.py    # LangGraph 投研主工作流
│   │   ├── analysis.py            # SecurityAnalysisResult + analyze_security()
│   │   ├── indicators.py          # 技术指标计算（pandas，非 LLM）
│   │   ├── portfolio_analysis.py  # 多股票并行对比
│   │   ├── chart_builder.py       # Plotly 图表工厂
│   │   ├── report_exporter.py     # Excel + HTML 报告导出
│   │   ├── akshare_data_provider.py
│   │   ├── sample_data_provider.py
│   │   └── constants.py           # DISCLAIMER 共享常量
│   │
│   ├── api/main.py                # FastAPI（健康检查、对话、分析、深度投研恢复）
│   ├── ui/streamlit_app.py        # Streamlit 前端（4 个 Tab）
│   ├── storage/sqlite_store.py    # SQLite CRUD
│   ├── services/application_service.py
│   └── config.py                  # Settings（pydantic-settings）
│
└── tests/                     # pytest 单元测试与集成测试
```

---

## 关键入口

| 用途 | 命令 |
|------|------|
| 启动 API 服务 | `uvicorn agent_platform.api.main:app --reload` |
| 启动 Streamlit UI | `streamlit run src/agent_platform/ui/streamlit_app.py` |
| 运行全套测试 | `pytest tests/ -q` |
| 切换 LLM 提供商 | `.env` 中设置 `LLM_PROVIDER=mock\|deepseek\|claude` |

---

## 当前进度

详见 `progress.txt` 和 `checklist.json`。
