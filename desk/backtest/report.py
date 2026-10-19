"""Backtest numbers, CSV files and an HTML report with an equity chart."""

from __future__ import annotations

import html
import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from desk.backtest.engine import BacktestResult

# Chart colours: validated categorical slots 1 and 2 (dataviz reference palette)
STRATEGY = "#2a78d6"
HOLD = "#eb6834"
SURFACE = "#fcfcfb"
GRID = "#e6e5e1"
TEXT = "#0b0b0b"
TEXT_2 = "#52514e"


def max_drawdown_pct(equity: pd.Series) -> float:
    """Largest fall from a peak, in % (a negative number, 0 if it never fell)."""
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    return float(((equity / peak) - 1).min() * 100)


def drawdown_series(equity: pd.Series) -> pd.Series:
    return (equity / equity.cummax() - 1) * 100


def metrics(r: BacktestResult) -> dict:
    t = r.trades
    wins = t[t["pnl_aud"] > 0]
    losses = t[t["pnl_aud"] <= 0]
    gross_win = float(wins["pnl_aud"].sum())
    gross_loss = float(-losses["pnl_aud"].sum())
    end_eq = float(r.equity.iloc[-1]) if len(r.equity) else r.starting_balance
    end_hold = float(r.hold.iloc[-1]) if len(r.hold) else r.starting_balance
    days = (r.end - r.start).total_seconds() / 86400
    return {
        "days": days,
        "start_equity": r.starting_balance,
        "end_equity": end_eq,
        "total_return_pct": (end_eq / r.starting_balance - 1) * 100,
        "max_drawdown_pct": max_drawdown_pct(r.equity),
        "trades": int(len(t)),
        "trades_per_week": len(t) / days * 7 if days else 0.0,
        "wins": int(len(wins)),
        "win_rate_pct": len(wins) / len(t) * 100 if len(t) else 0.0,
        "avg_win_pct": float(wins["pnl_pct"].mean()) if len(wins) else 0.0,
        "avg_loss_pct": float(losses["pnl_pct"].mean()) if len(losses) else 0.0,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0),
        "expectancy_aud": float(t["pnl_aud"].mean()) if len(t) else 0.0,
        "best_trade_pct": float(t["pnl_pct"].max()) if len(t) else 0.0,
        "worst_trade_pct": float(t["pnl_pct"].min()) if len(t) else 0.0,
        "fees_aud": r.fees_aud,
        "fees_pct_of_start": r.fees_aud / r.starting_balance * 100,
        "in_market_pct": r.in_market_pct,
        "exit_reasons": dict(t["exit_reason"].value_counts()) if len(t) else {},
        "avg_invested_pct": float(r.exposure.mean() * 100) if len(r.exposure) else 0.0,
        "return_on_invested_pct": ((end_eq / r.starting_balance - 1) * 100 / (r.exposure.mean()))
                                  if len(r.exposure) and r.exposure.mean() > 0 else 0.0,
        "hold_same_invested_pct": ((end_hold / r.starting_balance - 1) * 100 * float(r.exposure.mean()))
                                  if len(r.exposure) else 0.0,
        "hold_end_equity": end_hold,
        "hold_return_pct": (end_hold / r.starting_balance - 1) * 100,
        "hold_max_drawdown_pct": max_drawdown_pct(r.hold),
        "daily_limit_days": r.daily_limit_days,
        "open_at_end": r.open_at_end,
    }


