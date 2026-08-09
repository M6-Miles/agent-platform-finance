"""共享 Plotly 图表工厂，供 report_exporter 使用。

设计令牌（单一改动处）
----------------------
主涨红  #e84040   主跌绿  #2ca02c
DIF橙   #eb6834   DEA蓝   #2a78d6
RSI/J紫 #9467bd   网格    #eef2f7
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

# ── 设计令牌 ──────────────────────────────────────────────────────────────────
_C_UP       = "#e84040"
_C_DOWN     = "#2ca02c"
_C_MA5      = "#eb6834"
_C_MA20     = "#2a78d6"
_C_SIGNAL   = "#9467bd"
_C_BOLL     = "rgba(180,180,180,0.18)"
_C_GRID     = "#eef2f7"
_C_AXIS     = "#dce4ef"
_C_TICK     = "#6b7c93"
_FONT       = "'SF Pro Display','Helvetica Neue',Arial,sans-serif"


def _base_layout(height: int = 380, **overrides) -> dict:
    """工业级 Plotly 布局基线：统一背景、网格、字体、悬停样式。"""
    cfg: dict = dict(
        height=height,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(248,249,251,0.55)",
        font=dict(family=_FONT, size=12, color="#1a2332"),
        margin=dict(l=52, r=18, t=32, b=40),
        hovermode="x unified",
        hoverlabel=dict(
            bgcolor="white", bordercolor=_C_AXIS,
            font=dict(size=12, family=_FONT),
        ),
        legend=dict(
            orientation="h", yanchor="bottom", y=1.02,
            xanchor="left", x=0,
            font=dict(size=11), bgcolor="rgba(0,0,0,0)",
        ),
        xaxis=dict(
            showgrid=False,
            showline=True, linecolor=_C_AXIS, linewidth=1,
            tickfont=dict(size=11, color=_C_TICK),
            rangeslider=dict(visible=False),
        ),
        yaxis=dict(
            gridcolor=_C_GRID, gridwidth=1,
            showline=False, zeroline=False,
            tickfont=dict(size=11, color=_C_TICK),
        ),
    )
    cfg.update(overrides)
    return cfg


def make_candlestick_fig(df: pd.DataFrame) -> go.Figure:
    """K线图 + MA5/MA20 + 布林带填充区域和上下轨线。"""
    x = pd.to_datetime(df["date"])
    fig = go.Figure()

    # 布林带填充
    fig.add_trace(go.Scatter(
        x=pd.concat([x, x[::-1]]),
        y=pd.concat([df["bb_upper"], df["bb_lower"][::-1]]),
        fill="toself", fillcolor=_C_BOLL,
        line=dict(color="rgba(0,0,0,0)"),
        name="布林带", showlegend=True, hoverinfo="skip",
    ))
    fig.add_trace(go.Scatter(
        x=x, y=df["bb_upper"],
        line=dict(color="rgba(150,150,150,0.55)", width=1, dash="dot"),
        name="布林上轨", hovertemplate="上轨 %{y:.2f}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=x, y=df["bb_lower"],
        line=dict(color="rgba(150,150,150,0.55)", width=1, dash="dot"),
        name="布林下轨", hovertemplate="下轨 %{y:.2f}<extra></extra>",
    ))

    # K 线
    fig.add_trace(go.Candlestick(
        x=x, open=df["open"], high=df["high"], low=df["low"], close=df["close"],
        name="K线",
        increasing=dict(line=dict(color=_C_UP,   width=1), fillcolor=_C_UP),
        decreasing=dict(line=dict(color=_C_DOWN, width=1), fillcolor=_C_DOWN),
    ))

    # 均线
    fig.add_trace(go.Scatter(
        x=x, y=df["ma5"],
        line=dict(color=_C_MA5, width=1.6), name="MA5",
        hovertemplate="MA5 %{y:.2f}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=x, y=df["ma20"],
        line=dict(color=_C_MA20, width=1.6), name="MA20",
        hovertemplate="MA20 %{y:.2f}<extra></extra>",
    ))

    fig.update_layout(**_base_layout(height=440))
    return fig


def make_macd_fig(df: pd.DataFrame) -> go.Figure:
    """MACD 柱状图 + DIF/DEA 双线。"""
    x = pd.to_datetime(df["date"])
    bar_colors = [_C_UP if v >= 0 else _C_DOWN for v in df["macd_hist"]]
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=x, y=df["macd_hist"],
        marker=dict(color=bar_colors, line=dict(width=0)),
        name="MACD柱", hovertemplate="柱 %{y:.4f}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=x, y=df["macd"],
        line=dict(color=_C_MA5, width=1.6), name="DIF",
        hovertemplate="DIF %{y:.4f}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=x, y=df["macd_signal"],
        line=dict(color=_C_MA20, width=1.6), name="DEA",
        hovertemplate="DEA %{y:.4f}<extra></extra>",
    ))
    fig.add_hline(y=0, line=dict(color=_C_AXIS, width=1))
    fig.update_layout(**_base_layout(height=300))
    return fig


def make_rsi_fig(df: pd.DataFrame) -> go.Figure:
    """RSI 折线 + 超买(70)/超卖(30) 参考带和虚线。"""
    x = pd.to_datetime(df["date"])
    fig = go.Figure()
    fig.add_hrect(y0=70, y1=100, fillcolor="rgba(232,64,64,0.07)",  line_width=0)
    fig.add_hrect(y0=0,  y1=30,  fillcolor="rgba(44,160,44,0.07)", line_width=0)
    fig.add_hline(y=70, line=dict(color=_C_UP,   width=1, dash="dot"),
                  annotation_text="超买 70", annotation_position="right")
    fig.add_hline(y=30, line=dict(color=_C_DOWN, width=1, dash="dot"),
                  annotation_text="超卖 30", annotation_position="right")
    fig.add_trace(go.Scatter(
        x=x, y=df["rsi"],
        line=dict(color=_C_SIGNAL, width=1.8), name="RSI(14)",
        hovertemplate="RSI %{y:.1f}<extra></extra>",
    ))
    fig.update_layout(**_base_layout(
        height=260,
        yaxis=dict(range=[0, 100], gridcolor=_C_GRID,
                   tickfont=dict(size=11, color=_C_TICK)),
    ))
    return fig


def make_kdj_fig(df: pd.DataFrame) -> go.Figure:
    """KDJ 三线 + 超买(80)/超卖(20) 参考带和虚线。"""
    x = pd.to_datetime(df["date"])
    fig = go.Figure()
    fig.add_hrect(y0=80,  y1=110, fillcolor="rgba(232,64,64,0.07)",  line_width=0)
    fig.add_hrect(y0=-10, y1=20,  fillcolor="rgba(44,160,44,0.07)", line_width=0)
    fig.add_hline(y=80, line=dict(color=_C_UP,   width=1, dash="dot"),
                  annotation_text="超买 80", annotation_position="right")
    fig.add_hline(y=20, line=dict(color=_C_DOWN, width=1, dash="dot"),
                  annotation_text="超卖 20", annotation_position="right")
    fig.add_trace(go.Scatter(
        x=x, y=df["kdj_k"],
        line=dict(color=_C_MA5, width=1.6), name="K",
        hovertemplate="K %{y:.1f}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=x, y=df["kdj_d"],
        line=dict(color=_C_MA20, width=1.6), name="D",
        hovertemplate="D %{y:.1f}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=x, y=df["kdj_j"],
        line=dict(color=_C_SIGNAL, width=1.3, dash="dot"), name="J",
        hovertemplate="J %{y:.1f}<extra></extra>",
    ))
    fig.update_layout(**_base_layout(height=280))
    return fig
