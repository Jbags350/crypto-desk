"""Crypto Desk dashboard: double-click dashboard.bat (opens in your browser).

Shows the bot's state from the database and has the same switches as
control.bat / Telegram. It only ever reads the database, plus the switches.
Refreshes itself every 30 seconds.
"""

from __future__ import annotations

import hmac
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))      # so "import desk" works

import plotly.graph_objects as go  # noqa: E402
import streamlit as st  # noqa: E402

from desk import dashboard_data as dd  # noqa: E402
from desk.clock import Clock  # noqa: E402
from desk.config import load_config, load_secrets  # noqa: E402
from desk.controls import ControlPanel  # noqa: E402
from desk.db import Database  # noqa: E402
from desk.status import running_lines  # noqa: E402
from desk.strategy import apply_strategy  # noqa: E402

LINE_COLOUR = "#2a78d6"     # validated categorical slot 1 (dataviz reference palette)
MODE = "paper"

st.set_page_config(page_title="Crypto Desk", page_icon="📈", layout="wide")


@st.cache_resource
def resources():
    config = apply_strategy(load_config(os.environ.get("DESK_CONFIG") or None))
    secrets = load_secrets(os.environ.get("DESK_ENV") or None)
    clock = Clock(config.tz)
    db = Database(config.path("database"), clock)
    db.migrate()
    return config, secrets, clock, db


config, secrets, clock, db = resources()
panel = ControlPanel(db, clock)

# ---- optional password (set DASHBOARD_PASSWORD in .env; needed on a server) -----------
if secrets.dashboard_password and not st.session_state.get("authed"):
    st.title("Crypto Desk")
    pw = st.text_input("Password", type="password")
    if pw and hmac.compare_digest(pw, secrets.dashboard_password):
        st.session_state["authed"] = True
        st.rerun()
    elif pw:
        st.error("Wrong password.")
    st.stop()


@st.cache_data(ttl=30, show_spinner=False)
def live_quotes():
    """Public CoinSpot prices (no key needed), at most every 30 seconds."""
    from desk.exchange.coinspot import CoinSpotClient
    try:
        client = CoinSpotClient(clock=clock, max_retries=0, timeout=8)
        try:
            return client.latest_all()
        finally:
            client.close()
    except Exception:
        return None


# ---- sidebar: switches ------------------------------------------------------------------
with st.sidebar:
    st.header("Switches")
    state = panel.state()
    st.markdown(f"**{state.describe()}**")
    if st.button("⏸ Pause new trades", width="stretch", disabled=state.paused or state.killed):
        st.toast(panel.pause(by="dashboard"))
        st.rerun()
    if st.button("▶ Resume", width="stretch", disabled=not state.paused or state.killed):
        st.toast(panel.resume(by="dashboard")[1])
        st.rerun()
    st.divider()
    sure = st.checkbox("I'm sure", key="kill_sure",
                       help="Tick this, then press a red button. Stop-losses keep working after a kill.")
    if st.button("🛑 Kill (stop trading + AI)", width="stretch", type="primary", disabled=not sure or state.killed):
        st.toast(panel.kill(by="dashboard"))
        st.rerun()
    if st.button("🛑 Kill and SELL everything", width="stretch", type="primary", disabled=not sure):
        st.toast(panel.kill(by="dashboard", close_all=True))
        st.rerun()
    if state.killed and st.button("Lift the kill switch", width="stretch", disabled=not sure):
        st.toast(panel.resume(by="dashboard", confirm=True)[1])
        st.rerun()
    st.caption("Switches take effect at the bot's next price check (within a minute). "
               "Selling is never blocked by a pause or kill.")