def summary_text(r: BacktestResult, m: dict) -> str:
    def money(x):
        return f"A${x:,.2f}"

    lines = [
        f"BACKTEST  {r.start:%Y-%m-%d} to {r.end:%Y-%m-%d} ({m['days']:.0f} days)  coins: {', '.join(r.coins)}",
        f"Strategy: {r.settings.get('mode', 'day')}   orders: entry {r.settings.get('entry_order', 'market')}, "
        f"take-profit {r.settings.get('take_profit_order', 'market')}   candles: {r.settings.get('base_timeframe', '5m')}",
        f"Assumptions: CoinSpot spread {r.settings['spread_pct']}%, slippage {r.settings['slippage_pct']}%, "
        f"fee {r.settings['fee_pct']}% per order; every signal taken (no AI, no news)",
        "",
        f"  {'':<26}{'Desk':>16}{'Just hold BTC':>18}",
        f"  {'Start':<26}{money(m['start_equity']):>16}{money(m['start_equity']):>18}",
        f"  {'End':<26}{money(m['end_equity']):>16}{money(m['hold_end_equity']):>18}",
        f"  {'Total return':<26}{m['total_return_pct']:>15.2f}%{m['hold_return_pct']:>17.2f}%",
        f"  {'Max drawdown':<26}{m['max_drawdown_pct']:>15.2f}%{m['hold_max_drawdown_pct']:>17.2f}%",
        f"  {'Average money invested':<26}{m['avg_invested_pct']:>15.1f}%{100.0:>17.1f}%",
        f"  Fair comparison: holding BTC with the same {m['avg_invested_pct']:.1f}% invested would have made "
        f"{m['hold_same_invested_pct']:+.2f}%; the desk made {m['total_return_pct']:+.2f}%",
        f"  Return on the money actually invested: {m['return_on_invested_pct']:+.1f}%",
        "",
        f"  Trades: {m['trades']} ({m['trades_per_week']:.1f}/week)   wins: {m['wins']}   "
        f"win rate: {m['win_rate_pct']:.1f}%",
        f"  Average win {m['avg_win_pct']:+.2f}%   average loss {m['avg_loss_pct']:+.2f}%   "
        f"profit factor {m['profit_factor']:.2f}   average per trade {money(m['expectancy_aud'])}",
        f"  Best trade {m['best_trade_pct']:+.2f}%   worst trade {m['worst_trade_pct']:+.2f}%",
        f"  Fees paid {money(m['fees_aud'])} ({m['fees_pct_of_start']:.2f}% of the starting balance)",
        f"  Time with a position open: {m['in_market_pct']:.1f}%   days the daily loss limit hit: "
        f"{m['daily_limit_days']}   positions still open at the end: {m['open_at_end']}",
        f"  Exits: " + (", ".join(f"{k} {v}" for k, v in m["exit_reasons"].items()) or "none"),
        f"  Setups found by the scanner: " + (", ".join(f"{k} {v}" for k, v in r.setups_found.items()) or "none"),
        f"  Refused by the Risk Manager: " + (", ".join(f"{k} {v}" for k, v in r.risk_rejections.most_common()) or "none"),
        f"  Skipped (limits already reached): " + (", ".join(f"{k} {v}" for k, v in r.skipped.items()) or "none"),
    ]
    return "\n".join(lines)


def _figure(r: BacktestResult) -> go.Figure:
    eq = r.equity.resample("1h").last().dropna()
    hold = r.hold.resample("1h").last().dropna()
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.72, 0.28], vertical_spacing=0.07,
                        subplot_titles=("Account value (A$)", "Drawdown from peak (%)"))
    line = dict(width=2, shape="linear")
    fig.add_trace(go.Scatter(x=eq.index, y=eq.values, name="Desk (backtest)", line={**line, "color": STRATEGY},
                             hovertemplate="A$%{y:,.2f}<extra>Desk</extra>"), row=1, col=1)
    fig.add_trace(go.Scatter(x=hold.index, y=hold.values, name="Just hold BTC", line={**line, "color": HOLD},
                             hovertemplate="A$%{y:,.2f}<extra>Hold BTC</extra>"), row=1, col=1)
    for s, color, name in ((eq, STRATEGY, "Desk"), (hold, HOLD, "Hold BTC")):
        dd = drawdown_series(s)
        fig.add_trace(go.Scatter(x=dd.index, y=dd.values, name=f"{name} drawdown", showlegend=False,
                                 line={**line, "color": color}, fill="tozeroy",
                                 fillcolor=color + "1a", hovertemplate="%{y:.2f}%<extra>" + name + "</extra>"),
                      row=2, col=1)
    # direct labels at the line ends (2 series: legend + labels)
    for s, color, name in ((eq, STRATEGY, "Desk"), (hold, HOLD, "Hold BTC")):
        if len(s):
            fig.add_annotation(x=s.index[-1], y=float(s.iloc[-1]), text=f"{name} A${s.iloc[-1]:,.0f}",
                               showarrow=False, xanchor="left", xshift=6, font=dict(color=TEXT_2, size=12),
                               row=1, col=1)
    fig.update_layout(
        paper_bgcolor=SURFACE, plot_bgcolor=SURFACE, font=dict(color=TEXT, size=13,
                                                               family="system-ui, -apple-system, Segoe UI, sans-serif"),
        hovermode="x unified", margin=dict(l=70, r=150, t=60, b=40), height=620,
        legend=dict(orientation="h", y=1.08, x=0, font=dict(color=TEXT_2)),
    )
    fig.update_xaxes(showgrid=True, gridcolor=GRID, gridwidth=1, linecolor=GRID, tickfont=dict(color=TEXT_2))
    fig.update_yaxes(showgrid=True, gridcolor=GRID, gridwidth=1, zeroline=False, tickfont=dict(color=TEXT_2))
    fig.update_yaxes(tickprefix="A$", tickformat=",.0f", row=1, col=1)
    fig.update_yaxes(ticksuffix="%", tickformat=".1f", row=2, col=1)
    for a in fig.layout.annotations[:2]:
        a.font = dict(color=TEXT_2, size=13)
    return fig


