"""
Evening signals email (Cloud Run service `stockmailer`, entry point send_buy_list_email).

Reads today's Stage 1-3 rows (batch_status = 'MECHANICAL') from fact_stock_rankings and sends
ONE email whose top section says exactly what, if anything, to enter at Saxo tomorrow.

  1. ORDERS TO PLACE   - trade-eligible stocks as literal order tickets (side, type, quantity,
                         price, duration), plus what happens after the fill. Or, clearly,
                         "No orders to place".
  2. NEXT 10 DAYS     - FTSE 100 and FTSE 250 ex-dividend and earnings dates, with key numbers.
  3. REVIEWED TODAY    - every FTSE 100 / FTSE 250 Gemini review, for information only, each saying
                         why it is not a trade.
  4. OTHER TRIGGERS    - moves logged but not reviewed (S&P 500, no news, over the cap). AIM left out.
  5. MARKET CONTEXT    - macro banner, world-index chart, sector momentum.

Credentials come from the `mail-config` secret (Secret Manager), never from this file.

v21 (10 Oct 2026): "Early warning" card in the Saturday email (early_warning.py): 12 market-stress indicators
(yield curves, junk-bond spread, US and UK jobs, VIX, Buffett indicator, breadth, distribution days) with
traffic lights from BigQuery table ew_indicators, and what changed since last week. Every run also fetches
FRED, ONS and RSP/SPY data and rebuilds ew_daily so the Power BI page is current daily. For judgement only.
v18 (9 Oct 2026): "Pure mono" restyle -- one typeface, black/white/grey with colour only for gains and losses,
every section a white card on a grey ground; regime shown as a "ladder" (UK regime step line, FTSE 100 panel below,
chosen 9 Oct) with a summary underneath (now, days in regime, daily reading, US regime, time in each); the mix drawn as email-safe table bars instead of an image.
v17 (9 Oct 2026): links the Portfolio Rulebook (pinned page in Claude) under the header with its version, and any
change in red -- no attachment; Compounders box reworded for QGARP v1.0 (12 names, April re-rank, quarterly floor checks).
v16 (8 Oct 2026, late): adds "Month-end moves" (build 6 mix checker, vw_mix_moves) under the mix chart, and the
Compounders target list (build 7, vw_compounders_picks) in the Compounders box. Starts by refreshing
market_regime_daily (sp_refresh_market_regime) so the regime maths runs once; every regime read uses that table.
v15 (8 Oct 2026, 23:30): same as v14 (below) plus the Barclays ISA in the Portfolio section; version stamp in
the footer so the deployed version can be seen in every email.
v14 (8 Oct 2026, evening): charts sent as attached inline images at high resolution (sharp, and they no longer
count towards Gmail's clipping limit); ex-dividend and earnings tables cover today and the next 2 working days for
FTSE 100, FTSE 250 and S&P 500; the order box is labelled Opportunity and a Compounders box follows it; closed
trades get totals (portfolio sections in digest_job.py).
v13 (8 Oct 2026, build item 4): THE ONE DAILY EMAIL, sent at 07:00 as the "Morning briefing". It replaces
the 21:30 evening signals email and the 07:00 portfolio digest (digest_job.py is bundled here and its sections
reused). Order: regime + 2-year regime timeline; your mix against the regime target (whole portfolio incl.
manual_holdings); orders to place today; actions on holdings; portfolio; then calendar, reviews, other signals,
market context, closed trades, routine announcements and system health.
v12 (8 Oct 2026, build item 3): Gemini is a veto, not a gate (stages-v7). New Opportunity exits -- one resting disaster stop only (11.25x daily
volatility below the buy price), NO take-profit target, sell at the 6-month time exit (about 126 trading
days = 183 calendar days). The regime sets the stake: full / half / none (from fact_stock_rankings.
stake_multiplier, written by stages-v7). US (S&P 500) tickets are now placeable, sized in pounds at
Saxo's USD rate, with the FX cost shown. A regime line opens the email.
"""

import base64
import io
import json
import math
import os
import re
from datetime import date, datetime, timedelta

import functions_framework
import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
from google.cloud import bigquery

# =====================================================================================
# ORDER RULES -- the one place to tune what the email tells you to enter
# =====================================================================================
STAKE_GBP = 500.0                 # fixed stake per new swing trade
MAX_SHARE_PRICE_GBP = 100.0       # skip if one share costs more than this (whole shares only)
ENTRY_LIMIT_BUFFER = 0.01         # buy limit = today's close + 1%: fills near tomorrow's price,
                                  # never chases a gap up
DISASTER_VOL_MULTIPLE = 11.25     # the only stop: resting, 11.25x the 20-day daily volatility below the buy limit
                                  # (= the old disaster stop, 1.5 x 2.5 x 3). Tested 8 Oct: best with no target.
TIME_EXIT_DAYS = 183              # sell at the next open after ~126 trading days (183 calendar days)
FX_ROUND_TRIP_PCT = 0.5           # assumed Saxo currency charge on a US trade, buy + sell together
CALENDAR_WORKING_DAYS = 2         # ex-dividend and earnings tables: today and the next 2 working days
CALENDAR_MAX_ROWS = 25            # per table; your holdings first, then soonest
CALENDAR_INDEXES = ("FTSE 100", "FTSE 250", "S&P 500")  # which stocks the ex-dividend and earnings tables cover
EMAIL_INDEXES = ["FTSE 100", "FTSE 250", "S&P 500"]   # AIM is left out of the email entirely
# =====================================================================================

MAILER_VERSION = "v21"
RULEBOOK_VERSION = "v1.1 (9 Oct 2026)"
RULEBOOK_URL = "https://claude.ai/artifact/WqzDqj18NJNTW7XdHgETjo"   # the pinned page (private: opens when signed in to Claude)
RULEBOOK_CHANGED = "the two source documents (Swing Trading Recipe, QGARP spec) are now linked at the top"                 # one line on what changed, shown in the email while non-empty
PROJECT_ID = os.environ.get("GCP_PROJECT", "project-e042f011-a587-4cbe-8f7")
DATASET = f"{PROJECT_ID}.Market_Data_Project"
SEGMENTS_IN_ORDER = ["FTSE 100", "S&P 500", "FTSE 250"]
USD_SEGMENTS = {"S&P 500"}
MOMENTUM_WINDOWS_DAYS = {"1W": 7, "1M": 28, "3M": 91}

# palette (hardcoded literals for Outlook; see the design notes in the previous mailer)
# v18 "Pure mono": black, white and grey; colour only for gains (green) and losses / warnings (red)
PAPER, PANEL, INK, SOFT, FAINT, LINE = "#F2F2F2", "#FFFFFF", "#0A0A0A", "#6B6B6B", "#8F8F8F", "#EBEBEB"
ACCENT, GOOD, BAD, WARN, TINT = "#0A0A0A", "#0A7A3E", "#C2261C", "#0A0A0A", "#F5F5F5"
TRACK, RESERVE_BAR = "#EBEBEB", "#BDBDBD"
SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Helvetica Neue', Helvetica, Arial, sans-serif"
SERIF = SANS   # v18: one typeface throughout
MONO = "'SF Mono', Consolas, Monaco, monospace"

VERDICT_STYLE = {
    "OVERREACTION": (GOOD, "#EAF4EE", "Overreaction"),
    "UNDERREACTION": (GOOD, "#EAF4EE", "Underreaction"),
    "JUSTIFIED": (BAD, "#FBECEA", "Justified"),
    "NO_CATALYST": (SOFT, "#F0F0F0", "No catalyst"),
    "UNCLEAR": (SOFT, "#F0F0F0", "Unclear"),
}
TRIGGER_WORDS = {
    "EVENT_DROP": "sharp one-day drop (6-10%)", "WIDE_DROP": "large one-day drop",
    "NEWS_REACTION": "unusual move on news", "QUIET_RESULTS": "results with little reaction",
    "DISLOCATION": "fresh deep dislocation", "HELD_REVIEW": "held-stock review",
    "DIRECTOR_BUY": "director share dealing", "ANALYST_RERATE": "analyst upgrade",
    "MOMENTUM_LEADER": "joined the strongest fifth (momentum)",
}
MOMENTUM_PCTILE_FOR_TRADE = 0.80  # must match stages.py (v5): past-year return in the top fifth of its own index
SECTOR_WIDE_MOVE_PCT = -1.5       # must match stages.py (route 2: sector also fell this much)
HIDE_FROM_OTHER_SIGNALS = {"MOMENTUM_LEADER"}   # tested weak as a trigger; still logged, not emailed


# ------------------------------------------------------------------------ small helpers

