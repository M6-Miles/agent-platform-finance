#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Scripts/generate_sample_data.py
================================
生成内置离线样例行情数据（data/sample/prices.csv）。

设计目标
--------
1. **完全确定性**：每个标的使用固定整数种子，与 PYTHONHASHSEED 无关。
   同一份代码在任何机器、任何进程上生成的 CSV 逐字节一致（见 --verify）。
2. **两组数据**：
   - DEMO001–DEMO004：README / Streamlit / 测试引用的离线演示标的。
   - TEST001–TEST020：交付物验收（≥20 只）使用的合成标的集。
3. **不预设涨跌**：漂移项按标的确定性地取值，正负兼有，量级接近真实 A 股，
   不人为拉高信噪比。回测结论因此可信。

用法
----
    python Scripts/generate_sample_data.py            # 缺失时生成
    python Scripts/generate_sample_data.py --force    # 强制重新生成
    python Scripts/generate_sample_data.py --verify    # 校验确定性（生成两次比对哈希）

⚠️  仅供研究参考，不构成投资建议。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import random
import sys
from datetime import date, timedelta
from pathlib import Path

# 强制 UTF-8 输出（兼容 Windows GBK 终端）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

SAMPLE_CSV = ROOT / "data" / "sample" / "prices.csv"

# 数据起始日与长度 —— 测试依赖此起点（tests/test_sample_data_provider.py）
START_DATE = date(2025, 1, 2)
N_DAYS = 252
UPDATED_AT = "2026-07-29"  # 固定值，保证 CSV 可复现（README 第 11 节记录此日期）

# ── 标的定义 ──────────────────────────────────────────────────────────────────
# (symbol, name, base_price, daily_vol, annual_drift)
# annual_drift 为年化漂移，正负兼有，量级参考真实 A 股个股（-25% ~ +30%）。

DEMO_UNIVERSE = [
    ("DEMO001", "样例科技股",   100.0, 0.018,  0.12),
    ("DEMO002", "样例消费股",    56.5, 0.015,  0.06),
    ("DEMO003", "样例周期股",    32.8, 0.022, -0.08),
    ("DEMO004", "样例金融股",    18.3, 0.012,  0.03),
]

TEST_UNIVERSE = [
    ("TEST001", "科技龙头A",  100.0, 0.018,  0.22),
    ("TEST002", "消费白马B",   56.5, 0.015,  0.14),
    ("TEST003", "医药成长C",   32.8, 0.022, -0.11),
    ("TEST004", "新能源D",     88.0, 0.025,  0.28),
    ("TEST005", "金融蓝筹E",   18.3, 0.012,  0.07),
    ("TEST006", "地产周期F",   12.6, 0.020, -0.19),
    ("TEST007", "化工材料G",   45.2, 0.019,  0.05),
    ("TEST008", "半导体H",    210.0, 0.028,  0.31),
    ("TEST009", "食品饮料I",   68.0, 0.014,  0.09),
    ("TEST010", "军工主题J",   55.5, 0.023,  0.17),
    ("TEST011", "互联网K",    142.0, 0.021, -0.06),
    ("TEST012", "汽车整车L",   28.4, 0.017,  0.11),
    ("TEST013", "电力公用M",    8.9, 0.010,  0.04),
    ("TEST014", "有色金属N",   16.2, 0.024,  0.20),
    ("TEST015", "农林牧渔O",   22.1, 0.020, -0.13),
    ("TEST016", "传媒娱乐P",   11.5, 0.026,  0.08),
    ("TEST017", "零售电商Q",   95.0, 0.019, -0.04),
    ("TEST018", "生物科技R",   78.3, 0.030,  0.25),
    ("TEST019", "机械设备S",   34.7, 0.016,  0.13),
    ("TEST020", "银行大盘T",    6.8, 0.009,  0.02),
]

FULL_UNIVERSE = DEMO_UNIVERSE + TEST_UNIVERSE

# 固定种子基准 —— 不使用 hash()，因为 CPython 对 str 的 hash 默认加盐
# （PYTHONHASHSEED 随进程随机），会导致数据不可复现。
SEED_BASE = 20250102

_TRADING_DAYS = 252


