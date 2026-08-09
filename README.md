# 通用 Agent 平台及证券金融分析应用（本地演示版）

这是一版面向小白的本地演示项目：你可以在自己的电脑上启动一个简单的 Agent 应用，用内置样例证券数据完成行情分析和问答演示。

> 重要提醒：本项目默认使用 Mock Agent 和内置样例数据，不调用真实大模型、不访问真实证券数据源、不产生 API 费用。**可选地**，你可以启用 AkShare 接入 A 股真实日线数据（需联网，免费）。所有分析仅供研究参考，不构成投资建议，也不支持自动下单或交易。

## 1. 你将得到什么

### Web 应用
- **Streamlit 可视化页面**（三主标签页）：📈 证券分析 · 📊 多股对比 · 💬 Agent 对话
- **FastAPI 后端**：健康检查、证券分析、会话管理、分析历史、聊天接口
- **Mock Agent**：离线可预测，不调用任何外部大模型
- **侧边栏**：数据源切换、会话管理（新建/重命名/删除）、历史记录

### 数据源
- **内置样例数据**（默认）：`DEMO001`–`DEMO004`（演示用）+ `TEST001`–`TEST020`（验收用），完全离线，零配置。
  由 `Scripts/generate_sample_data.py` 确定性生成（固定整数种子，跨进程逐字节一致）。
- **AkShare A 股日线**（可选）：Stooq 前复权数据，免费，需联网

### 技术指标（全部同时计算）
- **趋势类**：MA5、MA20、EMA、布林带（上/中/下轨 + 位置百分比）
- **动量类**：MACD（DIF/DEA/柱状图）、RSI(14)、KDJ(K/D/J)
- **波动类**：ATR(14)、CCI(20)、年化波动率
- **收益类**：区间收益率、最大回撤、Sharpe 比率
- **图表**：K线蜡烛图 + MA双线 + 布林带区域、MACD柱状图、KDJ三线、RSI超买/超卖带

### 多股对比
- 同时分析 1-10 只股票（5线程并行拉取，< 5秒出结果）
- 归一化收益率曲线对比、日收益率相关性热力图、指标对比表（绿色高亮最优项）

### 报告导出
- **Excel**（两Sheet：分析摘要 + 完整行情数据）
- **HTML**（含4张Plotly交互图表的自包含报告）

### 会话与历史
- SQLite 本地存储，刷新后会话/消息/分析历史不丢失
- 会话重命名和删除功能
- 连续对话上下文（Agent 加载历史消息）

### 测试覆盖
- **358 项单元测试**，覆盖核心模块（数据库、Agent、指标、API、数据提供、组合分析、报告导出、Mock LLM、工具注册、Harness Guardrail、Graph 引擎、专业 Agent、回测、Evaluator、MockBroker、P-05 非金融 Demo）

## 2. 项目不会做什么

- 不提供任何投资建议。
- 不连接真实券商账户。
- 不自动下单或交易。
- 不默认调用 Claude、OpenAI 或其他收费大模型。
- 不默认访问商业数据接口（AkShare 使用 Stooq 公开免费接口，按需启用）。
- 导出的报告仅供参考，不构成投资建议。

## 3. 准备软件

请先安装：

1. Python 3.11 或更高版本。
2. Git Bash、PowerShell 或 Windows 终端任选一个。

检查 Python：

```bash
python --version
```

正常情况下会看到类似：

```text
Python 3.11.7
```

如果提示找不到 `python`，请先安装 Python，并勾选“Add Python to PATH”。

## 4. 进入项目目录

在终端执行：

```bash
cd "/d/study/mzx/项目/东方国信/构建通用Agent平台及证券金融分析应用/project"
```

如果你使用 PowerShell，可执行：

```powershell
cd "D:\study\mzx\项目\东方国信\构建通用Agent平台及证券金融分析应用\project"
```

## 5. 创建虚拟环境

> 请先确认你使用的是哪一种终端。PowerShell 使用 `Activate.ps1`，Git Bash 才使用 `source`；不要把两种命令混用。VS Code 终端提示符以 `PS` 开头时，说明你正在使用 PowerShell。

