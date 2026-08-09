# Rule: 禁止无人工确认下单

**ID**: rule-001  
**优先级**: CRITICAL  
**适用范围**: 所有涉及资金操作的 Agent

## 规则内容

任何仓位比例超过 10% 的交易信号，**必须** 经过人工确认后方可执行。

## 触发条件

- `signal.position_pct > 0.10`
- 或 `signal.action in ["buy", "sell"]` 且未携带 `human_approved=True`

## 执行动作

1. `TradingHarness.pre_flight_check()` 检测到违规时，抛出 `HumanApprovalRequired`
2. 系统暂停执行，等待人工通过 `/approve` 指令确认
3. 审计日志记录：时间戳、信号内容、操作人

## 合规示例

```python
# ✅ 合规
signal = TradeSignal(symbol="600519", action="buy", position_pct=0.05)

# ❌ 违规 —— 触发人工审批门
signal = TradeSignal(symbol="600519", action="buy", position_pct=0.15)
```

## 豁免条件

- 止损单（`signal.is_stop_loss=True`）在回撤 > 15% 时可自动执行
- 仓位清零（`position_pct=0`）无需确认