def _table(m: dict) -> str:
    rows = [
        ("Total return", f"{m['total_return_pct']:+.2f}%", f"{m['hold_return_pct']:+.2f}%"),
        ("End value", f"A${m['end_equity']:,.2f}", f"A${m['hold_end_equity']:,.2f}"),
        ("Max drawdown", f"{m['max_drawdown_pct']:.2f}%", f"{m['hold_max_drawdown_pct']:.2f}%"),
        ("Trades", f"{m['trades']}", "1 buy, 1 sell"),
        ("Win rate", f"{m['win_rate_pct']:.1f}%", "-"),
        ("Profit factor", f"{m['profit_factor']:.2f}", "-"),
        ("Fees paid", f"A${m['fees_aud']:,.2f}", "-"),
        ("Time in the market", f"{m['in_market_pct']:.1f}%", "100%"),
        ("Average money invested", f"{m['avg_invested_pct']:.1f}%", "100%"),
        ("Same money invested in BTC", "-", f"{m['hold_same_invested_pct']:+.2f}%"),
        ("Return on money invested", f"{m['return_on_invested_pct']:+.1f}%", f"{m['hold_return_pct']:+.2f}%"),
    ]
    body = "".join(f"<tr><th>{html.escape(a)}</th><td>{html.escape(b)}</td><td>{html.escape(c)}</td></tr>"
                   for a, b, c in rows)
    return f"<table><thead><tr><th></th><th>Desk</th><th>Just hold BTC</th></tr></thead><tbody>{body}</tbody></table>"


LIMITS = """<ul>
<li><b>The AI agents are not in this test.</b> Every signal was taken, using its suggested stop and target. The real desk is pickier (Entry Agent, Skeptic), so this measures the scanner rules,
the Risk Manager and the exits, not the AI. Paper trading is how the AI gets tested.</li>
<li><b>No news.</b> Major-event blocks can't be replayed.</li>
<li><b>Prices are Binance (USDT) candles</b> standing in for CoinSpot AUD prices. Returns are in %, so this is close,
but AUD/USD moves over the period are ignored.</li>
<li><b>CoinSpot's spread is assumed constant</b> (it really varies from about 0.03% to 0.5%).</li>
<li><b>Stops fill at the stop price</b> (or the open, if the price jumped past it). Real fills in a fast crash
can be worse.</li>
<li><b>Your 5%-per-trade limit keeps most money in cash</b>, so compare with "same money invested in BTC",
not with putting everything into BTC.</li>
<li>Past results don't predict future results.</li>
</ul>"""


def write_report(r: BacktestResult, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    m = metrics(r)
    text = summary_text(r, m)
    (out_dir / "summary.txt").write_text(text + "\n", encoding="utf-8")
    r.trades.to_csv(out_dir / "trades.csv", index=False)
    pd.DataFrame({"strategy_aud": r.equity, "hold_btc_aud": r.hold}).resample("1h").last().dropna() \
        .to_csv(out_dir / "equity_hourly.csv", index_label="time_utc")
    (out_dir / "metrics.json").write_text(json.dumps(m, indent=2, default=str), encoding="utf-8")

    chart = _figure(r).to_html(full_html=False, include_plotlyjs="cdn", config={"displaylogo": False})
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Backtest report</title>
<style>
  :root {{ color-scheme: light; --surface: {SURFACE}; --text: {TEXT}; --text-2: {TEXT_2}; --rule: {GRID}; }}
  body {{ margin: 0; background: var(--surface); color: var(--text);
         font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
  main {{ max-width: 1100px; margin: 0 auto; padding: 24px 16px 48px; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .sub {{ color: var(--text-2); margin: 0 0 20px; }}
  table {{ border-collapse: collapse; margin: 8px 0 24px; min-width: 420px; }}
  th, td {{ text-align: right; padding: 6px 14px; border-bottom: 1px solid var(--rule); font-variant-numeric: tabular-nums; }}
  th:first-child {{ text-align: left; font-weight: 500; }}
  thead th {{ color: var(--text-2); font-weight: 600; }}
  pre {{ background: #f3f2ef; padding: 12px; overflow-x: auto; font-size: 13px; }}
  .limits {{ color: var(--text-2); }}
</style></head>
<body><main>
<h1>Backtest: {html.escape(', '.join(r.coins))}</h1>
<p class="sub">Strategy: <b>{html.escape(str(r.settings.get('mode', 'day')))}</b>, entry orders
{html.escape(str(r.settings.get('entry_order', 'market')))}, take-profit orders
{html.escape(str(r.settings.get('take_profit_order', 'market')))}.
{r.start:%d %b %Y} to {r.end:%d %b %Y} ({m['days']:.0f} days). Starting balance
A${r.starting_balance:,.0f}. Spread {r.settings['spread_pct']}%, slippage {r.settings['slippage_pct']}%,
fee {r.settings['fee_pct']}% per order.</p>
{_table(m)}
{chart}
<h2>Full summary</h2>
<pre>{html.escape(text)}</pre>
<p>Every trade is in <code>trades.csv</code>; hourly account values in <code>equity_hourly.csv</code>.</p>
<h2>What this test can and can't tell you</h2>
<div class="limits">{LIMITS}</div>
</main></body></html>"""
    (out_dir / "report.html").write_text(page, encoding="utf-8")
    return m