Git Bash：

```bash
python -m venv .venv
source .venv/Scripts/activate
```

PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

激活成功后，命令行前面通常会出现 `(.venv)`。如果你已经看到 `(.venv)`，就不需要再次激活，可以直接运行 `pytest` 或启动服务。

如果同时看到 `(.venv) (base)`，表示项目虚拟环境和 Conda base 提示同时存在。只要 `python -c "import sys; print(sys.prefix)"` 的输出路径以项目的 `.venv` 结尾，项目环境就是正确的。

如果 PowerShell 提示脚本不能运行，可临时执行：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

## 6. 安装依赖

```bash
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

正常情况下会安装 FastAPI、Streamlit、pandas、pytest 等依赖。

如果安装慢或失败，通常是网络问题；可以稍后重试，或使用公司/学校允许的镜像源。

### 6.1 可选：安装 AkShare 以接入真实 A 股数据

如果你希望在 Streamlit 页面中切换到真实 A 股日线数据（免费、需联网），请执行：

```bash
python -m pip install -e ".[akshare]"
```

### 6.2 可选：安装 LLM 依赖以接入 DeepSeek 或 Claude

如果你希望使用 DeepSeek 或 Claude 真实大模型替代离线 Mock Agent，请执行：

```bash
python -m pip install -e ".[llm]"
```

这会额外安装 `openai`、`anthropic`、`python-dotenv`（约 5～10 MB）。

### 6.3 可选：安装 Tushare 以接入专业数据源

Tushare 提供更全面的 A 股数据（需注册账号获取 Token，部分接口免费）：

```bash
python -m pip install -e ".[tushare]"
```

获取 Token 后，在 `.env` 中添加：

```
TUSHARE_TOKEN=你的Token
```

或在调用时通过环境变量传入：

```bash
TUSHARE_TOKEN=你的Token python Scripts/validate_deliverables.py --online --tushare-token 你的Token
```

**一次安装所有可选依赖：**

```bash
python -m pip install -e ".[akshare,tushare,llm]"
```

**注意**：
- AkShare 是第三方开源项目，提供免费的 A 股行情接口，无需 Token。
- 首次调用会联网获取证券列表，约需 1～3 秒。
- 如果网络不稳定或 AkShare 服务临时不可用，Streamlit 页面会显示明确错误，你可以点击按钮切换回内置样例数据。
- 不安装 AkShare 也可以正常使用项目（仅限内置样例数据）。
- Tushare Token 不要提交到代码仓库；仅通过 `.env` 或环境变量传入。

## 7. 准备环境变量

复制示例文件并按需修改：

Git Bash：

```bash
cp .env.example .env
```

PowerShell：

```powershell
Copy-Item .env.example .env
```

默认 `.env` 内容（无需修改即可运行 Mock 离线模式）：

```
LLM_PROVIDER=mock
MARKET_DATA_PROVIDER=sample
```

### 7.1 可选：启用 DeepSeek 大模型

1. 前往 [platform.deepseek.com](https://platform.deepseek.com) 申请 API Key（首次注册有免费额度）。
2. 编辑 `.env`，填入你的 Key（**不要引号**，不要有空格）：

```
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=你的密钥
```

3. 保存后重启 Streamlit 即可。

### 7.2 可选：启用 Claude 大模型

1. 前往 [console.anthropic.com](https://console.anthropic.com) 申请 API Key。
2. 编辑 `.env`：

```
LLM_PROVIDER=claude
ANTHROPIC_API_KEY=你的密钥
```

### ⚠️ 安全注意事项

- **`.env` 文件已在 `.gitignore` 中，不会被提交到代码仓库。**
- 永远不要把 API Key 直接写入代码文件或提交到 Git。
- 不要把 `.env` 文件发给他人或上传到网络。
- 如果 Key 泄露，请立即在相应平台重置。

本项目使用 Python 内置 SQLite，不需要另外安装数据库。首次启动 API 或页面时会自动创建：

```text
data/app.sqlite3
```

它只保存在你的电脑上，用于存放聊天会话和分析历史。

## 8. 运行测试

```bash
# 标准离线测试（默认；无需网络）
python -m pytest -q -p no:cacheprovider

