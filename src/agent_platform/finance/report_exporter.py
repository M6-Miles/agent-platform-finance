"""报告导出工具：把 SecurityAnalysisResult 导出为 Excel 或 HTML。"""
from __future__ import annotations

import math
from io import BytesIO
from typing import TYPE_CHECKING

import pandas as pd

# fallback 安全格式化——遇到 NaN/Inf/None 返回 "N/A"
_safe = lambda v, fmt: (f"{{:{fmt}}}").format(v) if isinstance(v, (int, float)) and math.isfinite(v) else "N/A"

if TYPE_CHECKING:
    from agent_platform.finance.analysis import SecurityAnalysisResult


# ── Excel 导出 ─────────────────────────────────────────────────────────────

def to_excel_bytes(result: "SecurityAnalysisResult") -> bytes:
    """生成包含两张工作表的 Excel 文件并返回字节流。
    Sheet1 「分析摘要」：所有汇总指标。
    Sheet2 「历史行情」：完整 price_history。
    """
    buf = BytesIO()
    summary_rows = [
        ("证券代码", result.symbol),
        ("证券名称", result.name),
        ("市场", result.market),
        ("数据来源", result.source),
        ("更新时间", result.updated_at),
        ("分析开始", result.start_date),
        ("分析结束", result.end_date),
        ("最新收盘价", result.latest_close),
        ("5日均线", result.latest_ma5),
        ("20日均线", result.latest_ma20),
        ("布林上轨", result.latest_bb_upper),
        ("布林下轨", result.latest_bb_lower),
        ("布林带位置(%)", result.latest_bb_position_pct),
        ("RSI(14)", result.latest_rsi),
        ("MACD DIF", result.latest_macd),
        ("MACD DEA", result.latest_macd_signal),
        ("KDJ K", result.latest_kdj_k),
        ("KDJ D", result.latest_kdj_d),
        ("KDJ J", result.latest_kdj_j),
        ("ATR(14)", result.latest_atr),
        ("CCI(20)", result.latest_cci),
        ("EMA12", result.latest_ema12),
        ("EMA26", result.latest_ema26),
        ("区间收益率(%)", result.total_return_pct),
        ("年化波动率(%)", result.annualized_volatility_pct),
        ("最大回撤(%)", result.max_drawdown_pct),
        ("风险提示", result.disclaimer),
    ]
    summary_df = pd.DataFrame(summary_rows, columns=["指标", "数值"])

    # 选择 price_history 中的关键列导出
    history_cols = [
        c for c in [
            "date", "open", "high", "low", "close", "volume",
            "ma5", "ma20", "ema12", "ema26", "macd", "macd_signal", "rsi",
            "bb_upper", "bb_middle", "bb_lower",
            "kdj_k", "kdj_d", "kdj_j", "atr", "cci", "volume_ma5",
        ]
        if c in result.price_history.columns
    ]
    history_df = result.price_history[history_cols].copy()
    col_rename = {
        "date": "日期", "open": "开盘", "high": "最高", "low": "最低",
        "close": "收盘", "volume": "成交量",
        "ma5": "MA5", "ma20": "MA20",
        "ema12": "EMA12", "ema26": "EMA26",
        "macd": "MACD DIF", "macd_signal": "MACD DEA", "rsi": "RSI",
        "bb_upper": "布林上轨", "bb_middle": "布林中轨", "bb_lower": "布林下轨",
        "kdj_k": "KDJ K", "kdj_d": "KDJ D", "kdj_j": "KDJ J",
        "atr": "ATR", "cci": "CCI", "volume_ma5": "成交量MA5",
    }
    history_df = history_df.rename(columns=col_rename)

    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="分析摘要", index=False)
        history_df.to_excel(writer, sheet_name="历史行情", index=False)

        # 简单美化：自动调整列宽
        for sheet_name, df in [("分析摘要", summary_df), ("历史行情", history_df)]:
            ws = writer.sheets[sheet_name]
            for col_idx, col in enumerate(df.columns, start=1):
                max_len = max(
                    len(str(col)),
                    df.iloc[:, col_idx - 1].astype(str).str.len().max() if len(df) > 0 else 0,
                )
                ws.column_dimensions[
                    ws.cell(row=1, column=col_idx).column_letter
                ].width = min(max_len + 2, 30)

    return buf.getvalue()


# ── HTML 导出 ─────────────────────────────────────────────────────────────

