# Claude 最终整改任务：严格满足原始 Word 项目说明书

你是本项目的实现工程师。必须以项目外部原始说明书“构建通用Agent平台及证券金融分析应用.docx”为最高验收依据，严格完成全部细节要求。只允许读取、修改、创建或删除项目目录内文件，不得修改项目外文件、系统设置、IDE 配置、全局包、用户资料，不得读取或输出 .env 密钥，不得真实交易、付费或调用危险服务。所有沟通、代码注释和最终报告使用中文。

## 一、先审计再修改

先读取 README.md、SPEC.md、AGENTS.md、checklist.json、progress.txt、Rule/、Skill/、Workflow/、MCP/、SubAgents/、src/、tests/、Scripts/ 和 examples/weather_analysis/，结合原始 Word 说明书建立逐项差距表。不要因为 SPEC.md 已标记通过就直接相信，必须以真实代码、测试和运行结果为准。

## 二、必须补齐的缺口

### 1. Harness / Loop / Graph

- 保留 LangGraph 作为证券主工作流编排引擎，同时确保具备说明书要求的 DAG、并行、条件边、checkpoint、重试、超时和失败可见能力。
- 补齐 Loop 说明书要求：规划、工具调用、观察、反思、继续规划/结束；完善可持久化记忆。
- 对心跳/定时、事件钩子、目标循环等能力，如果实现则必须是项目内可测试的最小可靠实现；不能只写文档或假接口。
- Workflow 目录不能只是空目录。补充可执行的工作流定义、schema 或明确的 LangGraph 工作流说明，并加入测试。
- 保留并验证 AgentHarness 及 JSONSchemaValidator、SourceAttributionFilter、RateLimiter、KeywordBlocker、CrossValidator 五类 Guardrail。
- 保留天气 Demo 作为 P-05/Bonus 可迁移性证明，并确保其测试通过。

### 2. MCP 工具层

- 将 AkShare/Tushare 工具统一封装为可被主业务调用的 MCP 工具层。
- 至少覆盖历史行情、日/周/分钟行情或明确说明不支持的边界、实时行情、资金流向、财务三大报表、PE/PB/PS/ROE/资产负债率、指数/行业数据。
- 所有工具结果必须带 source、updated_at/timestamp 和明确错误字段。
- 不能只保留未被任何主链路调用的工具文件；至少让相关 Provider/Agent 通过统一工厂或适配器使用这些工具。
- 离线模式必须完全禁止外网，auto 模式才允许真实 Provider；失败时不得伪造价格。

### 3. 四个 Specialist Agent

- 技术分析：结构化输出并通过 Schema，指标必须由确定性代码计算。
- 基本面分析：必须补齐 PE、PB、ROE、资产负债率等现有指标，并实现真实可解释的 DCF 估值字段、公式、输入假设、边界校验和测试；不能用固定常数冒充 DCF。
- 行业分析：景气度、资金流和龙头排序。
- 市场/宏观 Agent：Market Regime、指数和风险偏好。
- 所有结果带 source、updated_at、data_status、fallback_reason 和免责声明。

### 4. Bull/Bear 综合研判

- 将当前简单打分升级为至少 2 轮结构化 Bull/Bear 辩论：Claim -> Evidence -> Reasoning -> Rebuttal。
- 明确每轮输入输出 Schema、引用的数据证据、反方回应和最终 Synthesis。
- 增加 Consistency Check 和 Bias Detector；发现引用矛盾或只引用单边证据时必须标记或阻断。
- 最终输出 signal、confidence、bull_arguments、bear_arguments、reasoning、source、updated_at、disclaimer。

### 5. Trader / Risk / 模拟盘

- 保持仓位超过 10% 必须人工审批。
- Risk Manager 继续执行单笔风险、行业集中度、总回撤、流动性、止损止盈检查。
- TradingHarness 必须在真实建议仓位不超过风控批准仓位时通过。
- MockBroker 只能是本地模拟撮合，不得接入真实券商。
- 增加或补齐连续多交易日模拟盘运行验收；真实行情不可用时必须明确标记 fallback/unavailable。

### 6. 回测与 Sharpe

- 不修改原始 Sharpe 定义以迎合阈值，不注入正漂移、不挑选有利样本、不伪造结果。
- 修复策略或信号质量，使说明书要求的 Sharpe > 0.5 在真实可复现数据和样本外检验中成立；同时报告置信区间、交易成本、最大回撤、胜率、未来函数检查和存活者偏差。
- 如果真实数据下仍不能达到要求，不得谎报通过，必须明确标记未达标并返回可审计证据。

### 7. 交付与可复现性

- 补齐 Git 仓库初始化和至少一个清晰的本地提交，提交前确认 .env、密钥、数据库敏感数据和缓存不在提交中。
- 保留 JSON 功能清单、progress.txt 和 Git 三位一体记录。
- 更新 README、SPEC、AGENTS 和交付文档中的测试数量、端口、API、LangGraph、数据模式和真实限制，删除过期数字。
- 只保留有用途的文档和日志，临时输出、缓存、旧报告不得混入交付目录。

## 三、不可违反的安全和真实性约束

- 不得删除有效测试来“通过”验收。
- 不得降低 Guardrail、关闭人工审批、吞异常或把错误伪装成成功。
- 不得把样例数据标成实时数据。
- 不得把网络失败时的随机值或固定值标成真实行情。
- 不得修改 .env 或输出其中任何密钥。
- 不得真实下单、连接券商实盘、付费或访问项目外路径。

## 四、必须执行的验证

完成修改后执行并保留结果：

1. .venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
2. .venv\Scripts\python.exe -m pyflakes src Scripts tests
3. .venv\Scripts\python.exe -m compileall -q src Scripts tests
4. Scripts/check_frontend_syntax.js
5. 离线 20 股端到端验收，确认零网络调用
6. auto 实时行情接口验收；无法联网时必须记录明确失败原因
7. DCF、Bull/Bear 多轮辩论、MCP 路由、人工审批、SQLite checkpoint、模拟盘、回测和天气 Demo 专项测试
8. 检查项目外无文件被修改，检查 .env 未进入 Git

## 五、最终报告格式

最终只用中文报告：

- 修改文件清单及每个文件的原因
- 原始说明书 P-01 至 P-05、A-01 至 A-07、E-01 至 E-06 的逐项状态
- 每项的代码证据、测试名称和命令结果
- 实际通过、部分完成、未完成项目必须分开列出
- 真实网络、真实浏览器、Sharpe、模拟盘连续运行等无法完成的验收必须如实说明
- 不得使用“全部完成”作为没有证据的结论