# 仅运行 LangGraph 工作流测试（14 项）
python -m pytest -q -p no:cacheprovider tests/test_langgraph_workflow.py

# 排除慢速测试
python -m pytest -q -m "not slow"
```

正常情况下应看到全部通过：

```text
1220 passed, 1 skipped, 1 warning
```

> 该 warning 来自 `fastapi/testclient.py` 的 `StarletteDeprecationWarning`（第三方库，
> 非本项目代码）；1 项 skipped 为需要真实网络的联网用例。

需要真实网络的测试带 `@pytest.mark.online` 标记，不在默认运行集内：

```bash
# 联网测试（需安装 akshare/tushare，并设置 TUSHARE_TOKEN）
python -m pytest -q -m "online"
```

如果测试失败，先确认：

- 已经进入项目根目录。
- 虚拟环境已经激活。
- 已经执行 `python -m pip install -e ".[dev]"`。

### 8.1 离线 / 在线交付物验证

```bash
# 离线验证（无需网络）——使用 SampleMarketDataProvider
python Scripts/validate_deliverables.py --offline

# 在线验证（需 AkShare 联网；可选 Tushare Token）
python Scripts/validate_deliverables.py --online
python Scripts/validate_deliverables.py --online --tushare-token $TUSHARE_TOKEN
```

离线验证运行 ≥20 只股票的完整投研流程，输出四类计数（execute / manual_review / block / error），
完整报告写入 `docs/deliverables_report.md`。

## 9. 启动 FastAPI 后端

```bash
uvicorn agent_platform.api.main:app --reload
```

正常情况下会看到类似：

```text
Uvicorn running on http://127.0.0.1:8000
```

打开浏览器访问：

- 健康检查：http://127.0.0.1:8000/health
- 接口文档：http://127.0.0.1:8000/docs

如果端口 8000 被占用，可改用：

```bash
uvicorn agent_platform.api.main:app --reload --port 8010
```

停止后端：在终端按 `Ctrl + C`。

## 10. 启动 Streamlit 页面

另开一个终端，进入同一个项目目录并激活虚拟环境，然后执行：

```bash
streamlit run src/agent_platform/ui/streamlit_app.py
```

正常情况下浏览器会打开：

```text
http://localhost:8501
```

如果端口 8501 被占用，可执行：

```bash
streamlit run src/agent_platform/ui/streamlit_app.py --server.port 8502
```

停止页面：在终端按 `Ctrl + C`。

## 11. 跑一个证券分析示例

在 Streamlit 页面中：

1. 选择 `DEMO001` 或 `DEMO002`。
2. 选择开始日期和结束日期。
3. 点击“运行分析”。
4. 查看最新收盘价、5 日均线、区间收益率和最大回撤。
5. 展开“查看表格数据（无障碍/复核用）”可以看到原始样例数据。
6. 在左侧“本地历史”中可以看到最近的分析记录。

你应该能看到：

- 数据来源：内置样例数据。
- 更新时间：2026-07-29。
- 风险提示：仅供研究参考，不构成投资建议。

## 12. 跑一个 Agent 问答示例

在 Streamlit 页面底部输入：

```text
请分析 DEMO001
```

按回车发送给 Agent。

Mock Agent 会调用本地证券分析工具，并返回一段分析说明。展开“查看 Agent 工具调用过程”可以看到工具执行结果。你可以继续发送下一条消息，当前会话会保存在 SQLite 中；刷新页面后，从左侧“最近会话”重新选择即可查看历史。点击“新建会话”可以开始一段独立对话。

也可以通过 FastAPI 接口测试：

```bash
curl -X POST "http://127.0.0.1:8000/chat" \
  -H "Content-Type: application/json" \
  -d '{"message":"请分析 DEMO002"}'
