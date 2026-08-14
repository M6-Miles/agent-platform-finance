# 项目状态看板

更新时间：2026-08-14

本文件是原始任务书“任务看板”的正式状态入口。功能明细以 `checklist.json` 为准，测试结果以实际命令输出为准，历史协作过程保存在 `CLAUDE_CODEX_LOG.md`。

## 当前结论

| 范围 | 状态 | 可核验证据 |
|---|---|---|
| Harness 九大组件 | 已实现并集成 | `SPEC.md`、`Rule/`、`Skill/`、`Workflow/`、`Scripts/`、`MCP/`、`SubAgents/`、`AGENTS.md`、本看板 |
| Loop / Graph | 已实现并集成 | ReAct、记忆、调度、事件钩子、LangGraph、SQLite Checkpoint、人工审批恢复 |
| 证券分析主流程 | 已实现并集成 | 4 个 Specialist Agent、Bull/Bear、Synthesis、Trader、Risk Manager |
| 单笔亏损风控 | 已实现并集成 | 参考价、止损价、止损距离和批准仓位共同计算，预计账户损失不超过 2% |
| 非金融 Demo | 已实现并集成 | 天气 API、Open-Meteo Provider、前端天气页 |
| 20 只股票端到端 | 已达标 | `docs/deliverables_report.md` |
| Sharpe > 0.5 | 未达标 | 样本外结果未稳定超过 0.5，详见 `docs/strategy_comparison.md` |
| Harness Mock 对照 | 已达标 | 固定评测集可量化，但不代表真实 LLM 流量 |
| 真实 LLM 对照 | 新版 100 条已完成，继续积累多日证据 | 标签匹配率 91.0%，违规召回率 100%，正常误报率 0%，Provider 错误率 0% |
| 事实核验与无效调用 | 新口径已完成真实回放 | 固定事实错误 0/40（阻断率因分母为 0 记 N/A）；无效下游动作资格 60→0，不执行真实业务 API |
| 模拟盘运行 1-2 周 | 收集中（1/7） | 2026-08-14 已记录 2 只证券的腾讯公开 live 行情，剩余至少 6 个有效交易日 |
| 全量测试 | 通过 | `1621 collected；1620 passed, 1 skipped, 0 failed`（2026-08-14） |

## 不可伪造约束

- 不修改 Sharpe 公式、阈值或样本来制造达标结果。
- 不把 Mock Harness 指标描述为真实 LLM 生产实验。
- 不用历史回放冒充模拟盘自然运行 1-2 周。
- 不把离线样例或降级数据标记为实时数据。
- 不连接真实券商，不执行真实下单。

## 下一验收动作

1. 每个交易日运行 `Scripts/run_daily_paper_monitor.py`，累计至少 7 个不同的真实交易日。
2. 每日执行同一固定事实评测集；只有观测到真实事实错误时才计算事实错误阻断率。
3. 继续进行严格样本外策略研究；稳健选参挑战方案未改善结果，不替换正式基线。
4. 每次交付前运行 `python -m pytest -q -p no:cacheprovider` 并更新本文件和 `checklist.json`。
