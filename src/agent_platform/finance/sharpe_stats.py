"""
夏普比率的统计显著性（Sharpe Statistics）
==========================================
给夏普比率配上标准误、置信区间与假设检验。

为什么需要这个模块
------------------
夏普比率是**样本估计量**，不是常数。只报单点值会产生两类错误判断：
  (a) 把噪声当成效果（"参数 0.08 比 0.05 好"）；
  (b) 把"样本太短看不出来"误读为"策略确实不行"。

本项目在 2026-08-05 的三轮优化中两次踩到 (a)：追踪止损扫参数、
sentiment 注入常量，都出现过"某一档看起来更好"的假象。根因就是
1 年日频数据的年化夏普标准误约等于 1.00，任何小于 1 的差异都在噪声内。

标准误公式（Lo 2002）
---------------------
对 IID 收益，单期夏普 SR_p 的渐近标准误为：

    SE(SR_p) = sqrt( (1 + SR_p^2 / 2) / T )

其中 T 为观测数。年化时 SR_a = SR_p * sqrt(q)（q = 每年期数），
标准误同比例放大：

    SE(SR_a) = sqrt( q * (1 + SR_a^2 / (2q)) / T )

直观量级（日频、q=252、SR_a≈0）：
    T=252   (1年)   → SE ≈ 1.00
    T=1260  (5年)   → SE ≈ 0.45
    T=2520  (10年)  → SE ≈ 0.32

重要限制
--------
上式假设收益 IID。真实收益存在自相关与厚尾，会使真实标准误**更大**，
因此本模块给出的是**乐观下界**。`ar1_inflation_factor()` 提供一阶
自相关修正的粗略膨胀系数，可用于敏感度检查，但它本身也只是近似。

参考：Lo, A. W. (2002). "The Statistics of Sharpe Ratios."
     Financial Analysts Journal, 58(4), 36-52.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

TRADING_DAYS_PER_YEAR = 252

# 标准正态分布的双侧 95% 临界值
_Z_95 = 1.959964


def _norm_sf(z: float) -> float:
    """标准正态分布的上尾概率 P(Z > z)，用 erfc 实现，避免依赖 scipy。"""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


@dataclass(frozen=True, slots=True)
class SharpeStats:
    """夏普比率的点估计及其不确定性。"""

    sharpe: float               # 年化夏普点估计
    n_obs: int                  # 观测数（交易日）
    periods_per_year: int       # 年化因子（日频=252）
    std_error: float            # 年化夏普的标准误（IID 假设下）
    ci_low: float               # 95% 置信区间下界
    ci_high: float              # 95% 置信区间上界
    t_vs_zero: float            # H0: SR=0 的 t 统计量
    p_vs_zero: float            # 上述单侧 p 值
    threshold: float            # 被检验的目标阈值（如 E-01 的 0.5）
    t_vs_threshold: float       # H0: SR=threshold 的 t 统计量
    p_vs_threshold: float       # 上述单侧 p 值
    threshold_in_ci: bool       # 阈值是否落在 95% CI 内（落在内=无法区分）

    def to_dict(self) -> dict[str, Any]:
        return {
            "sharpe": round(self.sharpe, 4),
            "n_obs": self.n_obs,
            "periods_per_year": self.periods_per_year,
            "std_error": round(self.std_error, 4),
            "ci_low": round(self.ci_low, 4),
            "ci_high": round(self.ci_high, 4),
            "t_vs_zero": round(self.t_vs_zero, 3),
            "p_vs_zero": round(self.p_vs_zero, 4),
            "threshold": self.threshold,
            "t_vs_threshold": round(self.t_vs_threshold, 3),
            "p_vs_threshold": round(self.p_vs_threshold, 4),
            "threshold_in_ci": self.threshold_in_ci,
        }

    @property
    def verdict(self) -> str:
        """对"是否达到 threshold"给出可读判定。"""
        if self.threshold_in_ci:
            return (
                f"无法区分于 {self.threshold:g}"
                f"（95% CI [{self.ci_low:+.3f}, {self.ci_high:+.3f}] 覆盖该值）"
            )
        if self.sharpe > self.threshold:
            return f"显著高于 {self.threshold:g}（p={self.p_vs_threshold:.4f}）"
        return f"显著低于 {self.threshold:g}（p={self.p_vs_threshold:.4f}）"

    def to_line(self) -> str:
        """单行摘要，供 CLI 表格使用。"""
        return (
            f"Sharpe={self.sharpe:+.3f} ± {self.std_error:.3f}  "
            f"95%CI[{self.ci_low:+.3f}, {self.ci_high:+.3f}]  {self.verdict}"
        )


def sharpe_std_error(
    sharpe: float,
    n_obs: int,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """
    年化夏普的渐近标准误（Lo 2002，IID 假设）。

        SE(SR_a) = sqrt( q * (1 + SR_a^2 / (2q)) / T )

    n_obs < 2 时返回 inf（无法估计）。
    """
    if n_obs < 2:
        return float("inf")
    q = float(periods_per_year)
    variance = q * (1.0 + (sharpe * sharpe) / (2.0 * q)) / float(n_obs)
    return math.sqrt(variance)


def ar1_inflation_factor(returns: Sequence[float]) -> float:
    """
    一阶自相关对夏普标准误的粗略膨胀系数。

    对 AR(1) 过程，长期方差相对 IID 放大约 (1+rho)/(1-rho)，
    标准误则放大其平方根。返回 max(1.0, sqrt((1+rho)/(1-rho)))。

    仅用于敏感度检查：真实收益并非严格 AR(1)，该系数只说明
    "把自相关考虑进来后 SE 至少还要乘上多少"。
    """
    r = [float(x) for x in returns]
    n = len(r)
    if n < 3:
        return 1.0
    mean = sum(r) / n
    denom = sum((x - mean) ** 2 for x in r)
    if denom <= 0:
        return 1.0
    numer = sum((r[i] - mean) * (r[i - 1] - mean) for i in range(1, n))
    rho = numer / denom
    # 夹住 rho，避免 rho→1 时系数爆炸
    rho = max(-0.95, min(0.95, rho))
    if rho <= 0:
        return 1.0
    return math.sqrt((1.0 + rho) / (1.0 - rho))


def compute_sharpe_stats(
    sharpe: float,
    n_obs: int,
    *,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
    threshold: float = 0.5,
    se_inflation: float = 1.0,
) -> SharpeStats:
    """
    由夏普点估计与观测数构造完整的统计描述。

    参数
    ----
    sharpe          年化夏普点估计
    n_obs           观测数（交易日）
    threshold       要检验的目标值（E-01 为 0.5）
    se_inflation    标准误膨胀系数，用于纳入自相关等非 IID 效应；
                    传入 ar1_inflation_factor() 的结果可做敏感度检查。
    """
    se = sharpe_std_error(sharpe, n_obs, periods_per_year) * max(1.0, se_inflation)

    if not math.isfinite(se) or se <= 0:
        return SharpeStats(
            sharpe=sharpe, n_obs=n_obs, periods_per_year=periods_per_year,
            std_error=float("inf"), ci_low=float("-inf"), ci_high=float("inf"),
            t_vs_zero=0.0, p_vs_zero=1.0, threshold=threshold,
            t_vs_threshold=0.0, p_vs_threshold=1.0, threshold_in_ci=True,
        )

    ci_low = sharpe - _Z_95 * se
    ci_high = sharpe + _Z_95 * se

    t0 = sharpe / se
    p0 = _norm_sf(abs(t0))

    t_thr = (sharpe - threshold) / se
    p_thr = _norm_sf(abs(t_thr))

    return SharpeStats(
        sharpe=sharpe,
        n_obs=n_obs,
        periods_per_year=periods_per_year,
        std_error=se,
        ci_low=ci_low,
        ci_high=ci_high,
        t_vs_zero=t0,
        p_vs_zero=p0,
        threshold=threshold,
        t_vs_threshold=t_thr,
        p_vs_threshold=p_thr,
        threshold_in_ci=(ci_low <= threshold <= ci_high),
    )


def required_n_obs(
    target_se: float,
    sharpe: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> int:
    """
    反解：要把标准误压到 target_se，需要多少个观测。

    由 SE^2 = q(1 + SR^2/(2q))/T 解出 T：
        T = q * (1 + SR^2/(2q)) / SE^2

    用于回答"E-01 要多少年数据才可证伪"这类问题。
    """
    if target_se <= 0:
        return 0
    q = float(periods_per_year)
    t = q * (1.0 + (sharpe * sharpe) / (2.0 * q)) / (target_se * target_se)
    return int(math.ceil(t))


def paired_diff_stats(
    a: Sequence[float],
    b: Sequence[float],
) -> tuple[float, float, float]:
    """
    配对差值检验：返回 (均值差 b-a, 标准误, t 统计量)。

    用于比较两个 arm 跑在**同一批价格路径**上的表现。配对设计消掉了
    标的间的共同波动，标准误远小于各自的独立标准误——这正是
    2026-08-05 测量 regime_aware 时能把效果界定在 [-0.16, +0.17]
    的原因（配对 SE=0.083 vs 单股 SE=1.00）。
    """
    if len(a) != len(b) or len(a) < 2:
        return 0.0, 0.0, 0.0
    diffs = [float(y) - float(x) for x, y in zip(a, b)]
    n = len(diffs)
    mean = sum(diffs) / n
    var = sum((d - mean) ** 2 for d in diffs) / (n - 1)
    se = math.sqrt(var / n) if var > 0 else 0.0
    t = mean / se if se > 0 else 0.0
    return mean, se, t