```

## 13. 常见问题

### 13.1 `python` 找不到

说明 Python 没装好或没加入 PATH。重新安装 Python，并勾选 Add Python to PATH。

### 13.2 `pytest` 找不到

请确认虚拟环境已激活，并重新安装依赖：

```bash
python -m pip install -e ".[dev]"
```

### 13.3 端口被占用

FastAPI 默认端口是 8000，Streamlit 默认端口是 8501。可以按上文改端口。

### 13.4 中文路径导致问题

本项目路径包含中文。大多数情况下可以正常运行。如果某个工具报路径问题，可以把项目复制到较短的英文路径，例如：

```text
D:\projects\agent-platform-finance-demo
```

### 13.5 页面打开但没有数据

请确认文件存在：

```text
data/sample/prices.csv
```

并确认你是在项目根目录启动 Streamlit。

### 13.6 查看 SQLite 文件是否已经创建

PowerShell：

```powershell
Get-Item .\data\app.sqlite3
```

Git Bash：

```bash
ls -l data/app.sqlite3
```

### 13.7 重置本地聊天和分析历史

> 警告：下面的操作会永久删除本机聊天会话和分析历史。先确认不再需要这些记录，并停止 FastAPI 和 Streamlit。

PowerShell：

```powershell
Remove-Item .\data\app.sqlite3
```

Git Bash：

```bash
rm data/app.sqlite3
```

下次启动时，应用会自动创建一个新的空数据库。内置行情文件 `data/sample/prices.csv` 不会被删除。

## 14. 后续可扩展方向

第一版故意保持简单。后续可以逐步增加：

- 接入真实 Claude 或其他大模型。
- 接入 AkShare、Tushare 或企业数据源。
- 增加 MCP 工具。
- 增加 PostgreSQL、登录、权限和多用户。
- 增加 Docker 部署。
- 增加更多金融指标和报告生成。

这些扩展会涉及真实 API Key、网络访问、可能的费用和更多安全要求，需要单独确认后再做。

---

## 15. Harness Engineering V2.0 核心组件（进阶）

> 本节面向需要深入了解 Agent 平台架构的读者。

### 15.1 AgentHarness SDK

`src/agent_platform/core/harness.py` 实现了完整的 Harness 免疫系统，包含 5 个内置 Guardrail：

| Guardrail | 功能 |
|-----------|------|
| JSONSchemaValidator | 强制 Agent 输出符合预定义 JSON Schema |
| SourceAttributionFilter | 确保数据带 source / updated_at 字段 |
| RateLimiter | 限流（默认 20 次/分钟） |
| KeywordBlocker | 拦截违禁词（绝对稳赚、100%收益等） |
| CrossValidator | 交叉验证多字段一致性 |

CircuitBreaker（熔断器）：连续失败 N 次后自动开路，防止雪崩。

### 15.2 工作流编排引擎

#### 主引擎：LangGraph（生产入口）

`src/agent_platform/finance/securities_graph.py` — **证券分析主工作流的生产编排引擎**，
基于 [LangGraph](https://github.com/langchain-ai/langgraph) `StateGraph` 实现。

工作流结构（START → 并行 → 汇合 → 条件路由）：

```
START
  ├─ technical_agent      ┐
  ├─ fundamental_agent    │ 并行（四路独立执行）
  ├─ industry_agent       │
  └─ market_regime_agent  ┘
          ↓ 四路汇合（全部完成后才触发）
    synthesis_agent
          ↓ 条件路由
    置信度 ≤ 0.3 → END（no_trade，跳过交易）
    置信度 > 0.3 → trader_agent
                        ↓
              [HumanApprovalRequired] → human_approval（interrupt 暂停）
              [正常]                  → risk_manager
                                            ↓
                                    trading_harness（Pre-Flight）
                                            ↓ 条件路由
                            execute       → END（可执行）
                            manual_review → human_approval（interrupt 暂停）
                            block         → END（阻断）
```

**Checkpoint 与状态恢复**：

```python
from agent_platform.finance.securities_graph import build_securities_graph, run_securities_analysis
from langgraph.checkpoint.memory import MemorySaver