def to_html_bytes(result: "SecurityAnalysisResult") -> bytes:
    """生成包含 K线、MACD、KDJ、RSI 四张 Plotly 图表的自包含 HTML 报告。"""
    import plotly.io as pio

    from agent_platform.finance.chart_builder import (
        make_candlestick_fig,
        make_kdj_fig,
        make_macd_fig,
        make_rsi_fig,
    )

    df = result.price_history.copy()
    fig1 = make_candlestick_fig(df)
    fig2 = make_macd_fig(df)
    fig3 = make_kdj_fig(df)
    fig4 = make_rsi_fig(df)

    # 摘要表格 HTML
    summary_html = (
        "<table style='border-collapse:collapse;width:100%;font-size:14px'>"
        "<tr><th style='background:#2a78d6;color:#fff;padding:6px 12px'>指标</th>"
        "<th style='background:#2a78d6;color:#fff;padding:6px 12px'>数值</th></tr>"
    )
    rows = [
        ("证券", f"{result.name}（{result.market}:{result.symbol}）"),
        ("数据来源", result.source),
        ("分析区间", f"{result.start_date} 至 {result.end_date}"),
        ("最新收盘价", _safe(result.latest_close, ".2f")),
        ("MA5 / MA20", f"{_safe(result.latest_ma5, '.2f')} / {_safe(result.latest_ma20, '.2f')}"),
        ("RSI(14)", _safe(result.latest_rsi, ".2f")),
        ("MACD DIF/DEA", f"{_safe(result.latest_macd, '.4f')} / {_safe(result.latest_macd_signal, '.4f')}"),
        ("KDJ K/D/J", f"{_safe(result.latest_kdj_k, '.2f')} / {_safe(result.latest_kdj_d, '.2f')} / {_safe(result.latest_kdj_j, '.2f')}"),
        ("ATR(14)", _safe(result.latest_atr, ".2f")),
        ("CCI(20)", _safe(result.latest_cci, ".2f")),
        ("EMA12 / EMA26", f"{_safe(result.latest_ema12, '.2f')} / {_safe(result.latest_ema26, '.2f')}"),
        ("布林带位置", f"{_safe(result.latest_bb_position_pct, '.1f')}%"),
        ("区间收益率", f"{_safe(result.total_return_pct, '.2f')}%"),
        ("年化波动率", f"{_safe(result.annualized_volatility_pct, '.2f')}%"),
        ("最大回撤", f"{_safe(result.max_drawdown_pct, '.2f')}%"),
    ]
    for i, (k, v) in enumerate(rows):
        bg = "#f5f7fa" if i % 2 == 0 else "#fff"
        summary_html += (
            f"<tr style='background:{bg}'>"
            f"<td style='padding:5px 12px;border-bottom:1px solid #e0e0e0'>{k}</td>"
            f"<td style='padding:5px 12px;border-bottom:1px solid #e0e0e0'>{v}</td></tr>"
        )
    summary_html += "</table>"

    chart_specs = [
        ("K线、均线与布林带", fig1, True),
        ("MACD（12/26/9）", fig2, False),
        ("KDJ（9/3/3）", fig3, False),
        ("RSI（14）", fig4, False),
    ]
    charts_html = "\n".join(
        "<section class='chart-section'>"
        f"<h3>{title}</h3>"
        + pio.to_html(
            fig,
            full_html=False,
            include_plotlyjs=True if include_js else False,
            config={"responsive": True, "displaylogo": False},
        )
        + "</section>"
        for title, fig, include_js in chart_specs
    )

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{result.name}（{result.symbol}）行情分析报告</title>
<style>
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
          max-width: 1200px; margin: 0 auto; padding: 24px; color: #333; }}
  h1 {{ color: #2a78d6; border-bottom: 2px solid #2a78d6; padding-bottom: 8px; }}
  h2 {{ margin: 32px 0 14px; }}
  .chart-section {{ margin: 0 0 34px; break-inside: avoid; overflow: hidden; }}
  .chart-section h3 {{ margin: 0 0 4px; font-size: 18px; line-height: 1.4; color: #1f2937; }}
  .chart-section .plotly-graph-div {{ width: 100% !important; }}
  .disclaimer {{ background: #fff8e1; border-left: 4px solid #ffc107;
                  padding: 10px 16px; margin: 16px 0; font-size: 13px; color: #795548; }}
</style>
</head>
<body>
<h1>{result.name}（{result.market}:{result.symbol}）行情分析报告</h1>
<p style="color:#666;font-size:14px">分析区间：{result.start_date} 至 {result.end_date}
   &nbsp;|&nbsp; 数据来源：{result.source}
   &nbsp;|&nbsp; 更新时间：{result.updated_at}</p>
<div class="disclaimer">⚠️ {result.disclaimer}</div>
<h2>指标摘要</h2>
{summary_html}
<h2>图表分析</h2>
{charts_html}
</body>
</html>"""

    return html.encode("utf-8")
