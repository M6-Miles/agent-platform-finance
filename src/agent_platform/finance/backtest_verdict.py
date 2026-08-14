"""回测夏普判定的展示无关领域逻辑。"""
from __future__ import annotations

import math
from dataclasses import dataclass

from agent_platform.finance.backtesting import BacktestResult


@dataclass
class BacktestSharpeStats:
    """回测判定所需的夏普统计摘要。"""

    sharpe: float
    n_obs: int
    std_error: float
    ci_low: float
    ci_high: float
    threshold_in_ci: bool
    verdict: str


def backtest_sharpe_verdict(
    result: BacktestResult,
    threshold: float = 0.5,
) -> tuple[BacktestSharpeStats, str, str]:
    """按日历口径夏普及其置信区间返回判定和用户提示。"""
    sharpe = float(result.sharpe_calendar)
    n_obs = max(len(result.equity_curve) - 1, 0)

    if n_obs < 2:
        std_error = float("inf")
        ci_low, ci_high = float("-inf"), float("inf")
    else:
        std_error = math.sqrt(252.0 / n_obs)
        ci_low = sharpe - 1.96 * std_error
        ci_high = sharpe + 1.96 * std_error

    threshold_in_ci = ci_low <= threshold <= ci_high
    if ci_low > threshold:
        level = "pass"
        verdict = f"CI 下界 {ci_low:.3f} 高于阈值 {threshold}，达标"
        message = (
            f"夏普（日历口径）{sharpe:.3f} 的 95% CI 下界 {ci_low:.3f} > {threshold}，"
            "样本支持达标。注意：不可外推为策略整体达标，本区间仅覆盖所测区间。"
        )
    elif ci_high < threshold:
        level = "fail"
        verdict = f"CI 上界 {ci_high:.3f} 低于阈值 {threshold}，显著低于目标"
        message = (
            f"夏普（日历口径）{sharpe:.3f} 的 95% CI 上界 {ci_high:.3f} < {threshold}，"
            f"显著低于目标阈值 {threshold}。"
        )
    else:
        level = "inconclusive"
        verdict = f"CI [{ci_low:.3f}, {ci_high:.3f}] 覆盖阈值 {threshold}，无法区分"
        message = (
            f"夏普（日历口径）{sharpe:.3f}，95% CI [{ci_low:.3f}, {ci_high:.3f}]"
            f" 覆盖阈值 {threshold}。样本太短看不出来是否达标（SE={std_error:.3f}）。"
        )

    stats = BacktestSharpeStats(
        sharpe=sharpe,
        n_obs=n_obs,
        std_error=std_error,
        ci_low=ci_low,
        ci_high=ci_high,
        threshold_in_ci=threshold_in_ci,
        verdict=verdict,
    )
    return stats, level, message
