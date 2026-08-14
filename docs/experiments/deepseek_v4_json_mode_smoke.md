# 真实 LLM Harness ON/OFF 离线回放实验报告

**生成时间**: 2026-08-14 09:58:34

---

## 实验概览

- **实验类型**: real_llm_offline_replay
- **Provider 类型**: real
- **状态**: completed
- **Provider**: deepseek/deepseek-v4-flash
- **Model**: deepseek-v4-flash
- **样本数**: 3

## 聚合指标

| 指标 | 值 |
|------|----|
| 成功数 | 1 |
| Schema 错误数 | 0 |
| 拦截数 | 2 |
| 错误数 | 0 |
| Provider 错误数 | 0 |
| 成功率 | 33.3% |
| Schema 错误率 | 0.0% |
| Guardrail 拦截率 | 66.7% |
| Provider 错误率 | 0.0% |
| 平均延迟 | 5.75s |
| P50 延迟 | 3.65s |
| P95 延迟 | 11.08s |
| P99 延迟 | 11.08s |
| 平均输入 Token | 230 |
| 平均输出 Token | 436 |
| 总输入 Token | 689 |
| 总输出 Token | 1308 |
| 人工审核率 | 0.0% |
| 总重试次数 | 0 |
| 重试率 | 0.0% |
| Harness Token 增量 | 0 |

## 任务详情

| Task ID | OFF 状态 | ON 状态 | 延迟(s) | Token | 重试 | 错误类型 |
|---------|---------|---------|--------|-------|------|----------|
| REAL-001 | success | passed | 11.08 | 226+975 | 0 |  |
| REAL-002 | success | blocked | 3.65 | 226+219 | 0 |  |
| REAL-003 | success | blocked | 2.51 | 237+114 | 0 |  |

---

## 重要说明

### 实验类型区分

- **Mock 固定评测**: 不调用真实 LLM，使用构造性样本
- **simulated 测试**: 测试用 Fake Provider，不代表真实模型
- **real_llm_offline_replay**: 调用真实 LLM（本报告）
- **production_traffic**: 真实生产流量（尚未开展）

### Guardrail 拦截率说明

- 无人工标签时，不能称为 'hallucination_rate' 或 'false_positive_rate'
- 当前指标名称: guardrail_block_rate（Harness 拦截率）
- 代表 Harness 判定为违规的比例，不等同于模型幻觉率

### 样本量限制

- 本实验样本量有限，不代表大规模生产流量表现
- 真实生产环境的拦截率、误报率需要长期观测

### Sharpe 指标

- Sharpe 指标仍按原公式和 0.5 阈值评估
- 本实验不影响 Sharpe 计算

### 敏感信息

- 所有 API Key、邮箱、手机号等敏感信息已脱敏