# 构建带 checkpoint 的图
cp = MemorySaver()
g = build_securities_graph(checkpointer=cp)

# 运行并指定 thread_id 以支持恢复
state = run_securities_analysis("600519", thread_id="my-analysis-001", graph=g)

# 查询同一个 thread_id 的历史状态
config = {"configurable": {"thread_id": "my-analysis-001"}}
snap = g.get_state(config)
print(snap.values["final_action"])   # execute / manual_review / block
```

**人工审批流程（`HumanApprovalRequired` / `manual_review`）**：

当仓位超过 10% 或 Pre-Flight 返回 `manual_review` 时，工作流通过 `interrupt()` 暂停，
等待外部决策：

```python
from langgraph.types import Command
from agent_platform.finance.securities_graph import resume_securities_analysis

# 暂停后查询状态
snap = g.get_state(config)
print(snap.next)      # 非空表示还有待执行步骤（处于 interrupt 状态）
print(snap.interrupts)  # 显示中断原因

# 批准 → 继续执行后续流程（risk_manager → trading_harness → execute）
result = g.invoke(Command(resume="approve"), config=config)

# 拒绝 → 进入 block 状态
result = g.invoke(Command(resume="reject"), config=config)

# 或使用便捷函数
result = resume_securities_analysis("approve", thread_id="my-analysis-001", graph=g)
```

### 15.3 专业分析 Agent

| Agent | 文件 | 输出 |
|-------|------|------|
| 技术分析 | `analysis.py` + `indicators.py` | MA/EMA/MACD/RSI/KDJ/BB/ATR/CCI |
| 基本面 | `fundamental_agent.py` | PE/PB/ROE/DCF 估值区间 |
| 行业 | `industry_agent.py` | 景气度（booming/normal/sluggish）+ 龙头排序 |
| 大盘宏观 | `market_regime_agent.py` | Market Regime（bull/bear/consolidation） |
| 综合研判 | `synthesis_agent.py` | Bull/Bear 辩论 + 置信度（0.0–1.0）+ 买卖信号 |
| 交易员 | `trader_agent.py` | 目标价 + 仓位建议（≤10%，>10% 需人工审批） |
| 风控 | `risk_manager_agent.py` | 单仓≤2%、行业集中≤30%、回撤>15% 强制减仓 |

### 15.4 Pre-Flight Checklist（交易前门卫）

`src/agent_platform/finance/trading_harness.py` 实现 6 项前置检查：

1. 数据溯源（source / updated_at 完整性）
2. 违禁词拦截
3. 仓位合规（approved ≤ suggested）
4. Schema 有效性
5. 置信度阈值（默认 ≥ 0.55）
6. 回撤保护（final_signal ≠ "reduce"）

### 15.5 工程层（Phase 4）

| 组件 | 文件 | 说明 |
|------|------|------|
| 回测引擎 | `finance/backtesting.py` | Sharpe / 最大回撤 / 胜率 / 滑点 0.1% + 佣金 0.03% |
| 可观测面板 | `core/observability.py` | Token 消耗 / 延迟 P50/P95 / Guardrail 触发率 |
| Evaluator Agent | `core/evaluator_agent.py` | 数据完整性 + 逻辑一致性 + 违禁词 三维评分 0–100 |
| MockBroker | `finance/mock_broker.py` | 本地纸面交易：限价单 / 市价单 / 撮合 / 持仓盈亏 |
| 幻觉率实验 | `core/harness_experiment.py` | Harness ON vs OFF 对比：幻觉拦截率 100%，误拦截率 0% |

### 15.6 快速运行回测

```bash
# 使用内置样例数据（离线，零配置）
python Scripts/run_backtest.py

# 指定标的 + 资金
python Scripts/run_backtest.py --symbol DEMO002 --capital 500000

