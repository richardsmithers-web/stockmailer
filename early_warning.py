"""Early warning panel (v21): fetches the outside data each morning, refreshes the panel in BigQuery, and draws
the Saturday email card.

How it fits together
- The 12 indicators, their thresholds and their plain-English rules live in BigQuery table ew_indicators.
  Edit that table to change a threshold; no code change needed. (Source SQL: sql/early_warning_01_indicators.sql)
- The maths lives in the BigQuery view vw_early_warning (sql/early_warning_02_view.sql). It reads:
    hist_macro_20y      FRED and ONS series fetched here, plus the Bank of England series already loaded nightly
    hist_index_20y      FTSE 100 and S&P 500 prices and volumes (already loaded nightly)
    hist_prices_20y_us  RSP and SPY fund prices, fetched here, for the equal-weight vs market-cap comparison
    fact_daily_prices   our own stock prices (UK breadth)
- Every morning (Tue-Sat) the stockmailer calls refresh_feeds() then refresh_panel(). refresh_panel() runs
  sp_refresh_early_warning(), which snapshots the view into ew_daily. Power BI reads ew_daily through the
  rpt_*early_warning* views, so the dashboard is up to date every morning.
- Only the Saturday email shows the card (show_card_today). It compares the latest reading with a week earlier.
- Everything here fails open: if a website doesn't answer, the rest still runs, the panel uses the last
  figures it has, and the card says which feed failed.

Where the outside data comes from (all free, no keys)
- FRED (Federal Reserve Bank of St. Louis), fredgraph.csv download: T10Y3M, BAMLH0A0HYM2, SAHMREALTIME, ICSA,
  VIXCLS, NCBEILQ027S, GDP
- ONS: UK unemployment rate, series MGSX
- Yahoo Finance chart data: RSP and SPY daily prices
"""
import csv
import html
import io
import json
import urllib.request
from datetime import date, datetime, timedelta, timezone

from google.cloud import bigquery

# Same palette as main.py (Pure mono; colour only for the lights)
INK, SOFT, FAINT, LINE = "#0A0A0A", "#6B6B6B", "#8F8F8F", "#EBEBEB"
GOOD, BAD, AMBER = "#0A7A3E", "#C2261C", "#A35C00"

FRED_SERIES = ["T10Y3M", "BAMLH0A0HYM2", "SAHMREALTIME", "ICSA", "VIXCLS", "NCBEILQ027S", "GDP"]
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd=2004-01-01"
ONS_URL = ("https://www.ons.gov.uk/employmentandlabourmarket/peoplenotinwork/unemployment/"
           "timeseries/mgsx/lms/data")
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1y&interval=1d"
FUNDS = {"RSP": "BENCHMARK S&P 500 EQUAL WEIGHT", "SPY": "BENCHMARK S&P 500"}
UA = {"User-Agent": "Mozilla/5.0 (stockmailer early-warning feed; personal use)"}
TIMEOUT_S = 25

SHOW_ON_WEEKDAY = 5          # Saturday (Monday = 0)
LIGHT_RANK = {"RED": 0, "AMBER": 1, "GREEN": 2, "NO DATA": 3}


# ------------------------------------------------------------------------ fetching

def _get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return r.read().decode("utf-8")


def _fred(sid):
    """Rows (series, obs_date, value) from a FRED CSV. FRED marks missing days with '.'."""
    text = _get(FRED_URL.format(sid=sid))
    reader = csv.reader(io.StringIO(text))
    next(reader)                                  # header: observation_date,<SID>
    rows = []
    for rec in reader:
        if len(rec) < 2 or rec[1] in ("", "."):
            continue
        rows.append({"series": sid, "obs_date": rec[0][:10], "value": float(rec[1]), "source": "fred"})
    return rows


def _ons_unemployment():
    """UK unemployment rate (MGSX), monthly. ONS dates like '2026 JUL' are the last month of a 3-month period."""
    data = json.loads(_get(ONS_URL))
    rows = []
    for m in data.get("months", []):
        try:
            d = datetime.strptime(m["date"].title(), "%Y %b").date()
            rows.append({"series": "UK_UNEMPLOYMENT_RATE", "obs_date": d.isoformat(),
                         "value": float(m["value"]), "source": "ons"})
        except (KeyError, ValueError):
            continue
    return rows


