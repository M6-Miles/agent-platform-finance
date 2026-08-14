# 交付物验证报告

> 更新时间：2026-08-15。仅供研究参考，不构成投资建议。

本文件给出当前交付状态的简明入口。详细成功标准见 `SPEC.md`，结构化状态见
`checklist.json`，策略原始结果见 `docs/strategy_comparison.md`，真实模型实验见
`docs/experiments/real_llm_replay_deepseek_latest.md`。

## 验收结论

| 验收项 | 当前结果 | 判定 |
| --- | --- | --- |
| Harness、Loop、LangGraph、Checkpoint | 可运行、可恢复、可观测，含插件化 Guardrail | 通过 |
| 四路 Specialist 与综合投研 Graph | 20 只确定性标的端到端完成；高风险结果进入人工复核 | 通过 |
| Trader、Risk Manager、Pre-Flight | 单笔账户风险预算不超过 2%，支持行业集中度、回撤与流动性检查 | 通过 |
| 回测工程能力 | 成本、滑点、印花税、基准、Walk-forward 和样本外统计齐全 | 通过 |
| 样本外 Sharpe > 0.5 | 正式多因子样本外均值约 -0.337 | **未达标** |
| 真实 DeepSeek Harness 回放 | 100 条；标签匹配 91%，违规召回 100%，正常误报 0%，Provider 错误 0% | 已完成离线评测 |
| 真实行情模拟盘自然证据 | 2026-08-14 已记录 000001、600519 的腾讯公开 live 行情 | **收集中：1/7 个有效交易日** |
| 非金融复用 | 天气 Demo 已接入 API 和前端；CMA-GRAPES 优先、Open-Meteo 补全，支持新疆省市区选择并显示逐日模型来源 | 通过 |
| 股票标的一致性 | 六位 A 股代码和数据源标的均校验；`600338` 可分析，`660338` 明确拒绝且不复用其他股票数据 | 通过 |
| 单机部署 | Docker 单 worker、Nginx、可选 HTTPS、SQLite 持久化与每日备份；2026-08-15 已完成保留密钥和业务数据的服务器更新验收 | 通过 |
| 全量测试 | 1663 项收集，1662 passed，1 skipped，0 failed | 通过 |

## 证据边界

- 真实 DeepSeek 回放是固定评测集，不等于真实生产用户流量；事实错误样本分母为 0 时，事实错误阻断率必须记为 N/A。
- 模拟盘只使用本地 `MockBroker`，不会连接券商或执行真实下单。
- 真实行情证据必须按自然交易日积累；同一天重复运行、离线数据、降级数据和非交易日不会增加有效天数。
- Sharpe 公式、年化方式、无风险利率、0.5 阈值和 MA baseline 均未为达标而修改。

## 复现命令

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m pyflakes src Scripts tests
.\.venv\Scripts\python.exe -m compileall -q src Scripts tests
```

联网功能依赖外部服务可用性和本机网络。默认离线测试不调用真实 LLM、不产生费用，也不连接真实交易接口。

部署时使用 `deploy/install_demo.sh`。`.env`、`.env.production`、`.venv/`、数据库、日志和缓存不是交付源代码，不得进入部署压缩包或覆盖服务器现有数据。