# 查看可用样例标的
python Scripts/run_backtest.py --list
```

> 内置样例数据是**合成序列**，用于验证引擎链路与离线演示，其回测数值不代表策略在真实市场
> 的表现。`docs/deliverables_report.md`「验收 B」同样跑在这份合成集上，**不是真实行情结果**。
> 真实行情实测见 `SPEC.md §3.1`（结论）与以下原始输出：
> `data/real/measure_10y_result.txt`（十年窗口）、
> `data/real/validate_rolling_oos_result.txt`（滚动窗口 + 样本外）、
> `data/real/measure_survivorship_result.txt`（存活者偏差）。
> 实测结论：E-01「夏普 > 0.5」**未达标**，留出段夏普为 −0.035。

### 15.6.1 重新生成样例数据

样例行情由确定性生成器产出，不依赖 `PYTHONHASHSEED`，任何机器上结果逐字节一致：

```bash
# 缺失时生成（已完整则跳过）
python Scripts/generate_sample_data.py

# 强制重新生成
python Scripts/generate_sample_data.py --force

# 校验确定性（同进程 + 跨进程双重比对 SHA256）
python Scripts/generate_sample_data.py --verify
```

数据集：`DEMO001`–`DEMO004`（演示）+ `TEST001`–`TEST020`（验收），252 个交易日，
起始 2025-01-02，输出到 `data/sample/prices.csv`。

### 15.7 硬性安全约束

- **禁止无人工确认下单**：仓位 > 10% 的信号强制抛出 `HumanApprovalRequired` 异常
- **数据必须带 source 字段**：所有 Agent 输出通过 `SourceAttributionFilter` 校验
- **免责声明**：所有分析输出包含"仅供研究参考，不构成投资建议"
- **API Key 不入代码**：DEEPSEEK_API_KEY / ANTHROPIC_API_KEY 只存 `.env`，已在 `.gitignore`
- **不连接真实券商**：MockBroker 为纯本地撮合，严禁接入实盘接口

### 15.8 非金融领域 Demo（P-05 可移植性验证）

`examples/weather_analysis/` 演示同一套 Harness 机制在非金融领域（天气分析）的零改动接入：

```bash
python examples/weather_analysis/run_demo.py
```

| 层 | 金融领域 | 天气领域 | 改动量 |
|----|---------|---------|--------|
| Guardrail 机制 | `JSONSchemaValidator` + `KeywordBlocker` | 完全相同 | **0 行** |
| 输出 Schema | `ANALYSIS_SCHEMA`（股票指标） | `WEATHER_REPORT_SCHEMA` | 新建 |
| 领域计算 | MA/RSI/MACD 等 | 均温/温差/趋势 | 新建 |
| AgentHarness 框架 | 不变 | 不变 | **0 行** |

接入新领域所需工作 ≤ 2 天。25 项专项测试：`tests/test_p05_weather_demo.py`

### 15.9 HTML 前端原型（frontend_prototype.html）

`frontend_prototype.html` 是一个**零依赖的单文件前端原型**，用 Tailwind CDN 实现，
不需要 Node / npm / 构建步骤，直接双击或用浏览器打开即可。它与 Streamlit 页面功能对应，
包含 7 个视图：证券分析 / 多股对比 / Agent 对话 / 深度投研 / 策略回测 / 模拟盘 / 可观测性。

两种数据模式（侧边栏切换）：

| 模式 | 说明 | 是否需要后端 |
|------|------|-------------|
| 内置样例（离线） | 使用页面内置的演示数据，零配置 | 否 |
| AkShare A股（联网） | 调用本地 FastAPI，取真实 A 股行情 | **是** |

选「AkShare A股（联网）」时需先启动后端，且端口必须与页面中的 `API_BASE` 一致
（默认 `http://localhost:8002`）：

```bash
.venv\Scripts\python.exe -m uvicorn src.agent_platform.api.main:app --host 127.0.0.1 --port 8002
```

后端不可达时页面不报错，而是回退到内置演示数据并给出黄色提示。

> 已知限制：请勿加 `--reload`。`--reload` 下 uvicorn 用 `sys.executable` spawn worker
> 进程，在部分 Windows 环境会解析到系统 Python 而非 `.venv`，导致 `/quote/{symbol}`
> 返回 503。不加 `--reload` 可避免。