def _yahoo_fund(sym):
    """Daily closes for a US fund. The last bar is dropped if it is today (US time), as it may be unfinished."""
    res = json.loads(_get(YAHOO_URL.format(sym=sym)))["chart"]["result"][0]
    offset = res["meta"].get("gmtoffset", 0)
    q = res["indicators"]["quote"][0]
    adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or q["close"]
    today_us = (datetime.now(timezone.utc) + timedelta(seconds=offset)).date()
    rows = []
    for ts, c, a, v in zip(res["timestamp"], q["close"], adj, q["volume"]):
        d = datetime.fromtimestamp(ts + offset, tz=timezone.utc).date()
        if c is None or d >= today_us:
            continue
        rows.append({"ticker": sym, "symbol": sym, "index_name": FUNDS[sym], "price_date": d.isoformat(),
                     "close": float(c), "adj_close": float(a) if a is not None else float(c),
                     "volume": float(v or 0), "source": "yahoo"})
    return rows


def _load_and_merge(bq, dataset, rows, stg, schema, merge_sql):
    job = bq.load_table_from_json(rows, f"{dataset}.{stg}", job_config=bigquery.LoadJobConfig(
        schema=schema, write_disposition="WRITE_TRUNCATE"))
    job.result(timeout=120)
    bq.query(merge_sql).result(timeout=120)


def refresh_feeds(bq, dataset):
    """Fetch every outside series and merge it into BigQuery. Returns a list of feeds that failed (empty = all OK)."""
    failed, macro = [], []
    for sid in FRED_SERIES:
        try:
            macro += _fred(sid)
        except Exception as e:  # noqa: BLE001
            print(f"Early warning: FRED {sid} failed: {e}")
            failed.append(f"FRED {sid}")
    try:
        macro += _ons_unemployment()
    except Exception as e:  # noqa: BLE001
        print(f"Early warning: ONS unemployment failed: {e}")
        failed.append("ONS unemployment")
    if macro:
        try:
            _load_and_merge(
                bq, dataset, macro, "ew_stg_macro",
                [bigquery.SchemaField("series", "STRING"), bigquery.SchemaField("obs_date", "DATE"),
                 bigquery.SchemaField("value", "FLOAT64"), bigquery.SchemaField("source", "STRING")],
                f"""MERGE `{dataset}.hist_macro_20y` t USING `{dataset}.ew_stg_macro` s
                    ON t.series = s.series AND t.obs_date = s.obs_date
                    WHEN MATCHED AND t.value != s.value THEN
                      UPDATE SET value = s.value, source = s.source, loaded_at = CURRENT_TIMESTAMP()
                    WHEN NOT MATCHED THEN
                      INSERT (series, obs_date, value, source, loaded_at)
                      VALUES (s.series, s.obs_date, s.value, s.source, CURRENT_TIMESTAMP())""")
            print(f"Early warning: merged {len(macro)} FRED/ONS rows.")
        except Exception as e:  # noqa: BLE001
            print(f"Early warning: saving FRED/ONS rows failed: {e}")
            failed.append("saving FRED/ONS data")
    funds = []
    for sym in FUNDS:
        try:
            funds += _yahoo_fund(sym)
        except Exception as e:  # noqa: BLE001
            print(f"Early warning: Yahoo {sym} failed: {e}")
            failed.append(f"{sym} fund prices")
    if funds:
        try:
            _load_and_merge(
                bq, dataset, funds, "ew_stg_funds",
                [bigquery.SchemaField("ticker", "STRING"), bigquery.SchemaField("symbol", "STRING"),
                 bigquery.SchemaField("index_name", "STRING"), bigquery.SchemaField("price_date", "DATE"),
                 bigquery.SchemaField("close", "FLOAT64"), bigquery.SchemaField("adj_close", "FLOAT64"),
                 bigquery.SchemaField("volume", "FLOAT64"), bigquery.SchemaField("source", "STRING")],
                f"""MERGE `{dataset}.hist_prices_20y_us` t USING `{dataset}.ew_stg_funds` s
                    ON t.ticker = s.ticker AND t.price_date = s.price_date
                    WHEN MATCHED AND t.close != s.close THEN
                      UPDATE SET close = s.close, adj_close = s.adj_close, volume = s.volume, loaded_at = CURRENT_TIMESTAMP()
                    WHEN NOT MATCHED THEN
                      INSERT (ticker, symbol, index_name, price_date, close, adj_close, volume, source, loaded_at)
                      VALUES (s.ticker, s.symbol, s.index_name, s.price_date, s.close, s.adj_close, s.volume,
                              s.source, CURRENT_TIMESTAMP())""")
            print(f"Early warning: merged {len(funds)} fund price rows.")
        except Exception as e:  # noqa: BLE001
            print(f"Early warning: saving fund prices failed: {e}")
            failed.append("saving fund prices")
    return failed