# ---- main page (refreshes every 30s) ------------------------------------------------------
@st.fragment(run_every=30)
def main_page():
    st.title(f"Crypto Desk · {MODE.upper()} · {config.strategy.mode} strategy")
    st.caption(" · ".join(running_lines(db, clock, config)) + f" · {clock.local_now():%a %d %b %H:%M %Z}")

    k = dd.kpis(db, clock, config, MODE)
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Equity", f"A${k['equity']:,.2f}", f"{k['total_pct']:+.2f}% since start", border=True)
    c2.metric("Today", f"{k['today_pct']:+.2f}%", f"limit -{config.risk.daily_loss_limit_pct:g}%",
              delta_color="off", border=True)
    c3.metric("Open positions", k["open_positions"], f"max {config.risk.max_open_positions}", delta_color="off",
              border=True)
    c4.metric("Trades today", k["trades_today"], border=True)
    c5.metric("AI spend today", f"US${k['ai_cost_today']:.4f}", f"{k['ai_calls_today']} calls",
              delta_color="off", border=True)

    eq = dd.equity_series(db, MODE)
    st.subheader("Account value")
    if len(eq) >= 2:
        hourly = eq.resample("1h").last().dropna() if (eq.index[-1] - eq.index[0]).days >= 2 else eq
        fig = go.Figure(go.Scatter(x=hourly.index, y=hourly.values, mode="lines", line=dict(color=LINE_COLOUR, width=2),
                                   hovertemplate="A$%{y:,.2f}<extra></extra>", name="Equity"))
        fig.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10), hovermode="x unified", showlegend=False)
        fig.update_yaxes(tickprefix="A$", tickformat=",.0f", showgrid=True, gridwidth=1)
        fig.update_xaxes(showgrid=False)
        st.plotly_chart(fig, width="stretch", theme="streamlit")
    else:
        st.info("The chart appears once the bot has recorded a few price checks (run paper.bat).")

    st.subheader("Open positions")
    pos = dd.positions_df(db, clock, config, live_quotes(), MODE)
    if pos.empty:
        st.caption("No open positions. In trend mode the desk often waits days or weeks for a signal.")
    else:
        st.dataframe(pos, width="stretch", hide_index=True)

    left, right = st.columns(2)
    with left:
        st.subheader("Recent trades")
        tr = dd.trades_df(db, clock, MODE, 50)
        if tr.empty:
            st.caption("No trades yet.")
        else:
            st.dataframe(tr, width="stretch", hide_index=True)
    with right:
        st.subheader("Risk Manager decisions (last 24h)")
        dec = dd.decisions_df(db, clock, 1)
        if dec.empty:
            st.caption("None in the last 24h.")
        else:
            st.dataframe(dec, width="stretch", hide_index=True)

    st.subheader("Debates (last 3 days)")
    deb = dd.debates(db, clock, days=3)
    if not deb:
        st.caption("No debates yet. They happen when a coin gets a signal.")
    for d in deb:
        with st.expander(f"{d['when']} · {d['coin']} · {d['kind']} → {d['outcome'].upper()}"):
            p, v = d["proposal"], d["verdict"]
            if p:
                if d["kind"] == "entry":
                    st.markdown(f"**Entry Agent:** {p['decision']} (confidence {p['confidence']})")
                    if p["decision"] == "BUY":
                        st.markdown(f"entry A${p['entry_aud']:,.6g} · stop A${p['stop_loss_aud']:,.6g}")
                else:
                    st.markdown(f"**Exit Agent:** {p['action']} (confidence {p['confidence']})")
                st.write(p.get("reasoning", ""))
            if v:
                st.markdown(f"**Skeptic votes {v['vote']}**")
                for a in v["arguments_against"]:
                    st.markdown(f"- {a}")
                st.write(v["reasoning"])
            for pr in d["problems"]:
                st.warning(pr)

    a, b = st.columns(2)
    with a:
        st.subheader("AI spend today by agent")
        ai = dd.ai_by_agent(db, clock)
        if ai.empty:
            st.caption("No AI calls today.")
        else:
            st.dataframe(ai, width="stretch", hide_index=True)
    with b:
        st.subheader("News blocks")
        blocks = dd.active_blocks(db, clock)
        if blocks.empty:
            st.caption("None active.")
        else:
            st.dataframe(blocks, width="stretch", hide_index=True)

    st.subheader("Events")
    st.dataframe(dd.events_df(db, clock, 60), width="stretch", hide_index=True)


main_page()