def esc(x):
    return "" if x is None else (str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def is_missing(v):
    try:
        return v is None or pd.isna(v)
    except (TypeError, ValueError):
        return False


def num(v, default=None):
    return default if is_missing(v) else float(v)


def fmt_price(v, segment):
    if is_missing(v):
        return "n/a"
    v = float(v)
    if segment in USD_SEGMENTS:
        return f"${v:,.2f}"
    return f"{v:,.2f}p"


def fmt_pct(v, dp=1):
    return "n/a" if is_missing(v) else f"{float(v):+.{dp}f}%"


def fmt_gbp(v):
    return "n/a" if is_missing(v) else f"£{float(v):,.0f}"


def short_date(d):
    return f"{d.day} {d:%b}"


def next_weekday(d):
    d = d + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def mail_config():
    """mail-config secret: {smtp_user, app_password, to}. Env vars are a fallback only."""
    try:
        from google.cloud import secretmanager
        sm = secretmanager.SecretManagerServiceClient()
        name = f"projects/{PROJECT_ID}/secrets/mail-config/versions/latest"
        return json.loads(sm.access_secret_version(name=name).payload.data.decode())
    except Exception as e:  # noqa: BLE001
        print(f"mail-config secret not readable ({e}); falling back to environment variables.")
        return {"smtp_user": os.environ.get("GMAIL_ADDRESS"), "app_password": os.environ.get("GMAIL_APP_PASSWORD"),
                "to": os.environ.get("REPORT_RECIPIENT")}


# ------------------------------------------------------------------------ order maths

def plan_order(row, today, usd_to_gbp=None):
    """Turns a trade-eligible row into literal order instructions, or a reason it can't be placed."""
    seg = row["market_segment"]
    close = num(row.get("close_price"))
    vol = num(row.get("volatility_20d"))
    if not close or not vol:
        return {"skip": "no usable close price or volatility"}
    mult = num(row.get("stake_multiplier"), 1.0)
    if mult <= 0:
        return {"skip": f"the {row.get('regime') or 'current'} regime allows no new trades"}
    stake = STAKE_GBP * mult
    us = seg in USD_SEGMENTS
    limit = round(close * (1 + ENTRY_LIMIT_BUFFER), 2)
    if us:
        if not usd_to_gbp:
            return {"skip": "no USD rate available to size the trade"}
        share_gbp = limit * usd_to_gbp
        if share_gbp > stake:
            return {"skip": f"one share costs £{share_gbp:,.2f}, more than the £{stake:.0f} stake"}
    else:
        share_gbp = limit / 100.0                                 # pence -> pounds
        if share_gbp > MAX_SHARE_PRICE_GBP:
            return {"skip": f"one share costs £{share_gbp:,.2f}, above the £{MAX_SHARE_PRICE_GBP:.0f} limit"}
    shares = int(math.floor(stake / share_gbp))
    if shares < 1:
        return {"skip": "stake too small for one share"}
    disaster = round(limit * (1 - vol / 100 * DISASTER_VOL_MULTIPLE), 2)
    cost = shares * share_gbp
    fill_day = today if today.weekday() < 5 else next_weekday(today)   # v13: email arrives at 07:00, orders go in today
    return {
        "shares": shares, "limit": limit, "disaster": disaster, "stake": stake, "mult": mult,
        "cost": cost, "fill_day": fill_day, "time_exit": fill_day + timedelta(days=TIME_EXIT_DAYS),
        "risk_disaster": shares * (limit - disaster) * (usd_to_gbp if us else 0.01),
        "fx_cost": cost * FX_ROUND_TRIP_PCT / 100 if us else 0.0,
    }


def not_a_trade_reason(row):
    trig = list(row.get("trigger_types") or [])
    verdict = row.get("verdict")
    if row.get("trade_eligible"):
        return None
    if "EVENT_DROP" not in trig:
        return "The move was outside the 6-10% drop band that the trade rule uses, so this is logged for learning only."
    pct = num(row.get("momentum_pctile"))
    if pct is None or pct < MOMENTUM_PCTILE_FOR_TRADE:
        mom = num(row.get("momentum_12_1"))
        shown = ("not ranked (under a year of prices)" if pct is None else
                 f"{fmt_pct(mom, 0)}, ahead of {pct * 100:.0f}% of its index" if mom is not None else
                 f"ahead of {pct * 100:.0f}% of its index")
        return (f"It dropped 6-10%, but the rule only buys drops in stocks whose past-year rise is among the "
                f"strongest fifth of their own index; this one was {shown}.")
    if row.get("gemini_veto"):
        return (f"Gemini read the drop as genuine bad news ({VERDICT_STYLE.get(verdict, (0, 0, str(verdict).title()))[2].lower()}, "
                f"catalyst {int(num(row.get('catalyst_score'), 0)):+d}), so it vetoed the trade.")
    if num(row.get("stake_multiplier"), 1.0) <= 0:
        return f"The {row.get('regime') or 'current'} market regime allows no new trades."
    if row.get("is_trust"):
        return "Investment trusts and funds are never traded."
    return "It failed a universe filter (price, size, volatility or data quality)."


# ------------------------------------------------------------------------ charts

REGIME_COLOURS = {"Calm uptrend": "#DCE6DE", "Unsettled": "#ECEAE5", "Stress": "#F0DAD6", "Recovery": "#DAE2EC"}
REGIME_ORDER = ["Calm uptrend", "Unsettled", "Stress", "Recovery"]
CHART_W_IN = 5.32          # = the 532px content width of a card, so every chart lines up with the text
CHART_DPI = 200            # sharp on phones and retina screens


def _png(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=CHART_DPI, facecolor="white")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def _style_axes(ax):
    for side in ("top", "left", "right"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(LINE)
    ax.yaxis.tick_right()
    ax.yaxis.set_major_locator(plt.MaxNLocator(nbins=4))
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax.tick_params(axis="y", labelsize=6.5, colors=SOFT, length=0, pad=4)
    ax.tick_params(axis="x", labelsize=6.5, colors=SOFT, length=2, color=LINE)
    ax.grid(axis="y", color="#F0F0F0", linewidth=0.6)
    ax.set_axisbelow(True)


LADDER = {"Stress": 0, "Unsettled": 1, "Recovery": 2, "Calm uptrend": 3}   # bottom to top


def _ladder(ax, d, regimes):
    """One regime ladder: four lanes, a step line, today as a dot, today's lane label in bold (left gutter)."""
    lv = regimes.map(LADDER).fillna(1)
    now = regimes.iloc[-1]
    for name, v in LADDER.items():
        ax.axhline(v, color="#EFEFEF", linewidth=0.7, zorder=0)
        ax.text(-0.02, v, name.replace(" uptrend", ""), transform=ax.get_yaxis_transform(), fontsize=6.2,
                color=INK if name == now else SOFT, fontweight="bold" if name == now else "normal",
                va="center", ha="right")
    ax.step(d, lv, where="post", color="#3A3A3A", linewidth=1.2, zorder=3)
    ax.scatter([d.iloc[-1]], [lv.iloc[-1]], s=16, color=INK, zorder=4, linewidths=0, clip_on=False)
    ax.set_xlim(d.iloc[0], d.iloc[-1])
    ax.set_ylim(-0.6, 3.6)
    ax.axis("off")


SP_DOTS = (0, (0.01, 2.4))   # round dots for the S&P 500 line (and its key)
REGIME_ROWS = ("Calm uptrend", "Recovery", "Unsettled", "Stress")


def regime_timeline_chart(df):
    """v18 'stacked', variant A (chosen 9 Oct): the UK regime ladder (FTSE 100, sets the mix) above the US regime ladder
    (S&P 500, gates US trades) on one shared 2-year timeline, then a taller panel with both indexes rebased to 100:
    FTSE 100 solid, S&P 500 in round dots. Key in the left gutter."""
    try:
        df = df.copy().sort_values("regime_date")
        df["regime_date"] = pd.to_datetime(df["regime_date"])
        d = df["regime_date"]
        H = 3.7
        lad = 0.27 * 3.1 / H          # each ladder keeps its earlier height; the index panel grows
        top = 0.975
        fig = plt.figure(figsize=(CHART_W_IN, H))
        a1 = fig.add_axes([0.15, top - lad, 0.77, lad])
        a2 = fig.add_axes([0.15, top - 2 * lad - 0.06, 0.77, lad])
        a3 = fig.add_axes([0.15, 0.085, 0.77, 0.30])
        _ladder(a1, d, df["uk_regime"])
        _ladder(a2, d, df["us_regime"])
        fig.text(0.005, top - lad / 2, "UK", fontsize=7.5, fontweight="bold", color=INK, va="center")
        fig.text(0.005, top - 1.5 * lad - 0.06, "US", fontsize=7.5, fontweight="bold", color=INK, va="center")
        uk = df["ftse100_close"].astype(float)
        us = df["sp500_close"].astype(float)
        a3.plot(d, uk / uk.iloc[0] * 100, color="#3A3A3A", linewidth=1.0)
        a3.plot(d, us / us.iloc[0] * 100, color="#3A3A3A", linewidth=1.7, linestyle=SP_DOTS, dash_capstyle="round")
        a3.set_xlim(d.iloc[0], d.iloc[-1])
        for side in ("top", "right", "left"):
            a3.spines[side].set_visible(False)
        a3.spines["bottom"].set_color(LINE)
        a3.yaxis.tick_right()
        a3.yaxis.set_major_locator(plt.MaxNLocator(nbins=3))
        a3.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
        a3.grid(axis="y", color="#F2F2F2", linewidth=0.6)
        a3.set_axisbelow(True)
        a3.tick_params(colors=SOFT, labelsize=6.0, length=0)
        a3.xaxis.set_major_locator(mdates.MonthLocator(bymonth=(1, 4, 7, 10)))
        a3.xaxis.set_major_formatter(mdates.DateFormatter("%b %y"))
        for y, name, ls, lw in ((0.68, "FTSE 100", "-", 1.0), (0.38, "S&P 500", SP_DOTS, 1.7)):
            a3.text(-0.085, y, name, transform=a3.transAxes, fontsize=6.2, color=SOFT, va="center", ha="right")
            a3.plot([-0.07, -0.02], [y, y], transform=a3.transAxes, color="#3A3A3A", linewidth=lw, linestyle=ls,
                    dash_capstyle="round", clip_on=False)
        a3.text(-0.085, 0.08, "rebased 100", transform=a3.transAxes, fontsize=5.6, color=FAINT, va="center", ha="right")
        return _png(fig)
    except Exception as e:  # noqa: BLE001
        print(f"regime chart failed: {e}")
        return ""


def _share_bar(pct, fill):
    """Email-safe 8px bar (same as the mix card)."""
    pct = max(0.0, min(100.0, pct))
    cells = ""
    if pct > 0.05:
        cells += f'<td style="width:{pct:.2f}%; height:8px; background:{fill}; font-size:0; line-height:0;">&nbsp;</td>'
    if pct < 99.95:
        cells += f'<td style="height:8px; background:{TRACK}; font-size:0; line-height:0;">&nbsp;</td>'
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border-collapse:collapse; table-layout:fixed;"><tr>{cells}</tr></table>')


def _regime_column(df, col, raw_col, title, note):
    """One market: regime now, since when, the daily reading, one line of context, then time in each regime as bars."""
    r = df[col].tolist()
    now = r[-1]
    k = len(r) - 1
    while k > 0 and r[k - 1] == now:
        k -= 1
    since = pd.to_datetime(df["regime_date"].iloc[k])
    days = len(r) - k
    raw = df[raw_col].tolist() if raw_col in df.columns else [now]
    if raw[-1] != now:
        n = 0
        for x in reversed(raw):
            if x != raw[-1]:
                break
            n += 1
        reading = f'Daily reading: <b>{esc(raw[-1])}</b> for {n} day{"s" if n != 1 else ""}'
    else:
        reading = f'Daily reading: <b>{esc(raw[-1])}</b>'
    share = pd.Series(r).value_counts(normalize=True)
    rows = ""
    for x in REGIME_ROWS:
        pct = float(share.get(x, 0)) * 100
        cur = x == now
        rows += (f'<tr><td style="padding:4px 8px 4px 0; font-size:12px; white-space:nowrap; width:62px; '
                 f'color:{INK if cur else SOFT}; font-weight:{600 if cur else 400};">{x.replace(" uptrend", "")}</td>'
                 f'<td style="padding:4px 0;">{_share_bar(pct, INK if cur else "#A8A8A8")}</td>'
                 f'<td style="padding:4px 0 4px 8px; font-size:12px; text-align:right; width:30px; '
                 f'color:{INK if cur else SOFT}; font-variant-numeric:tabular-nums;">{pct:.0f}%</td></tr>')
    lab = f'font-size:11px; text-transform:uppercase; letter-spacing:0.08em; color:{SOFT};'
    return (f'<div style="{lab}">{title}</div>'
            f'<div style="font-size:18px; font-weight:700; color:{INK}; margin-top:4px;">{esc(now)}</div>'
            f'<div style="font-size:12px; color:{SOFT}; margin-top:2px;">Since {since.day} {since:%b} &middot; {days} trading days</div>'
            f'<div style="font-size:12px; color:{INK}; margin-top:4px;">{reading}</div>'
            f'<div style="font-size:12px; color:{SOFT}; margin-top:2px;">{note}</div>'
            f'<table role="presentation" width="100%" style="border-collapse:collapse; margin-top:10px;">{rows}</table>')


def regime_summary_html(df, reg):
    """v18 summary under the stacked chart (variant A): UK and US side by side, each with its own time-in-regime bars."""
    try:
        df = df.copy().sort_values("regime_date")
        r = reg or {}
        t = [r.get(k) for k in ("tgt_core_pct", "tgt_compounders_pct", "tgt_opportunity_pct", "tgt_reserve_pct")]
        uk_note = ("Mix target " + "/".join(str(int(x)) for x in t)) if all(x is not None for x in t) else "&nbsp;"
        us_now = df["us_regime"].iloc[-1]
        us_note = "US trades allowed" if us_now == "Calm uptrend" else "No new US trades"
        uk = _regime_column(df, "uk_regime", "uk_regime_raw", "UK &middot; FTSE 100 &middot; sets the mix", uk_note)
        us = _regime_column(df, "us_regime", "us_regime_raw", "US &middot; S&amp;P 500 &middot; US trades", us_note)
        return (f'<table role="presentation" width="100%" style="border-collapse:collapse; margin-top:12px; border-top:1px solid {LINE};">'
                f'<tr><td style="width:50%; vertical-align:top; padding:12px 14px 0 0;">{uk}</td>'
                f'<td style="width:50%; vertical-align:top; padding:12px 0 0 14px; border-left:1px solid {LINE};">{us}</td></tr></table>'
                f'<div style="font-size:11px; color:{FAINT}; margin-top:8px;">Bars: share of the last 2 years spent in each regime; '
                f'the dark bar is today\'s regime. A regime switches after 5 days in a row (Stress at once).</div>')
    except Exception as e:  # noqa: BLE001
        print(f"regime summary failed: {e}")
        return ""


def stock_chart(df, lines):
    """3-month chart; lines = [(value, colour, style)]."""
    try:
        df = df.copy()
        df["price_date"] = pd.to_datetime(df["price_date"])
        df = df[df["price_date"] >= df["price_date"].max() - pd.DateOffset(months=3)].sort_values("price_date")
        fig = plt.figure(figsize=(CHART_W_IN, 1.7))
        ax = fig.add_axes([0.0, 0.14, 0.905, 0.84])
        ax.plot(df["price_date"], df["close_price"], color=INK, linewidth=1.1, zorder=3)
        for val, col, style in lines:
            if val:
                ax.axhline(val, color=col, linestyle=style, linewidth=0.9, zorder=2)
        ax.set_xlim(df["price_date"].min(), df["price_date"].max())
        _style_axes(ax)
        ax.xaxis.set_major_locator(mdates.MonthLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b"))
        return _png(fig)
    except Exception as e:  # noqa: BLE001
        print(f"chart failed: {e}")
        return ""


def macro_chart(df):
    try:
        df = df.copy()
        df["price_date"] = pd.to_datetime(df["price_date"])
        df = df[df["price_date"] >= df["price_date"].max() - pd.DateOffset(years=3)].sort_values("price_date")
        fig = plt.figure(figsize=(CHART_W_IN, 1.7))
        ax = fig.add_axes([0.0, 0.14, 0.905, 0.84])
        ax.plot(df["price_date"], df["close_price"], color=INK, linewidth=1.1)
        ax.set_xlim(df["price_date"].min(), df["price_date"].max())
        _style_axes(ax)
        ax.xaxis.set_major_locator(mdates.YearLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
        return _png(fig)
    except Exception as e:  # noqa: BLE001
        print(f"macro chart failed: {e}")
        return ""


def mix_bars_html(mix):
    """v18: the mix as email-safe table bars (no image): filled = held, notch = target. Works in Gmail and Outlook."""
    rows = [m for m in mix if m["sleeve"] != "Unclassified" or (m["actual_pct"] or 0) > 0]
    tick = 0.7
    def cells(segs):
        return "".join(f'<td style="width:{w:.2f}%; height:8px; background:{c}; font-size:0; line-height:0;">&nbsp;</td>'
                       for w, c in segs if w > 0.05)
    body = ""
    for m in rows:
        a = max(0.0, min(100.0, float(m["actual_pct"] or 0)))
        t = max(0.0, min(100.0, float(m["target_pct"] or 0)))
        fill = RESERVE_BAR if m["sleeve"] == "Reserve" else INK
        if a < t:
            segs = [(a, fill), (max(t - a - tick, 0), TRACK), (tick, INK), (max(100 - t, 0), TRACK)]
        else:
            segs = [(max(t - tick / 2, 0), fill), (tick, INK if abs(a - t) < 0.5 else PANEL), (max(a - t - tick / 2, 0), fill),
                    (max(100 - a, 0), TRACK)]
        body += (f'<tr><td style="padding:6px 12px 6px 0; font-size:12.5px; color:{INK}; white-space:nowrap; width:96px;">'
                 f'{esc(m["sleeve"])}</td>'
                 f'<td style="padding:6px 0;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                 f'style="border-collapse:collapse; table-layout:fixed;"><tr>{cells(segs)}</tr></table></td>'
                 f'<td style="padding:6px 0 6px 12px; font-size:12.5px; color:{SOFT}; text-align:right; white-space:nowrap; '
                 f'width:70px; font-variant-numeric:tabular-nums;">{a:.0f} / {t:.0f}%</td></tr>')
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">'
            f'{body}</table><div style="font-size:11px; color:{FAINT}; margin-top:4px;">Bar: what you hold. Notch: the target '
            f'for today\'s regime. Figures: held / target.</div>')


INLINE_IMAGES = []   # (content_id, png bytes); filled while the email is built, attached when it is sent


def img(b64, alt):
    """v14: the picture travels as an attached inline image (cid:), not inside the HTML."""
    if not b64:
        return ""
    cid = f"chart{len(INLINE_IMAGES) + 1}"
    INLINE_IMAGES.append((cid, base64.b64decode(b64)))
    return (f'<img src="cid:{cid}" alt="{esc(alt)}" width="532" '
            f'style="width:100%; max-width:532px; height:auto; display:block;"/>')


# ------------------------------------------------------------------------ sector momentum (unchanged logic)

def fetch_sector_momentum(bq):
    query = f"""
    WITH latest_date AS (SELECT MAX(price_date) AS d FROM `{DATASET}.fact_daily_prices`),
    pw AS (
      SELECT t.ticker, t.index_name AS market_segment, f.sector, p.close_price,
        ROW_NUMBER() OVER (PARTITION BY t.ticker ORDER BY ABS(DATE_DIFF(p.price_date, (SELECT d FROM latest_date), DAY))) rn_now,
        ROW_NUMBER() OVER (PARTITION BY t.ticker ORDER BY ABS(DATE_DIFF(p.price_date, DATE_SUB((SELECT d FROM latest_date), INTERVAL 7 DAY), DAY))) rn_1w,
        ROW_NUMBER() OVER (PARTITION BY t.ticker ORDER BY ABS(DATE_DIFF(p.price_date, DATE_SUB((SELECT d FROM latest_date), INTERVAL 28 DAY), DAY))) rn_1m,
        ROW_NUMBER() OVER (PARTITION BY t.ticker ORDER BY ABS(DATE_DIFF(p.price_date, DATE_SUB((SELECT d FROM latest_date), INTERVAL 91 DAY), DAY))) rn_3m
      FROM `{DATASET}.tickers` t
      JOIN `{DATASET}.fact_fundamentals` f ON t.ticker = f.ticker
      JOIN `{DATASET}.fact_daily_prices` p ON t.ticker = p.ticker
      WHERE t.index_name IN ('FTSE 100', 'S&P 500', 'FTSE 250') AND t.is_active IS TRUE
        AND p.price_date BETWEEN DATE_SUB((SELECT d FROM latest_date), INTERVAL 140 DAY) AND (SELECT d FROM latest_date)
    ),
    tr AS (
      SELECT ticker, ANY_VALUE(market_segment) market_segment, ANY_VALUE(sector) sector,
        MAX(IF(rn_now = 1, close_price, NULL)) p0, MAX(IF(rn_1w = 1, close_price, NULL)) p1w,
        MAX(IF(rn_1m = 1, close_price, NULL)) p1m, MAX(IF(rn_3m = 1, close_price, NULL)) p3m
      FROM pw GROUP BY ticker
    ),
    r AS (
      SELECT market_segment, sector, SAFE_DIVIDE(p0 - p1w, p1w) * 100 r1w, SAFE_DIVIDE(p0 - p1m, p1m) * 100 r1m,
             SAFE_DIVIDE(p0 - p3m, p3m) * 100 r3m FROM tr WHERE p0 IS NOT NULL
    )
    SELECT market_segment, sector,
      CAST(AVG(IF(ABS(r1w) <= 80, r1w, NULL)) AS FLOAT64) ret_1w_pct,
      CAST(AVG(IF(ABS(r1m) <= 80, r1m, NULL)) AS FLOAT64) ret_1m_pct,
      CAST(AVG(IF(ABS(r3m) <= 80, r3m, NULL)) AS FLOAT64) ret_3m_pct
    FROM r GROUP BY 1, 2
    """
    try:
        df = bq.query(query).to_dataframe()
    except Exception as e:  # noqa: BLE001
        print(f"Sector momentum query failed: {e}")
        return {}
    out = {}
    for seg in SEGMENTS_IN_ORDER:
        s = df[df["market_segment"] == seg].copy()
        if s.empty:
            continue
        def lab(r):
            if pd.isna(r["ret_1m_pct"]) or pd.isna(r["ret_3m_pct"]):
                return ""
            m, q = r["ret_1m_pct"], r["ret_3m_pct"] / 3.0
            return " · accelerating" if m > q + 1 else (" · cooling" if m < q - 1 else "")
        s["momentum"] = s.apply(lab, axis=1)
        out[seg] = s.sort_values("ret_3m_pct", ascending=False)
    return out


def momentum_table(segment, df):
    def shade(p):
        if pd.isna(p): return PAPER, FAINT
        if p >= 5: return "#EAF4EE", ACCENT
        if p >= 1.5: return "#F2F7F4", GOOD
        if p > -1.5: return PAPER, SOFT
        if p > -5: return "#FBECEA", BAD
        return "#F7DEDA", "#9B1C14"
    rows = ""
    for _, r in df.iterrows():
        cells = ""
        for col, extra in (("ret_1w_pct", ""), ("ret_1m_pct", ""), ("ret_3m_pct", r["momentum"])):
            bg, fg = shade(r[col])
            cells += (f'<td style="background:{bg}; color:{fg}; text-align:center; padding:6px; font-size:12px; '
                      f'border-bottom:1px solid #F0F0F0; white-space:nowrap;">{fmt_pct(r[col])}{extra}</td>')
        rows += (f'<tr><td style="padding:6px 8px; font-size:13px; color:{INK}; border-bottom:1px solid #F0F0F0;">'
                 f'{esc(r["sector"])}</td>{cells}</tr>')
    th = f'padding:0 6px 6px; font-size:11px; text-transform:uppercase; letter-spacing:0.04em; color:{FAINT}; border-bottom:1px solid {LINE}; font-weight:600;'
    return (f'<table role="presentation" style="width:100%; border-collapse:collapse; margin-bottom:20px;">'
            f'<tr><th style="{th} text-align:left;">{esc(segment)}</th><th style="{th}">1w</th><th style="{th}">1m</th>'
            f'<th style="{th}">3m</th></tr>{rows}</table>')


# ------------------------------------------------------------------------ HTML blocks

def pill(text, fg, bg):
    return (f'<span style="display:inline-block; padding:2px 8px; border-radius:10px; background:{bg}; color:{fg}; '
            f'font-size:11px; font-weight:700; letter-spacing:0.02em;">{esc(text)}</span>')


def card(label, inner, sub="", top_bar=False):
    """v18: every section is a white card on the grey ground: small capitals label, a hairline, then content."""
    if not inner:
        return ""
    head = ""
    if label:
        sub_html = (f'<div style="font-size:12px; color:{SOFT}; margin-top:4px; line-height:1.5; text-transform:none; '
                    f'letter-spacing:0; font-weight:400;">{sub}</div>') if sub else ""
        head = (f'<div style="font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:0.08em; color:{SOFT}; '
                f'border-bottom:1px solid {LINE}; padding-bottom:8px; margin-bottom:12px;">{label}{sub_html}</div>')
    bar = f'border-top:3px solid {INK};' if top_bar else ""
    return (f'<div style="background:{PANEL}; border-radius:10px; margin:0 16px 12px; padding:16px 18px; {bar}">'
            f'{head}{inner}</div>')


def section_heading(title, sub=""):
    sub_html = f'<div style="font-size:12px; color:{SOFT}; margin-top:4px;">{sub}</div>' if sub else ""
    return (f'<div style="padding:20px 24px 12px; margin-top:22px; border-top:1px solid {LINE};">'
            f'<div style="font-family:{SERIF}; font-size:20px; font-weight:700; color:{ACCENT};">{title}</div>{sub_html}</div>')


def order_line(step, label, body):
    return (f'<tr><td style="vertical-align:top; padding:8px 10px 8px 0; width:22px; font-weight:700; color:{INK}; '
            f'font-size:13px;">{step}</td><td style="padding:8px 0; border-bottom:1px solid {LINE};">'
            f'<div style="font-size:11px; text-transform:uppercase; letter-spacing:0.04em; color:{FAINT}; font-weight:700;">{label}</div>'
            f'<div style="font-size:13px; color:{INK}; margin-top:3px; line-height:1.55;">{body}</div></td></tr>')



def numbers_line(row):
    """Compact line of key facts: price position, balance sheet, analysts, next earnings."""
    seg = row["market_segment"]
    bits = [f'close {fmt_price(row.get("close_price"), seg)}']
    if not is_missing(row.get("ret_20d")):
        bits.append(f'20-day {fmt_pct(row.get("ret_20d"))}')
    if not is_missing(row.get("pct_diff_ma200")):
        bits.append(f'vs 200-day avg {fmt_pct(row.get("pct_diff_ma200"))}')
    if not is_missing(row.get("pct_52wk_range")):
        bits.append(f'{num(row.get("pct_52wk_range")):.0f}% of 52-week range')
    if not is_missing(row.get("net_debt_ebitda")):
        bits.append(f'net debt/EBITDA {num(row.get("net_debt_ebitda")):.1f}x')
    if not is_missing(row.get("roce_pct")):
        bits.append(f'return on assets {num(row.get("roce_pct")):.0f}%')
    if not is_missing(row.get("fcf_margin_pct")):
        bits.append(f'FCF margin {num(row.get("fcf_margin_pct")):.0f}%')
    if not is_missing(row.get("pe_ratio")):
        bits.append(f'P/E {num(row.get("pe_ratio")):.0f}')
    if not is_missing(row.get("analyst_upside_to_target_pct")):
        n = row.get("analyst_count")
        bits.append(f'analyst target {fmt_pct(row.get("analyst_upside_to_target_pct"), 0)}'
                    + (f' ({int(n)} analysts)' if not is_missing(n) else ""))
    ed = as_date(row.get("earnings_date"))
    if ed:
        bits.append(f'next results {ed.day} {ed:%b}')
    return (f'<div style="font-size:12px; color:{SOFT}; margin-top:6px; line-height:1.6;">'
            + " &middot; ".join(bits) + "</div>")


BASE_RATES_PCT = {"FTSE 100": 52, "FTSE 250": 41}   # share of sharp drops that beat their index (tests)
VERDICT_PHRASE = {
    "OVERREACTION": "an overreaction", "UNDERREACTION": "an underreaction (good news not yet fully priced in)",
    "JUSTIFIED": "a justified move",
    "NO_CATALYST": "not explained by any real news", "UNCLEAR": "unclear",
}


def story_html(row, url):
    """Two or three plain-English sentences: why it was flagged, Gemini's view, the cold read."""
    seg = row["market_segment"]
    rel, sig, srel = num(row.get("rel_move_1d")), num(row.get("rel_move_sigma")), num(row.get("sector_rel_move_1d"))
    trig = row.get("trigger_primary")
    size = f"{abs(rel):.1f}%" if rel is not None else "an unusual amount"
    times = f", about {abs(sig):.1f} times its normal daily move" if sig is not None and abs(sig) >= 1.5 else ""
    direction = "fell" if (rel or 0) < 0 else "rose"
    if trig == "QUIET_RESULTS":
        s1 = (f"It published results or a trading update but barely moved "
              f"({fmt_pct(rel)} against the market).")
    elif trig == "HELD_REVIEW":
        s1 = f"This is a routine check on a stock you hold; today it {direction} {size} against the market."
    elif trig == "DIRECTOR_BUY":
        s1 = (f"A director or senior manager dealt in the shares; the stock moved {fmt_pct(rel)} "
              f"against the market today.")
    elif trig == "DISLOCATION":
        s1 = (f"It has become deeply dislocated, now {abs(num(row.get('pct_diff_ma200'), 0)):.0f}% "
              f"{'below' if num(row.get('pct_diff_ma200'), 0) < 0 else 'above'} its 200-day average.")
    else:
        on_news = " on news" if trig == "NEWS_REACTION" or row.get("has_news") else ""
        s1 = f"It {direction} {size} more than the market today{on_news}{times}."
        if rel is not None and srel is not None and abs(rel) >= 2:
            if abs(srel) < 0.5 * abs(rel):
                s1 += " Much of that was a sector-wide move rather than company-specific."
            else:
                s1 += " The move was specific to this company, not its sector."
    mom = num(row.get("momentum_12_1"))
    if mom is not None:
        pct = num(row.get("momentum_pctile"))
        trend = ("among the strongest fifth of its index" if pct is not None and pct >= MOMENTUM_PCTILE_FOR_TRADE else
                 "an uptrend, but not in the top fifth of its index" if mom >= 0 else "a downtrend")
        s1 += f" Over the past year (excluding the last month) it was {fmt_pct(mom, 0)}, {trend}."
    v = row.get("verdict") or "UNCLEAR"
    p = num(row.get("prob_beat_ftse_90d"))
    base = BASE_RATES_PCT.get(seg)
    cat = int(num(row.get("catalyst_score"), 0))
    phrase = VERDICT_PHRASE.get(v, v.lower())
    if v == "OVERREACTION" and (rel or 0) > 0:
        phrase = "an overreaction (the rise looks too big)"
    s2 = f"Gemini read the announcement and judged it {phrase} (catalyst score {cat:+d})"
    if p is not None:
        s2 += f", giving it a {p * 100:.0f}% chance of beating the {esc(seg)} over 90 days"
        if base:
            s2 += f" against a typical {base}%"
    s2 += "."
    blind = num(row.get("blind_predicted_move"))
    s3 = ""
    if blind is not None and rel is not None:
        gap = rel - blind
        if gap <= -3:
            tail = "a much harsher reaction than the news alone suggests."
        elif gap >= 3:
            tail = "a much milder reaction than the news alone suggests."
        else:
            tail = "broadly in line with the news."
        s3 = (f"Reading it cold, without being told the price, Gemini expected {fmt_pct(blind)}; "
              f"the market did {fmt_pct(rel)}, {tail}")
    link = (f' <a href="{esc(url)}" style="color:{ACCENT}; text-decoration:none; white-space:nowrap;">'
            f'Read the announcement &rarr;</a>') if url else ""
    cold = (f'<div style="font-size:13px; color:{SOFT}; line-height:1.55; margin-top:8px;">{esc(s3)}</div>'
            if s3 else "")
    return (f'<div style="font-size:14px; color:{INK}; line-height:1.6; margin-top:10px;">{esc(s1)} {esc(s2)}</div>'
            f'{cold}<div style="margin-top:6px; font-size:13px;">{link}</div>')


def rank_phrase(pct):
    """0.86 -> 'top 14% of index'; 0.2 -> 'bottom 20% of index' (past-year momentum rank within own index)."""
    if pct >= 0.5:
        return f"top {max(1, round((1 - pct) * 100))}% of index"
    return f"bottom {max(1, round(pct * 100))}% of index"


def facts_grid(row):
    """Key numbers as a labelled two-column grid (label above value), skipping anything missing."""
    seg = row["market_segment"]
    items = [("Price", fmt_price(row.get("close_price"), seg) if not is_missing(row.get("close_price")) else None),
             ("Past-year trend", fmt_pct(row.get("momentum_12_1"), 0)
              + (" · " + rank_phrase(num(row.get("momentum_pctile"))) if not is_missing(row.get("momentum_pctile"))
                 else " (ex. last month)")
              if not is_missing(row.get("momentum_12_1")) else None),
             ("20-day change", fmt_pct(row.get("ret_20d")) if not is_missing(row.get("ret_20d")) else None),
             ("vs 200-day average", fmt_pct(row.get("pct_diff_ma200")) if not is_missing(row.get("pct_diff_ma200")) else None),
             ("52-week position", f'{num(row.get("pct_52wk_range")):.0f}% of range' if not is_missing(row.get("pct_52wk_range")) else None),
             ("Net debt / EBITDA", f'{num(row.get("net_debt_ebitda")):.1f}x' if not is_missing(row.get("net_debt_ebitda")) else None),
             ("Return on assets", f'{num(row.get("roce_pct")):.0f}%' if not is_missing(row.get("roce_pct")) else None),
             ("Free cash flow margin", f'{num(row.get("fcf_margin_pct")):.0f}%' if not is_missing(row.get("fcf_margin_pct")) else None),
             ("P/E", f'{num(row.get("pe_ratio")):.0f}' if not is_missing(row.get("pe_ratio")) else None)]
    if not is_missing(row.get("analyst_upside_to_target_pct")):
        n = row.get("analyst_count")
        items.append(("Analyst target", fmt_pct(row.get("analyst_upside_to_target_pct"), 0)
                      + (f" ({int(n)} analysts)" if not is_missing(n) else "")))
    ed = as_date(row.get("earnings_date"))
    items.append(("Next results", f"{ed.day} {ed:%b %Y}" if ed else None))
    q = num(row.get("quote_check_rate"))
    if q is not None:
        items.append(("Quotes checked", f"{q * 100:.0f}% found in source"))
    items = [(k, v) for k, v in items if v]
    cells = ""
    for i in range(0, len(items), 2):
        pair = items[i:i + 2]
        cells += "<tr>"
        for k, v in pair:
            cells += (f'<td style="width:50%; padding:6px 8px 6px 0; vertical-align:top; border-top:1px solid #F0F0F0;">'
                      f'<div style="font-size:10px; text-transform:uppercase; letter-spacing:0.04em; color:{FAINT};">{esc(k)}</div>'
                      f'<div style="font-size:14px; color:{INK}; font-weight:600; margin-top:1px;">{esc(v)}</div></td>')
        if len(pair) == 1:
            cells += '<td style="width:50%; border-top:1px solid #F0F0F0;"></td>'
        cells += "</tr>"
    return (f'<table role="presentation" style="width:100%; border-collapse:collapse; margin-top:12px; '
            f'table-layout:fixed;">{cells}</table>')


def route_label(row):
    """What Gemini said, in words (v12: Gemini is a veto, not a gate)."""
    v = row.get("verdict")
    if row.get("review_status") != "REVIEWED":
        return f'{pill("Rule only", SOFT, "#F0F0F0")} no announcement for Gemini to read, so no veto'
    if v == "OVERREACTION":
        return (f'Gemini: {pill("Overreaction", GOOD, "#EAF4EE")} catalyst '
                f'{int(num(row.get("catalyst_score"), 0)):+d}')
    return f'Gemini: {pill("No red flags", GOOD, "#EAF4EE")} read as {esc((v or "unclear").replace("_", " ").lower())}, not bad enough to veto'


def order_ticket(row, plan, chart_b64):
    seg = row["market_segment"]
    t, fp = row["ticker"], (lambda v: fmt_price(v, seg))
    code = f'<span style="font-family:{MONO}; font-weight:700;">'
    half = (f' <b>Half stake</b> ({esc(row.get("regime"))} market).' if plan["mult"] < 1 else '')
    fx = (f' US shares: Saxo converts pounds to dollars when it buys and back when it sells; allow about '
          f'{fmt_gbp(plan["fx_cost"])} for that ({FX_ROUND_TRIP_PCT:.1f}% round trip).' if plan["fx_cost"] else '')
    def stat(label, value, first=False):
        edge = "" if first else f"border-left:1px solid {LINE}; padding-left:14px;"
        return (f'<td style="vertical-align:top; width:33%; {edge}"><div style="font-size:11px; color:{SOFT};">{label}</div>'
                f'<div style="font-size:15px; font-weight:600; color:{INK}; margin-top:2px; font-variant-numeric:tabular-nums;">'
                f'{value}</div></td>')
    grid = (f'<table role="presentation" width="100%" style="border-collapse:collapse; margin-top:12px;"><tr>'
            f'{stat("Buy &middot; limit &middot; day", f"{plan["shares"]} @ {fp(plan["limit"])}", True)}'
            f'{stat("Attached stop", fp(plan["disaster"]))}{stat("Sell by", short_date(plan["time_exit"]) + f" {plan["time_exit"]:%Y}")}'
            f'</tr></table>')
    ticket = (f'<div style="font-family:{MONO}; font-size:12.5px; background:{TINT}; padding:10px 12px; margin-top:12px; '
              f'line-height:1.7; color:{INK};">BUY {plan["shares"]} {esc(t)} &middot; Limit {fp(plan["limit"])} &middot; Day<br>'
              f'Attach only: Stop loss {fp(plan["disaster"])} &middot; no take-profit</div>')
    after = (f'Check in Saxo that the position shows the attached stop. If your ticket had no attach option, '
             f'place it now: {code}Stop</span> sell at {code}{fp(plan["disaster"])}</span>, {code}Good till cancelled</span>.')
    watch = (f'Nothing more to do until the 6-month exit, about <b>{short_date(plan["time_exit"])}</b>. '
             f'Your morning email will say <b>sell at the next open</b> then: <b>cancel the stop order first</b>, then sell. '
             f'Expect about half of these trades to lose a little; the few big winners carry the result.')
    notes = (f'<div style="font-size:12px; color:{SOFT}; margin-top:10px; line-height:1.6;">No take-profit: winners are left to run. '
             f'Cost about {fmt_gbp(plan["cost"])}.{half}{fx} If the buy doesn\'t fill today the order lapses and the trade is skipped. '
             f'Worst case, at the stop: about <b style="color:{BAD};">&minus;{fmt_gbp(plan["risk_disaster"])}</b> before costs.</div>')
    return (f'<div style="padding:2px 0 14px; margin-bottom:14px; border-bottom:1px solid {LINE};">'
            f'<div style="font-size:20px; font-weight:700; color:{INK};">{esc(t)} '
            f'<span style="font-size:13px; font-weight:400; color:{SOFT};">{esc(row.get("company_name"))} &middot; '
            f'{fmt_pct(row.get("rel_move_1d"))} vs market</span></div>'
            f'<div style="font-size:12px; color:{SOFT}; margin-top:4px; line-height:1.5;">{esc(seg)} &middot; {route_label(row)} '
            f'&middot; chance of beating the index {num(row.get("prob_beat_ftse_90d"), 0) * 100:.0f}%</div>'
            f'{grid}{ticket}{notes}'
            f'<table role="presentation" style="width:100%; border-collapse:collapse; margin-top:8px;">'
            f'{order_line("1", "After it fills", after)}{order_line("2", "After that", watch)}</table>'
            f'{facts_grid(row)}'
            f'<div style="margin-top:12px;">{img(chart_b64, t + " 3-month chart")}</div>'
            f'<div style="font-size:11px; color:{FAINT}; margin-top:4px;">3 months &middot; red dotted line: the stop</div></div>')


def review_card(row, url, chart_b64):
    seg = row["market_segment"]
    rj = {}
    try:
        rj = json.loads(row.get("review_json") or "{}")
    except (TypeError, ValueError):
        pass
    v = row.get("verdict") or "UNCLEAR"
    fg, bg, word = VERDICT_STYLE.get(v, (SOFT, "#F0F0F0", str(v).title()))
    if v == "OVERREACTION" and num(row.get("rel_move_1d"), 0) > 0:
        fg, bg, word = BAD, "#FBECEA", "Overreaction (rise)"
    tags = ", ".join(t.replace("_", " ").lower() for t in (row.get("setup_tags") or []))
    blind = num(row.get("blind_predicted_move"))
    blind_html = ""
    if blind is not None:
        gap = num(row.get("rel_move_1d"), 0) - blind
        blind_html = (f'<div style="font-size:12px; color:{SOFT}; margin-top:6px;">Cold read (Gemini not told the price): '
                      f'expected <b>{fmt_pct(blind)}</b>; actual vs market <b>{fmt_pct(row.get("rel_move_1d"))}</b>'
                      f' &middot; gap {fmt_pct(gap)}</div>')
    facts = ""
    for f in (rj.get("facts") or [])[:2]:
        q = f.get("quote") if isinstance(f, dict) else None
        if q and q != "unknown":
            facts += (f'<div style="font-size:12px; color:{SOFT}; border-left:2px solid {LINE}; padding-left:8px; '
                      f'margin-top:6px; font-style:italic;">&ldquo;{esc(q)}&rdquo;</div>')
    def block(label, text, colour=FAINT):
        return (f'<div style="margin-top:12px;"><div style="font-size:10px; font-weight:700; text-transform:uppercase; '
                f'letter-spacing:0.04em; color:{colour};">{label}</div><div style="font-size:13px; color:{SOFT}; '
                f'line-height:1.55; margin-top:3px;">{esc(text)}</div></div>') if text else ""
    link = f' &middot; <a href="{esc(url)}" style="color:{ACCENT}; text-decoration:none;">announcement</a>' if url else ""
    q = num(row.get("quote_check_rate"))
    qtxt = f" &middot; quotes verified {q * 100:.0f}%" if q is not None else ""
    return (f'<div style="padding:14px 0; border-top:1px solid {LINE};">'
            f'<div><span style="font-family:{SERIF}; font-size:20px; font-weight:700; color:{INK};">{esc(row["ticker"])}</span>'
            f'<span style="font-size:13px; color:{SOFT}; margin-left:8px;">{esc(row.get("company_name"))} &middot; {esc(seg)}</span></div>'
            f'<div style="margin-top:8px;">{pill(word, fg, bg)}</div>'
            f'{story_html(row, url)}{facts_grid(row)}'
            f'<div style="background:#F5F5F5; padding:8px 10px; margin-top:10px; font-size:12px; color:{INK};">'
            f'<b>Not a trade:</b> {esc(not_a_trade_reason(row))}</div>'
            f'{block("What changed", rj.get("what_changed"))}{facts}'
            f'{block("Case for", rj.get("bull_case"))}{block("Case against", rj.get("bear_case"), BAD)}'
            f'{block("Tags", tags)}{block("Key uncertainty", rj.get("key_uncertainty"))}'
            f'<div style="margin-top:12px;">{img(chart_b64, row["ticker"] + " 3-month chart")}</div></div>')


def signal_value(r):
    """The number that matters for each trigger type, coloured."""
    t = r.get("trigger_primary")
    if t == "MOMENTUM_LEADER" and not is_missing(r.get("momentum_12_1")):
        return f'<span style="color:{GOOD};">{fmt_pct(r.get("momentum_12_1"), 0)} yr</span>'
    if t == "ANALYST_RERATE":
        if not is_missing(r.get("analyst_target_chg_20d")) and num(r.get("analyst_target_chg_20d")) >= 10:
            return f'<span style="color:{GOOD};">target {fmt_pct(r.get("analyst_target_chg_20d"), 0)}</span>'
        return f'<span style="color:{GOOD};">rating up</span>'
    if t == "DISLOCATION":
        return f'<span style="color:{SOFT};">score {num(r.get("score_mech"), 0):.1f}</span>'
    v = num(r.get("rel_move_1d"), 0)
    return f'<span style="color:{BAD if v < 0 else GOOD};">{fmt_pct(v)}</span>'


def others_table(rows):
    th = f'padding:0 6px 6px; font-size:10px; text-transform:uppercase; letter-spacing:0.04em; color:{FAINT}; border-bottom:1px solid {LINE}; font-weight:600;'
    body = ""
    for r in rows:
        body += (f'<tr><td style="padding:6px; font-size:13px; border-bottom:1px solid #F0F0F0;"><b>{esc(r["ticker"])}</b>'
                 f'<div style="font-size:10px; color:{FAINT};">{esc((r.get("company_name") or "")[:28])}</div></td>'
                 f'<td style="padding:6px; font-size:12px; color:{SOFT}; border-bottom:1px solid #F0F0F0;">{esc(r["market_segment"])}</td>'
                 f'<td style="padding:6px; font-size:12px; color:{SOFT}; border-bottom:1px solid #F0F0F0;">{esc(TRIGGER_WORDS.get(r.get("trigger_primary"), r.get("trigger_primary")))}</td>'
                 f'<td style="padding:6px; font-size:12px; text-align:right; white-space:nowrap; border-bottom:1px solid #F0F0F0;">'
                 f'{signal_value(r)}</td></tr>')
    return (f'<div><table role="presentation" style="width:100%; border-collapse:collapse;">'
            f'<tr><th style="{th} text-align:left;">Stock</th><th style="{th} text-align:left;">Index</th>'
            f'<th style="{th} text-align:left;">Trigger</th><th style="{th} text-align:right;">Signal</th></tr>{body}</table></div>')



# ------------------------------------------------------------------------ calendar tables

SEG_SHORT = {"FTSE 100": "100", "FTSE 250": "250", "S&P 500": "US", "AIM": "AIM"}


def calendar_end(today):
    """Last day the calendar covers: the 2nd working day after today (weekends skipped, not bank holidays)."""
    d = today
    for _ in range(CALENDAR_WORKING_DAYS):
        d = next_weekday(d)
    return d


def load_calendar(bq, today):
    """Stocks in the calendar indexes with an ex-dividend or earnings date in the window."""
    end = calendar_end(today).isoformat()
    rows = bq.query(f"""
        WITH latest AS (SELECT MAX(ranking_date) AS rd FROM `{DATASET}.fact_stock_rankings` WHERE batch_status = 'MECHANICAL')
        SELECT ticker, company_name, market_segment, ex_div_date, earnings_date, score_mech, is_held,
               close_price, ret_20d, pct_diff_ma200, analyst_upside_to_target_pct
        FROM `{DATASET}.fact_stock_rankings`
        WHERE batch_status = 'MECHANICAL' AND ranking_date = (SELECT rd FROM latest)
          AND market_segment IN UNNEST({list(CALENDAR_INDEXES)})
          AND (ex_div_date BETWEEN '{today.isoformat()}' AND '{end}'
               OR earnings_date BETWEEN '{today.isoformat()}' AND '{end}')""").result()
    return [dict(r) for r in rows]


def as_date(v):
    if is_missing(v):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def short_name(name):
    n = re.sub(r"(?i)\b(plc|group|holdings|limited|ltd|inc\.?|corporation|corp\.?)\b", "", name or "")
    n = re.sub(r"\s+", " ", n).strip(" ,.")
    return n[:18] + ("\u2026" if len(n) > 18 else "")


def short_price(v, segment):
    if is_missing(v):
        return "n/a"
    v = float(v)
    if segment in USD_SEGMENTS:
        return f"${v:,.0f}" if v >= 100 else f"${v:,.2f}"
    return f"{v:,.0f}p" if v >= 100 else f"{v:,.2f}p"


def calendar_table(title, rows, field, today, max_rows=None):
    max_rows = max_rows or CALENDAR_MAX_ROWS
    hits = []
    for r in rows:
        d = as_date(r.get(field))
        if d and today <= d <= calendar_end(today):
            hits.append((r, d))
    hits.sort(key=lambda x: (not x[0].get("is_held"), x[1], x[0]["market_segment"], x[0]["ticker"]))
    shown, extra = hits[:max_rows], len(hits) - max_rows
    th = (f'padding:0 3px 5px; font-size:10px; text-transform:uppercase; letter-spacing:0.03em; color:{FAINT}; '
          f'border-bottom:1px solid {LINE}; font-weight:600;')
    td = 'padding:6px 3px; font-size:12px; border-bottom:1px solid #F0F0F0; vertical-align:top;'
    def pc(v):
        if is_missing(v):
            return f'<span style="color:{FAINT};">&ndash;</span>'
        v = float(v)
        return f'<span style="color:{GOOD if v > 0 else BAD if v < 0 else SOFT};">{v:+.0f}%</span>'
    body = ""
    for r, d in shown:
        mark = f'<span style="color:{ACCENT}; font-weight:700;">&#9679;</span> ' if r.get("is_held") else ""
        days = (d - today).days
        sc = num(r.get("score_mech"))
        body += (f'<tr><td style="{td}">{mark}<b style="color:{INK};">{esc(r["ticker"])}</b> '
                 f'<span style="font-size:9px; color:{FAINT};">{SEG_SHORT.get(r["market_segment"], "")}</span>'
                 f'<div style="font-size:10px; color:{FAINT}; white-space:nowrap; overflow:hidden;">{esc(short_name(r.get("company_name")))}</div></td>'
                 f'<td style="{td} white-space:nowrap; color:{WARN if days == 0 else SOFT};">{"Today" if days == 0 else f"{d:%a} {d.day} {d:%b}"}</td>'
                 f'<td style="{td} text-align:right; white-space:nowrap;">{short_price(r.get("close_price"), r["market_segment"])}</td>'
                 f'<td style="{td} text-align:right;">{pc(r.get("ret_20d"))}</td>'
                 f'<td style="{td} text-align:right;">{pc(r.get("pct_diff_ma200"))}</td>'
                 f'<td style="{td} text-align:right; color:{INK if sc and sc >= 6 else SOFT};">{"" if sc is None else f"{sc:.1f}"}</td></tr>')
    if not shown:
        body = f'<tr><td colspan="6" style="{td} color:{FAINT};">None today or in the next {CALENDAR_WORKING_DAYS} working days.</td></tr>'
    more = (f'<div style="font-size:11px; color:{FAINT}; margin-top:4px;">+{extra} more not listed</div>' if extra > 0 else "")
    return (f'<div style="font-size:13px; font-weight:700; color:{INK}; margin:4px 0 6px;">{title} '
            f'<span style="font-weight:400; color:{FAINT};">({len(hits)})</span></div>'
            f'<table role="presentation" style="width:100%; border-collapse:collapse; table-layout:fixed;">'
            f'<colgroup><col><col style="width:74px"><col style="width:62px"><col style="width:40px">'
            f'<col style="width:44px"><col style="width:38px"></colgroup>'
            f'<tr><th style="{th} text-align:left;">Stock</th><th style="{th} text-align:left;">Date</th>'
            f'<th style="{th} text-align:right;">Close</th><th style="{th} text-align:right;">20d</th>'
            f'<th style="{th} text-align:right;">200d</th><th style="{th} text-align:right;">Score</th></tr>'
            f'{body}</table>{more}')


def calendar_title(today):
    end = calendar_end(today)
    return f"Ex-dividend and results: today to {end:%a} {end.day} {end:%b}"


CALENDAR_NOTE = ("FTSE 100, FTSE 250 and S&amp;P 500 (100 / 250 / US after the ticker). &#9679; = you hold it, listed first. "
                 "20d = 20-day return. 200d = distance from the 200-day average. Score: 6+ is dislocated.")


def calendar_block(cal_rows, today, max_rows=None):
    return (calendar_table("Ex-dividend", cal_rows, "ex_div_date", today, max_rows)
            + '<div style="height:14px;"></div>'
            + calendar_table("Results / earnings", cal_rows, "earnings_date", today, max_rows))


# ------------------------------------------------------------------------ data

def load_today(bq):
    rows = list(bq.query(f"""
        WITH latest AS (SELECT MAX(ranking_date) AS rd FROM `{DATASET}.fact_stock_rankings` WHERE batch_status = 'MECHANICAL')
        SELECT r.*, n.URL AS rns_url
        FROM `{DATASET}.fact_stock_rankings` r
        LEFT JOIN `{DATASET}.fact_rns_announcements` n ON n.rns_id = r.news_ids[SAFE_OFFSET(0)]
        WHERE r.batch_status = 'MECHANICAL' AND r.ranking_date = (SELECT rd FROM latest)
          AND ARRAY_LENGTH(r.trigger_types) > 0""").result())
    return [dict(r) for r in rows]


def build_email(rows, macro, history, momentum, today, run_date, price_date, cal_rows=None, light=0,
                regime=None, usd_to_gbp=None, timeline=None, mix=None, digest=None, moves_html="", picks=None,
                review_html="", ew_html=""):
    """light (v14): 0 or 1 = full; 2 = calendar tables cut to 10 rows each. Charts are attached images, so they
    never count towards Gmail's ~100KB clipping limit and are never dropped."""
    INLINE_IMAGES.clear()
    fp_rows = lambda f: [r for r in rows if f(r)]
    candidates = fp_rows(lambda r: r.get("trade_eligible"))
    reviewed = fp_rows(lambda r: r.get("review_status") == "REVIEWED" and not r.get("trade_eligible"))
    others = fp_rows(lambda r: r.get("review_status") in ("OUT_OF_SCOPE", "NO_NEWS_SHORTCUT", "SKIPPED_CAP", "REVIEW_ERROR", "LOGGED", None)
                     and r.get("trigger_primary") not in HIDE_FROM_OTHER_SIGNALS
                     and not r.get("trade_eligible") and r["market_segment"] in EMAIL_INDEXES)
    others.sort(key=lambda r: (SEGMENTS_IN_ORDER.index(r["market_segment"]) if r["market_segment"] in SEGMENTS_IN_ORDER else 9,
                               num(r.get("rel_move_1d"), 0)))

    def hist(t):
        return history[history["ticker"] == t] if history is not None and not history.empty else pd.DataFrame()

    # ---- Opportunity
    tickets, skipped, n_orders = "", [], 0
    for r in candidates:
        plan = plan_order(r, today, usd_to_gbp)
        if "skip" in plan:
            skipped.append((r, plan["skip"]))
            continue
        chart = stock_chart(hist(r["ticker"]), [(plan["disaster"], BAD, ":")])
        tickets += order_ticket(r, plan, chart)
        n_orders += 1
    n_rev = len([r for r in rows if r.get("review_status") == "REVIEWED"])
    if n_orders:
        opp_head = (f'<div style="font-size:15px; font-weight:700; color:{INK};">{n_orders} order{"s" if n_orders > 1 else ""} '
                    f'to place today</div><div style="font-size:12.5px; color:{SOFT}; margin:3px 0 14px;">Enter exactly what is '
                    f'shown. Everything else in this email is for information.</div>')
    else:
        opp_head = (f'<div style="font-size:15px; font-weight:700; color:{INK};">No orders to place</div>'
                    f'<div style="font-size:13px; color:{SOFT}; margin-top:6px; line-height:1.55;">{n_rev} stock'
                    f'{"s" if n_rev != 1 else ""} {"was" if n_rev == 1 else "were"} reviewed and none met the trade rule '
                    f'(a 6-10% drop in a stock whose past-year rise is in the top fifth of its index, in a regime that allows '
                    f'trading, unless Gemini finds genuine bad news).</div>')
    skip_html = "".join(
        f'<div style="font-size:12px; color:{SOFT}; margin-top:6px;">Qualified but not placeable: <b style="color:{INK};">'
        f'{esc(r["ticker"])}</b> &mdash; {esc(why)}.</div>' for r, why in skipped)
    opp_card = card("Opportunity", opp_head + tickets + skip_html, top_bar=bool(n_orders))

    # ---- header
    reg = regime or {}
    chip = (f'{n_orders} order{"s" if n_orders != 1 else ""} today' if n_orders else "No orders today")
    chip_style = (f'background:{INK}; color:#FFFFFF;' if n_orders else f'background:#E3E3E3; color:{INK};')
    changed = (f' &middot; <b style="color:{BAD};">Changed: {esc(RULEBOOK_CHANGED)}</b>' if RULEBOOK_CHANGED else "")
    header = (f'<table role="presentation" width="100%" style="border-collapse:collapse;"><tr>'
              f'<td style="padding:28px 20px 16px; vertical-align:bottom;">'
              f'<div style="font-size:12px; letter-spacing:0.08em; text-transform:uppercase; color:{SOFT};">'
              f'{today:%A} {today.day} {today:%B %Y}</div>'
              f'<div style="font-size:26px; font-weight:700; letter-spacing:-0.02em; color:{INK}; margin-top:4px;">Morning briefing</div>'
              f'<div style="font-size:12px; color:{SOFT}; margin-top:4px; line-height:1.5;">Portfolio Rulebook {esc(RULEBOOK_VERSION)} '
              f'&middot; <a href="{RULEBOOK_URL}" style="color:{INK};">open in Claude</a>{changed}</div></td>'
              f'<td style="padding:28px 20px 18px; vertical-align:bottom; text-align:right; white-space:nowrap;">'
              f'<span style="{chip_style} font-size:12px; font-weight:600; padding:6px 10px; border-radius:6px;">{chip}</span>'
              f'</td></tr></table>')

    # ---- regime and total
    stake_word = {"Calm uptrend": "Full stake", "Unsettled": "Half stake"}
    def stat(label, value, sub, first=False):
        edge = "padding-left:0;" if first else f"border-left:1px solid {LINE};"
        return (f'<td style="vertical-align:top; width:33%; padding:0 14px; {edge}">'
                f'<div style="font-size:11px; text-transform:uppercase; letter-spacing:0.08em; color:{SOFT};">{label}</div>'
                f'<div style="font-size:16px; font-weight:600; color:{INK}; margin-top:4px; font-variant-numeric:tabular-nums;">{value}</div>'
                f'<div style="font-size:12px; color:{SOFT}; margin-top:2px;">{sub}</div></td>')
    uk, us = reg.get("uk_regime"), reg.get("us_regime")
    total = (mix[0].get("portfolio_total_gbp") if mix else None)
    stats = (f'<table role="presentation" width="100%" style="border-collapse:collapse;"><tr>'
             f'{stat("UK regime", esc(uk or "Unknown"), stake_word.get(uk, "No new trades"), True)}'
             f'{stat("US regime", esc(us or "Unknown"), "Full stake" if us == "Calm uptrend" else "No new trades")}'
             f'{stat("Both ISAs", fmt_gbp(total) if total else "n/a", "Saxo + Barclays")}</tr></table>')
    building = ""
    if reg.get("uk_regime_raw") and uk and reg["uk_regime_raw"] != uk:
        building = (f'<div style="font-size:12px; color:{INK}; margin-top:12px; padding-top:10px; border-top:1px solid {LINE};">'
                    f'<b>A change is building:</b> the daily UK reading says {esc(reg["uk_regime_raw"])}; it counts after 5 days in a row.</div>')
    stats_card = card(None, stats)
    timeline_card = ""
    if timeline is not None and not timeline.empty:
        timeline_card = card("Regimes, last 2 years",
                             img(regime_timeline_chart(timeline), "UK and US regime ladders over two years on one timeline, FTSE 100 and S&P 500 below")
                             + regime_summary_html(timeline, reg),
                             sub="UK regime from the FTSE 100 sets the mix; US regime from the S&amp;P 500 gates US trades. "
                                 "Indexes rebased to 100: FTSE solid, S&amp;P dotted.")

    # ---- mix and month-end moves
    mix_card = ""
    if mix:
        uncl = next((m for m in mix if m["sleeve"] == "Unclassified" and (m["value_gbp"] or 0) > 0), None)
        mix_card = card("Mix against target", mix_bars_html(mix)
                        + (f'<div style="font-size:12px; color:{BAD}; margin-top:6px;">{fmt_gbp(uncl["value_gbp"])} is not yet '
                           f'tagged to a sleeve.</div>' if uncl else "")
                        + (f'<div style="margin-top:12px; padding-top:12px; border-top:1px solid {LINE};">{moves_html}</div>'
                           if moves_html else ""),
                        sub=(f'Target for {esc(uk)}: Core {reg.get("tgt_core_pct", "?")}% &middot; Compounders '
                             f'{reg.get("tgt_compounders_pct", "?")}% &middot; Opportunity {reg.get("tgt_opportunity_pct", "?")}% '
                             f'&middot; Reserve {reg.get("tgt_reserve_pct", "?")}%' if uk else ""))

    # ---- Compounders
    comp = next((m for m in (mix or []) if m["sleeve"] == "Compounders"), None)
    comp_card = ""
    if comp:
        tot = comp.get("portfolio_total_gbp") or 0
        tgt_gbp = (comp["target_pct"] or 0) / 100 * tot
        gap = tgt_gbp - (comp["value_gbp"] or 0)
        gap_txt = (f'about {fmt_gbp(gap)} below target' if gap > 0.02 * tot else
                   f'about {fmt_gbp(-gap)} above target' if gap < -0.02 * tot else 'on target')
        picks_html = ""
        if picks:
            each = tgt_gbp / max(len(picks), 1) if tgt_gbp else 0
            td = f'padding:6px 4px; font-size:12.5px; border-bottom:1px solid {LINE}; color:{INK};'
            body = "".join(
                f'<tr><td style="{td} color:{FAINT}; width:18px;">{p["pick_no"]}</td>'
                f'<td style="{td}"><b>{esc(p["ticker"])}</b> <span style="color:{SOFT};">{esc(short_name(p.get("company_name")))}</span></td>'
                f'<td style="{td} color:{SOFT};">{esc(p.get("sector") or "")}</td>'
                f'<td style="{td} text-align:right; color:{SOFT}; white-space:nowrap;">#{p.get("compounder_rank")}</td>'
                f'<td style="{td} text-align:right; width:40px;">{"held" if p.get("is_held_long_term") else ""}</td></tr>'
                for p in picks)
            picks_html = (f'<div style="font-size:12px; color:{SOFT}; margin:12px 0 4px; line-height:1.5;"><b style="color:{INK};">'
                          f'Target list</b> &middot; QGARP v1.0: {len(picks)} US names, about {fmt_gbp(each)} each, no sector '
                          f'above 25%. Watch flags go to the Gemini veto review before any money goes in. MNDI.L stays as a legacy '
                          f'holding; GOOG ranks outside the top 30, so the April re-rank will flag it.</div>'
                          f'<table role="presentation" width="100%" style="border-collapse:collapse;">{body}</table>')
        comp_card = card("Compounders",
                         f'<div style="font-size:15px; font-weight:700; color:{INK};">No orders today</div>'
                         f'<div style="font-size:13px; color:{SOFT}; margin-top:6px; line-height:1.55;">You hold '
                         f'{fmt_gbp(comp["value_gbp"])} ({(comp["actual_pct"] or 0):.0f}%) against a target of {fmt_gbp(tgt_gbp)} '
                         f'({comp["target_pct"]:.0f}%): {gap_txt}. Re-ranked each April; held names floor-checked each quarter; '
                         f'buys go in three monthly tranches.</div>{picks_html}')
    dg = digest or {}

    # ---- reviewed
    rev_html = ""
    for r in sorted(reviewed, key=lambda r: num(r.get("rel_move_1d"), 0)):
        chart = stock_chart(hist(r["ticker"]), [])
        rev_html += review_card(r, r.get("rns_url"), chart)
    if not rev_html:
        rev_html = f'<div style="font-size:13px; color:{SOFT};">No stocks triggered a review today.</div>'

    # ---- market context
    risk = (macro or {}).get("macro_risk_level") or "MODERATE"
    risk_col = {"LOW": GOOD, "MODERATE": INK, "HIGH": BAD}.get(risk, INK)
    vw = history[history["ticker"] == "VWRL.L"] if history is not None and not history.empty else pd.DataFrame()
    macro_html = (f'<div style="font-size:13px; font-weight:700; color:{risk_col};">Macro risk: {esc(risk.title())}</div>'
                  f'<div style="font-size:13px; color:{SOFT}; line-height:1.55; margin:4px 0 12px;">'
                  f'{esc((macro or {}).get("macro_summary") or "")}</div>'
                  f'<div style="font-size:11px; color:{SOFT}; margin-bottom:4px;">World shares (VWRL), 3 years</div>'
                  f'{img(macro_chart(vw) if not vw.empty else "", "World index")}')
    mom_html = "".join(momentum_table(s, momentum[s]) for s in SEGMENTS_IN_ORDER if s in momentum)

    counts = {s: len([r for r in rows if r["market_segment"] == s]) for s in EMAIL_INDEXES}
    meta = (f'Prices to close {short_date(price_date) if price_date else "n/a"} &middot; triggers: '
            + ", ".join(f"{s} {n}" for s, n in counts.items() if n))

    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Morning briefing</title></head>
<body style="margin:0; padding:0; background:{PAPER}; font-family:{SANS}; color:{INK};">
<div style="max-width:600px; margin:0 auto; padding:0 0 20px; background:{PAPER};">
 {header}
 {stats_card}
 {card("Daily review", review_html, sub="Checks on the system, the data and Saxo, run at 07:00 before this email.")}
 {card("Early warning", ew_html, sub="Saturdays: 12 market-stress indicators, now and against last week. For judgement only.") if ew_html else ""}
 {timeline_card}
 {mix_card}
 {opp_card}
 {comp_card}
 {card("Actions on your holdings", dg.get("actions", ""), sub="Stops, time exits and reminders from the position monitor.")}
 {card("Portfolio", dg.get("portfolio", ""))}
 {card(calendar_title(today), calendar_block(cal_rows or [], today, 10 if light >= 2 else CALENDAR_MAX_ROWS), sub=CALENDAR_NOTE)}
 {card("Reviewed today", rev_html, sub="Stocks Gemini read in full. For information only.")}
 {card("Other signals", others_table(others) if others else "", sub="Logged for learning, not reviewed by Gemini. No action.")}
 {card("Market context", macro_html + f'<div style="margin-top:16px;">{mom_html}</div>')}
 {card("Closed trades", dg.get("closed", ""))}
 {card("Routine announcements", dg.get("news", ""))}
 {card("System", dg.get("system", ""))}
 <div style="padding:8px 20px 0; font-size:11px; color:{FAINT}; line-height:1.6;">{meta}. Orders use one Saxo ticket with only the
 stop attached: a 6-10% one-day drop in a FTSE 100, FTSE 250 or S&amp;P 500 stock whose past-year rise is in the top fifth of its index,
 unless Gemini vetoes it, in a regime that allows trading. &pound;{STAKE_GBP:.0f} per full stake, limit at today's close +
 {ENTRY_LIMIT_BUFFER * 100:.0f}%, stop {DISASTER_VOL_MULTIPLE}x daily volatility below, no take-profit, 6-month time exit.
 Generated automatically; not investment advice. &middot; Morning briefing {MAILER_VERSION}</div>
</div></body></html>"""
    return html, n_orders


# ------------------------------------------------------------------------ entry point

@functions_framework.http
def send_buy_list_email(request):
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    bq = bigquery.Client(project=PROJECT_ID, location="europe-west2")
    today = date.today()
    try:   # v16: the regime maths runs once, into market_regime_daily; everything below reads that table
        bq.query(f"CALL `{DATASET}.sp_refresh_market_regime`()").result()
    except Exception as e:  # noqa: BLE001
        print(f"Regime refresh failed (using yesterday's stored regime): {e}")
    # v19: the daily review (checks in BigQuery; Gemini fragility read on Mondays). Fails open.
    import daily_review as dr
    review_err = dr.run_checks(bq, DATASET)
    dr.fragility_read(bq, DATASET, today, model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
                      project=PROJECT_ID, location=os.environ.get("GEMINI_LOCATION", "global"))
    review_html = dr.card_inner(dr.load(bq, DATASET, today), today, review_err)
    # v21: early warning panel. Feeds and the BigQuery snapshot refresh every run (for Power BI);
    # the card only goes in the Saturday email. Fails open.
    import early_warning as ew
    ew_failed = ew.refresh_feeds(bq, DATASET)
    ew_err = ew.refresh_panel(bq, DATASET)
    ew_html = ""
    if ew.show_card_today(today):
        ew_rows, _ = ew.load_week(bq, DATASET)
        ew_html = ew.card_inner(ew_rows, ew_failed, ew_err)
    try:
        rows = load_today(bq)
        macro = next(iter([dict(r) for r in bq.query(
            f"SELECT macro_risk_level, macro_summary FROM `{DATASET}.fact_macro_context` "
            f"ORDER BY ranking_date DESC LIMIT 1").result()]), {})
    except Exception as e:  # noqa: BLE001
        return f"BigQuery error: {e}", 500

    run_date = rows[0]["ranking_date"] if rows else None
    price_date = max((r["price_date"] for r in rows if r.get("price_date")), default=None)
    chart_tickers = sorted({r["ticker"] for r in rows if r.get("trade_eligible") or r.get("review_status") == "REVIEWED"})
    tick_sql = ",".join(f"'{t}'" for t in chart_tickers + ["VWRL.L"])
    history = bq.query(f"""SELECT ticker, price_date, CAST(close_price AS FLOAT64) close_price
        FROM `{DATASET}.fact_daily_prices` WHERE ticker IN ({tick_sql})
          AND price_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 3 YEAR) ORDER BY ticker, price_date""").to_dataframe()
    momentum = fetch_sector_momentum(bq)
    try:
        regime = next(iter([dict(r) for r in bq.query(
            f"SELECT * FROM `{DATASET}.market_regime_daily` WHERE is_latest").result()]), {})
    except Exception as e:  # noqa: BLE001
        print(f"Regime read failed: {e}")
        regime = {}
    try:
        usd_to_gbp = next(iter([r["to_gbp"] for r in bq.query(
            f"SELECT to_gbp FROM `{DATASET}.vw_saxo_fx_latest` WHERE currency = 'USD'").result()]), None)
    except Exception as e:  # noqa: BLE001
        print(f"USD rate read failed (US tickets will be skipped): {e}")
        usd_to_gbp = None
    try:
        cal_rows = load_calendar(bq, today)
    except Exception as e:  # noqa: BLE001
        print(f"Calendar query failed (tables will show empty): {e}")
        cal_rows = []

    try:
        timeline = bq.query(f"""SELECT regime_date, uk_regime, uk_regime_raw, ftse100_close, ftse100_ma200,
                   us_regime, us_regime_raw, sp500_close
            FROM `{DATASET}.market_regime_daily` WHERE regime_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 2 YEAR)
            ORDER BY regime_date""").to_dataframe()
    except Exception as e:  # noqa: BLE001
        print(f"Regime timeline failed: {e}")
        timeline = None
    try:
        mix = [dict(r) for r in bq.query(f"SELECT * FROM `{DATASET}.vw_portfolio_mix` ORDER BY sleeve_sort").result()]
    except Exception as e:  # noqa: BLE001
        print(f"Mix read failed: {e}")
        mix = []
    try:
        import mix_moves as mm
        moves = [dict(r) for r in bq.query(f"SELECT * FROM `{DATASET}.vw_mix_moves` ORDER BY sleeve_sort").result()]
        moves_html = mm.mix_moves_html(moves, today)
    except Exception as e:  # noqa: BLE001
        print(f"Month-end moves failed: {e}")
        moves_html = ""
    try:
        picks = [dict(r) for r in bq.query(f"SELECT * FROM `{DATASET}.vw_compounders_picks` ORDER BY pick_no").result()]
    except Exception as e:  # noqa: BLE001
        print(f"Compounders picks failed: {e}")
        picks = []
    digest, news_keys = {}, []
    try:
        import digest_job as dj
        news_html, news_keys = dj.routine_news()
        digest = {"actions": dj.actions(), "portfolio": dj.portfolio(), "closed": dj.closed_trades(),
                  "news": news_html, "system": dj.sync_status()}
    except Exception as e:  # noqa: BLE001
        print(f"Portfolio sections failed (email goes without them): {e}")

    for light in (0, 1, 2):     # keep under Gmail's ~100KB clipping limit
        html, n_orders = build_email(rows, macro, history, momentum, today, run_date, price_date, cal_rows, light,
                                     regime, usd_to_gbp, timeline, mix, digest, moves_html, picks, review_html, ew_html)
        if len(html.encode()) / 1024 < 95:
            break
        print(f"Email over 95KB at detail level {light}; building a lighter version.")
    size_kb = len(html.encode()) / 1024
    print(f"Email: {n_orders} order ticket(s), {len(rows)} triggered rows, {size_kb:.1f}KB HTML + "
          f"{len(INLINE_IMAGES)} images {sum(len(b) for _, b in INLINE_IMAGES) / 1024:.0f}KB "
          f"({'OK' if size_kb < 100 else 'OVER GMAIL CLIP THRESHOLD'}).")

    cfg = mail_config()
    reg_word = (regime or {}).get("uk_regime") or "regime unknown"
    subject = (f"Morning briefing: {n_orders} order{'s' if n_orders != 1 else ''} to place · {reg_word}"
               if n_orders else f"Morning briefing: no new orders · {reg_word}")
    from email.mime.image import MIMEImage
    msg = MIMEMultipart("related")
    msg["Subject"], msg["From"], msg["To"] = subject, cfg["smtp_user"], cfg["to"]
    rel = msg
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html, "html"))
    rel.attach(alt)
    for cid, png in INLINE_IMAGES:
        part = MIMEImage(png, "png")
        part.add_header("Content-ID", f"<{cid}>")
        part.add_header("Content-Disposition", "inline", filename=f"{cid}.png")
        rel.attach(part)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(cfg["smtp_user"], cfg["app_password"])
            s.sendmail(cfg["smtp_user"], cfg["to"], msg.as_string())
        dr.record_send(bq, DATASET, today, subject, MAILER_VERSION)   # v19: lets tomorrow's check A4 confirm the send
        if news_keys:      # same bookkeeping the old digest did
            try:
                bq.query(f"""UPDATE `{DATASET}.fact_alert_log` SET digest_included_at = CURRENT_TIMESTAMP()
                             WHERE alert_key IN UNNEST(@keys)""",
                         job_config=bigquery.QueryJobConfig(query_parameters=[
                             bigquery.ArrayQueryParameter("keys", "STRING", news_keys)])).result()
            except Exception as e:  # noqa: BLE001
                print(f"Could not mark routine news as included: {e}")
        return f"Email sent: {subject}", 200
    except Exception as e:  # noqa: BLE001
        return f"SMTP error: {e}", 500