def refresh_panel(bq, dataset, timeout_s=240):
    """Rebuild ew_daily from the view. Returns None on success or a short error string."""
    try:
        job = bq.query(f"CALL `{dataset}.sp_refresh_early_warning`()",
                       job_config=bigquery.QueryJobConfig(job_timeout_ms=timeout_s * 1000))
        job.result(timeout=timeout_s + 10)
        return None
    except Exception as e:  # noqa: BLE001
        print(f"Early warning panel refresh failed (using yesterday's snapshot): {e}")
        return str(e)[:200]


def show_card_today(today):
    return today.weekday() == SHOW_ON_WEEKDAY


# ------------------------------------------------------------------------ the card

def load_week(bq, dataset):
    """Latest reading per indicator, plus the reading about a week earlier. Returns (rows, latest_date) or (None, None)."""
    try:
        rows = [dict(r) for r in bq.query(f"""
            WITH latest AS (SELECT MAX(warning_date) AS d FROM `{dataset}.ew_daily`),
                 prev AS (SELECT MAX(warning_date) AS d FROM `{dataset}.ew_daily`, latest
                          WHERE warning_date <= DATE_SUB(latest.d, INTERVAL 7 DAY))
            SELECT n.indicator_id, n.sort, n.area, n.name, n.unit, n.value, n.light, n.detail, n.data_as_of,
                   n.pctile_20y, n.warning_date, p.value AS prev_value, p.light AS prev_light,
                   sn.risk_score, sp.risk_score AS prev_risk_score
            FROM `{dataset}.ew_daily` n
            JOIN latest ON n.warning_date = latest.d
            CROSS JOIN prev
            LEFT JOIN `{dataset}.ew_daily` p ON p.indicator_id = n.indicator_id AND p.warning_date = prev.d
            LEFT JOIN `{dataset}.rpt_fact_early_warning_daily` sn ON sn.warning_date = latest.d
            LEFT JOIN `{dataset}.rpt_fact_early_warning_daily` sp ON sp.warning_date = prev.d
            ORDER BY n.sort""").result()]
    except Exception as e:  # noqa: BLE001
        print(f"Early warning read failed: {e}")
        return None, None
    return rows, (rows[0]["warning_date"] if rows else None)


def _esc(x):
    return html.escape("" if x is None else str(x))


def _dot(light):
    col = {"RED": BAD, "AMBER": AMBER, "GREEN": GOOD}.get(light, FAINT)
    return (f'<span style="display:inline-block; width:8px; height:8px; border-radius:4px; background:{col}; '
            f'margin-right:8px; vertical-align:middle;"></span>')


def _fmt(v, unit):
    if v is None:
        return "&ndash;"
    v = float(v)
    if unit == "days":
        return f"{v:.0f}"
    if unit == "%":
        return f"{v:.1f}%" if abs(v) < 100 else f"{v:.0f}%"
    if unit == "pts":
        return f"{v:+.2f}"
    return f"{v:.1f}"


def _chg(now, prev, unit):
    if now is None or prev is None:
        return ""
    d = float(now) - float(prev)
    if abs(d) < 0.005:
        return "no change"
    if unit == "days":
        return f"{d:+.0f}"
    return f"{d:+.2f}" if unit == "pts" else f"{d:+.1f}"