def _gen_price_series(
    symbol: str,
    name: str,
    base: float,
    vol: float,
    annual_drift: float,
    seed: int,
    n_days: int = N_DAYS,
) -> pd.DataFrame:
    """
    生成确定性的模拟日线序列（几何随机游走）。

    annual_drift 为年化漂移，转换为日漂移后叠加到收益率上。
    使用显式整数种子，保证跨进程可复现。
    """
    rng = random.Random(seed)  # 独立实例，不污染全局 random 状态
    daily_drift = annual_drift / _TRADING_DAYS

    rows = []
    price = base
    d = START_DATE
    count = 0
    while count < n_days:
        if d.weekday() < 5:  # 仅工作日
            ret = rng.gauss(daily_drift, vol)
            open_p = round(price * (1 + rng.gauss(0, vol * 0.3)), 2)
            close_p = round(price * (1 + ret), 2)
            high_p = round(max(open_p, close_p) * (1 + abs(rng.gauss(0, vol * 0.2))), 2)
            low_p = round(min(open_p, close_p) * (1 - abs(rng.gauss(0, vol * 0.2))), 2)
            volume = int(rng.randint(800_000, 2_000_000))
            rows.append({
                "market": "A股",
                "symbol": symbol,
                "name": name,
                "date": str(d),
                "open": open_p,
                "high": high_p,
                "low": low_p,
                "close": max(close_p, 0.01),  # 防止负价
                "volume": volume,
                "source": "内置样例数据（确定性生成器 generate_sample_data.py）",
                "updated_at": UPDATED_AT,
            })
            price = max(close_p, 0.01)
            count += 1
        d += timedelta(days=1)
    return pd.DataFrame(rows)


def build_dataset() -> pd.DataFrame:
    """构建完整样例数据集（DEMO001-004 + TEST001-020）。"""
    frames = []
    for idx, (symbol, name, base, vol, drift) in enumerate(FULL_UNIVERSE):
        frames.append(
            _gen_price_series(symbol, name, base, vol, drift, seed=SEED_BASE + idx)
        )
    return pd.concat(frames, ignore_index=True)


def _csv_bytes(df: pd.DataFrame) -> bytes:
    """统一序列化（换行符固定为 \\n，保证跨平台哈希一致）。"""
    buf = io.StringIO()
    df.to_csv(buf, index=False, lineterminator="\n")
    return buf.getvalue().encode("utf-8")


def write_dataset(force: bool = False) -> None:
    if SAMPLE_CSV.exists() and not force:
        existing = pd.read_csv(SAMPLE_CSV)
        have = set(existing["symbol"].unique())
        need = {s[0] for s in FULL_UNIVERSE}
        missing = need - have
        if not missing:
            print(f"ℹ️  样例数据已完整（{len(have)} 只标的），跳过生成。")
            print("   如需重新生成，请加 --force")
            return
        print(f"⚠️  检测到缺失标的：{sorted(missing)} —— 重新生成完整数据集")

    SAMPLE_CSV.parent.mkdir(parents=True, exist_ok=True)
    df = build_dataset()
    SAMPLE_CSV.write_bytes(_csv_bytes(df))

    digest = hashlib.sha256(_csv_bytes(df)).hexdigest()
    print(f"✅ 已生成样例数据：{SAMPLE_CSV}")
    print(f"   标的数：{df['symbol'].nunique()}（DEMO×{len(DEMO_UNIVERSE)} + TEST×{len(TEST_UNIVERSE)}）")
    print(f"   行数：{len(df)}")
    print(f"   区间：{df['date'].min()} ~ {df['date'].max()}")
    print(f"   SHA256：{digest}")


def verify_determinism() -> int:
    """连续生成两次并比对哈希，验证与 PYTHONHASHSEED 无关。"""
    print("🔍 校验数据生成确定性 ...")
    d1 = hashlib.sha256(_csv_bytes(build_dataset())).hexdigest()
    d2 = hashlib.sha256(_csv_bytes(build_dataset())).hexdigest()
    print(f"   第一次：{d1}")
    print(f"   第二次：{d2}")
    if d1 == d2:
        print("✅ 同进程内一致")
    else:
        print("❌ 同进程内不一致")
        return 1

    # 跨进程校验：子进程用不同 PYTHONHASHSEED 重新生成
    import os
    import subprocess

    env = os.environ.copy()
    env["PYTHONHASHSEED"] = "12345"
    env["PYTHONIOENCODING"] = "utf-8"
    code = (
        "import sys,hashlib;"
        f"sys.path.insert(0, r'{ROOT / 'Scripts'}');"
        "from generate_sample_data import build_dataset, _csv_bytes;"
        "print(hashlib.sha256(_csv_bytes(build_dataset())).hexdigest())"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True
    )
    d3 = (out.stdout or "").strip().splitlines()[-1] if out.stdout else "<无输出>"
    print(f"   子进程(PYTHONHASHSEED=12345)：{d3}")
    if d3 == d1:
        print("✅ 跨进程一致 —— 数据可复现（E-06 达标）")
        return 0
    print("❌ 跨进程不一致")
    print(out.stderr[-500:] if out.stderr else "")
    return 1


def main() -> None:
    p = argparse.ArgumentParser(description="生成内置离线样例行情数据")
    p.add_argument("--force", action="store_true", help="强制重新生成")
    p.add_argument("--verify", action="store_true", help="校验生成确定性")
    args = p.parse_args()

    if args.verify:
        sys.exit(verify_determinism())
    write_dataset(force=args.force)
    print("\n⚠️  仅供研究参考，不构成投资建议。")


if __name__ == "__main__":
    main()