def card_inner(rows, feed_failures=None, refresh_error=None):
    """The card body (main.py wraps it with card()). rows=None means the panel couldn't be read."""
    if not rows:
        why = f" ({_esc(refresh_error)})" if refresh_error else ""
        return (f'<div style="font-size:13px; color:{BAD};">{_dot("RED")}The early warning panel couldn\'t be read'
                f'{why}. The rest of this email is unaffected; tell Claude.</div>')
    n_red = sum(r["light"] == "RED" for r in rows)
    n_amb = sum(r["light"] == "AMBER" for r in rows)
    n_nod = sum(r["light"] == "NO DATA" for r in rows)
    n_live = len(rows) - n_nod
    if n_red or n_amb:
        parts = ([f'<b style="color:{BAD};">{n_red} red</b>'] if n_red else []) + \
                ([f'<b style="color:{AMBER};">{n_amb} amber</b>'] if n_amb else [])
        headline = " &middot; ".join(parts) + f' <span style="color:{SOFT};">of {n_live} indicators</span>'
    else:
        headline = f'<b style="color:{GOOD};">All {n_live} indicators green</b>'
    head = f'<div style="font-size:14px; color:{INK}; margin-bottom:4px;">{headline}</div>'

    # the 1-10 risk score (same rule as Power BI page D3: amber 1 point, red 3, weighted; 10 = half the possible points)
    score, prev_score = rows[0].get("risk_score"), rows[0].get("prev_risk_score")
    if score is not None:
        score = float(score)
        band = "High" if score >= 7 else "Elevated" if score >= 5 else "Watch" if score >= 3 else "Calm"
        scol = BAD if score >= 7 else AMBER if score >= 3 else GOOD
        if prev_score is None:
            move = ""
        else:
            diff = score - float(prev_score)
            move = (" &middot; unchanged on last week" if abs(diff) < 0.05 else
                    f' &middot; {"up" if diff > 0 else "down"} {abs(diff):.1f} on last week')
        head += (f'<div style="font-size:13px; color:{INK}; margin-bottom:4px;">Risk score '
                 f'<b style="color:{scol};">{score:.1f} / 10 ({band})</b><span style="color:{SOFT};">{move}</span></div>')

    # what changed colour since last week
    moved = [r for r in rows if r.get("prev_light") and r["prev_light"] != r["light"]]
    if moved:
        mv = "; ".join(f'{_esc(r["name"])} {_esc(r["prev_light"].lower())} &rarr; '
                       f'<b>{_esc(r["light"].lower())}</b>' for r in moved)
        head += f'<div style="font-size:12.5px; color:{INK}; margin-bottom:10px;">Changed this week: {mv}.</div>'
    else:
        head += f'<div style="font-size:12.5px; color:{SOFT}; margin-bottom:10px;">No light changed colour this week.</div>'

    # one line per indicator, grouped by area in rulebook order
    td = f'padding:4px 0; font-size:12.5px; border-bottom:1px solid {LINE};'
    lines = ""
    for r in rows:
        col = INK if r["light"] in ("RED", "AMBER") else SOFT
        lines += (f'<tr><td style="{td} color:{INK};">{_dot(r["light"])}{_esc(r["name"])}</td>'
                  f'<td style="{td} color:{col}; text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums;">'
                  f'{_fmt(r["value"], r["unit"])}</td>'
                  f'<td style="{td} color:{FAINT}; text-align:right; white-space:nowrap; width:72px; '
                  f'font-variant-numeric:tabular-nums;">{_chg(r["value"], r.get("prev_value"), r["unit"])}</td></tr>')
    table = (f'<table role="presentation" width="100%" style="border-collapse:collapse;">'
             f'<tr><td style="font-size:11px; color:{FAINT}; padding-bottom:2px;">Indicator</td>'
             f'<td style="font-size:11px; color:{FAINT}; text-align:right;">Now</td>'
             f'<td style="font-size:11px; color:{FAINT}; text-align:right;">vs last wk</td></tr>{lines}</table>')

    # a sentence for every amber or red light, worst first
    det = ""
    for r in sorted((r for r in rows if r["light"] in ("RED", "AMBER")), key=lambda r: (LIGHT_RANK[r["light"]], r["sort"])):
        asof = r.get("data_as_of")
        stale = ""
        if asof and r.get("warning_date") and (r["warning_date"] - asof).days > 10:
            stale = f' <span style="color:{FAINT};">(latest figure {asof.day} {asof:%b %Y})</span>'
        det += (f'<div style="padding:8px 0; border-top:1px solid {LINE}; font-size:12px; line-height:1.5;">'
                f'<b style="color:{INK};">{_dot(r["light"])}{_esc(r["name"])}</b>'
                f'<span style="color:{SOFT};"> &middot; {_esc(r["detail"])}{stale}</span></div>')

    notes = []
    if n_nod:
        notes.append(f"{n_nod} indicator{'s' if n_nod != 1 else ''} had no data: "
                     + ", ".join(_esc(r["name"]) for r in rows if r["light"] == "NO DATA"))
    if feed_failures:
        notes.append("These feeds didn't answer this morning, so last week's figures were used: "
                     + ", ".join(_esc(f) for f in feed_failures))
    if refresh_error:
        notes.append("The panel wasn't rebuilt this morning, so these are yesterday's readings")
    note_html = "".join(f'<div style="font-size:12px; color:{AMBER}; margin-top:6px;">{n}.</div>' for n in notes)

    foot = (f'<div style="font-size:11px; color:{FAINT}; margin-top:10px; line-height:1.5;">For judgement only: nothing '
            f'here changes the regime, the mix or any order. Risk score: amber 1 point, red 3, weighted 1.5 for the '
            f'leading signals; 10 means half the possible points. Rules, thresholds and weights live in BigQuery table '
            f'ew_indicators; the daily history is on page D3 Early warning in Power BI.</div>')
    return head + table + (f'<div style="margin-top:10px;">{det}</div>' if det else "") + note_html + foot
