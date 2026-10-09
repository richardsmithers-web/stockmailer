"""Daily review (v19): runs the checks in BigQuery, adds the Monday fragility read, and draws the email card.

How it fits together
- The checks live in BigQuery table ops_dq_checks (one row per check, with its SQL). Edit that table to change a
  threshold or switch a check on/off; no code change needed.
- sp_run_daily_review() runs the enabled checks and writes one row per check to ops_daily_review for today.
  Daily checks run every day; weekly ones on Mondays.
- On Mondays this module also asks Gemini (with Google Search) for a short, advisory market-fragility read and
  stores it in ops_daily_review as check I1 (source GEMINI). It never changes the regime, the mix or any trade.
- Everything here fails open: if any step fails, the email still goes, and the card says what didn't run.
"""
import re
from datetime import date

from google.cloud import bigquery

# Same palette as main.py (Pure mono; colour only for problems and passes)
INK, SOFT, FAINT, LINE = "#0A0A0A", "#6B6B6B", "#8F8F8F", "#EBEBEB"
GOOD, BAD, AMBER = "#0A7A3E", "#C2261C", "#A35C00"

DAILY_AREAS = ["Runs", "Data", "Broker"]
WEEKLY_AREAS = ["Rules", "Performance", "Model", "Risk", "Fragility", "Housekeeping"]
STATUS_RANK = {"FAIL": 0, "ERROR": 1, "WARN": 2, "INFO": 3, "PASS": 4}

FRAGILITY_PROMPT = """You are a cautious market-risk analyst writing for a UK private investor who holds world and UK/US shares.
Using Google Search for news from the last 7 days, assess signs of market fragility that price data alone would not show:
1. credit stress (US high-yield and UK corporate bond spreads widening),
2. funding and liquidity stress (bank funding markets, dollar funding, money-market strains),
3. market breadth (how many stocks are carrying the main indexes),
4. volatility pricing (VIX term structure inverting, unusual demand for crash protection),
5. scheduled shocks in the next two weeks (Bank of England, Federal Reserve, UK fiscal events, elections, tariffs, large debt auctions).
Be factual and specific; name the figure and its date where you can; do not give trading advice.
Reply in EXACTLY this format and nothing else:
RATING: LOW or MEDIUM or HIGH
SUMMARY: three to five plain-English sentences covering the points above, most important first.
"""


def run_checks(bq, dataset, timeout_s=150):
    """Run today's checks. Returns None on success or a short error string (the email still goes)."""
    try:
        job = bq.query(f"CALL `{dataset}.sp_run_daily_review`(FALSE)",
                       job_config=bigquery.QueryJobConfig(job_timeout_ms=timeout_s * 1000))
        job.result(timeout=timeout_s + 10)
        return None
    except Exception as e:  # noqa: BLE001
        print(f"Daily review checks failed or timed out: {e}")
        return str(e)[:200]


def fragility_read(bq, dataset, today, model="gemini-2.5-flash", project=None, location="global"):
    """Mondays only: one grounded Gemini call, stored as check I1. Skips if already written today."""
    if today.weekday() != 0:
        return
    try:
        done = list(bq.query(f"""SELECT 1 FROM `{dataset}.ops_daily_review`
                                 WHERE review_date = @d AND check_id = 'I1' LIMIT 1""",
                             job_config=bigquery.QueryJobConfig(query_parameters=[
                                 bigquery.ScalarQueryParameter("d", "DATE", today)])).result())
        if done:
            return
        from google import genai
        from google.genai import types
        client = genai.Client(vertexai=True, project=project, location=location)
        resp = client.models.generate_content(
            model=model, contents=FRAGILITY_PROMPT,
            config=types.GenerateContentConfig(temperature=0.2,
                                               tools=[types.Tool(google_search=types.GoogleSearch())]))
        text = (resp.text or "").strip()
        m = re.search(r"RATING:\s*(LOW|MEDIUM|HIGH)", text, re.I)
        s = re.search(r"SUMMARY:\s*(.+)", text, re.I | re.S)
        rating = m.group(1).upper() if m else "UNRATED"
        summary = (s.group(1).strip() if s else text)[:1500]
        status = {"LOW": "PASS", "MEDIUM": "INFO", "HIGH": "WARN"}.get(rating, "INFO")
        bq.query(f"""INSERT INTO `{dataset}.ops_daily_review`
                       (review_date, check_id, area, cadence, title, status, metric, detail, action, source, created_at)
                     VALUES (@d, 'I1', 'Fragility', 'WEEKLY', 'Market fragility outside the data (advisory)', @st, NULL,
                             @detail, IF(@st = 'WARN', 'Advisory only. Re-read the crash ladder in the rulebook.', NULL),
                             'GEMINI', CURRENT_TIMESTAMP())""",
                 job_config=bigquery.QueryJobConfig(query_parameters=[
                     bigquery.ScalarQueryParameter("d", "DATE", today),
                     bigquery.ScalarQueryParameter("st", "STRING", status),
                     bigquery.ScalarQueryParameter("detail", "STRING", f"{rating}: {summary}")])).result()
    except Exception as e:  # noqa: BLE001
        print(f"Fragility read failed (card shows it as not run): {e}")


def load(bq, dataset, today):
    """Today's review rows, worst first within each area."""
    try:
        rows = [dict(r) for r in bq.query(
            f"""SELECT check_id, area, cadence, title, status, detail, action, source
                FROM `{dataset}.ops_daily_review` WHERE review_date = @d""",
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("d", "DATE", today)])).result()]
    except Exception as e:  # noqa: BLE001
        print(f"Daily review read failed: {e}")
        return None
    rows.sort(key=lambda r: (STATUS_RANK.get(r["status"], 9), r["check_id"]))
    return rows


def _esc(x):
    import html
    return html.escape("" if x is None else str(x))


def _dot(status):
    col = {"FAIL": BAD, "ERROR": BAD, "WARN": AMBER, "PASS": GOOD}.get(status, FAINT)
    return (f'<span style="display:inline-block; width:8px; height:8px; border-radius:4px; background:{col}; '
            f'margin-right:8px; vertical-align:middle;"></span>')


def _area_status(rows):
    return min((r["status"] for r in rows), key=lambda s: STATUS_RANK.get(s, 9)) if rows else None


WORD = {"FAIL": "Needs action", "ERROR": "Check failed to run", "WARN": "Check", "INFO": "For information", "PASS": "All clear"}


def card_inner(rows, today, run_error=None):
    """The card body (main.py wraps it with card()). rows=None means the review couldn't be read."""
    if not rows:
        why = f" ({_esc(run_error)})" if run_error else ""
        return (f'<div style="font-size:13px; color:{BAD};">{_dot("FAIL")}The review didn\'t run this morning{why}. '
                f'The rest of this email is unaffected; tell Claude.</div>')
    monday = today.weekday() == 0
    areas = DAILY_AREAS + (WEEKLY_AREAS if monday else [])
    by_area = {a: [r for r in rows if r["area"] == a] for a in areas}
    n_fail = sum(r["status"] in ("FAIL", "ERROR") for r in rows)
    n_warn = sum(r["status"] == "WARN" for r in rows)
    n_daily = sum(r["cadence"] == "DAILY" for r in rows)
    if n_fail or n_warn:
        parts = ([f'<b style="color:{BAD};">{n_fail} need{"s" if n_fail == 1 else ""} action</b>'] if n_fail else []) + \
                ([f'<b style="color:{AMBER};">{n_warn} to check</b>'] if n_warn else [])
        headline = " &middot; ".join(parts) + f' <span style="color:{SOFT};">of {len(rows)} checks</span>'
    else:
        headline = f'<b style="color:{GOOD};">All {len(rows)} checks clear</b>'
    head = f'<div style="font-size:14px; color:{INK}; margin-bottom:10px;">{headline}</div>'

    # one line per area: dot, area, short state
    lines = ""
    for a in areas:
        rs = by_area[a]
        st = _area_status(rs)
        if st is None:
            label = "Not run" if a != "Fragility" else "Not written"
            lines += (f'<tr><td style="padding:3px 0; font-size:12.5px; color:{SOFT}; width:120px;">{_dot("INFO")}{a}</td>'
                      f'<td style="padding:3px 0; font-size:12.5px; color:{SOFT};">{label}</td></tr>')
            continue
        act = [r["check_id"] for r in rs if r["status"] in ("FAIL", "ERROR")]
        chk = [r["check_id"] for r in rs if r["status"] == "WARN"]
        bad = act + chk
        if bad:
            txt = " &middot; ".join(([f"Needs action: {', '.join(map(_esc, act))}"] if act else []) +
                                    ([f"Check: {', '.join(map(_esc, chk))}"] if chk else []))
        else:
            txt = f"{WORD.get(st, st)} ({len(rs)})"
        lines += (f'<tr><td style="padding:3px 0; font-size:12.5px; color:{INK}; width:120px;">{_dot(st)}{a}</td>'
                  f'<td style="padding:3px 0; font-size:12.5px; color:{INK if bad else SOFT};">{txt}</td></tr>')
    table = f'<table role="presentation" width="100%" style="border-collapse:collapse;">{lines}</table>'

    # details for anything not clear
    det = ""
    for r in rows:
        if r["status"] not in ("FAIL", "ERROR", "WARN"):
            continue
        act = (f'<div style="font-size:12px; color:{INK}; margin-top:3px;">&rarr; {_esc(r["action"])}</div>'
               if r.get("action") else "")
        det += (f'<div style="padding:10px 0; border-top:1px solid {LINE};">'
                f'<div style="font-size:12.5px; font-weight:600; color:{INK};">{_dot(r["status"])}{_esc(r["check_id"])} '
                f'&middot; {_esc(r["title"])}</div>'
                f'<div style="font-size:12px; color:{SOFT}; margin-top:3px; line-height:1.5;">{_esc(r["detail"])}</div>{act}</div>')

    # Mondays: the information-only lines (performance, model, fragility)
    info = ""
    if monday:
        for r in rows:
            if r["status"] in ("INFO", "PASS") and r["cadence"] == "WEEKLY" and r["area"] in ("Performance", "Model", "Fragility"):
                info += (f'<div style="padding:6px 0; border-top:1px solid {LINE}; font-size:12px; line-height:1.5;">'
                         f'<b style="color:{INK};">{_esc(r["title"])}</b>'
                         f'<span style="color:{SOFT};"> &middot; {_esc(r["detail"])}</span></div>')
        if info:
            info = (f'<div style="font-size:11px; text-transform:uppercase; letter-spacing:0.08em; color:{SOFT}; '
                    f'margin-top:14px; margin-bottom:2px;">This week</div>{info}')

    foot = (f'<div style="font-size:11px; color:{FAINT}; margin-top:10px;">{n_daily} daily checks every morning; '
            f'performance, model, risk and a market-fragility read (advisory only) on Mondays. '
            f'Checks and thresholds live in BigQuery table ops_dq_checks.</div>')
    return head + table + (f'<div style="margin-top:10px;">{det}</div>' if det else "") + info + foot


def record_send(bq, dataset, today, subject, version):
    """One row per briefing sent, so tomorrow's check A4 can confirm it went (and which version)."""
    try:
        bq.query(f"""INSERT INTO `{dataset}.fact_alert_log`
                       (alert_key, alert_type, ticker, severity, headline, ai_note, email_status, email_subject,
                        created_at, sent_at, digest_included_at)
                     VALUES (@k, 'DAILY_BRIEFING', NULL, 'INFO', @v, NULL, 'SENT', @s,
                             CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), NULL)""",
                 job_config=bigquery.QueryJobConfig(query_parameters=[
                     bigquery.ScalarQueryParameter("k", "STRING", f"briefing-{today.isoformat()}-{version}"),
                     bigquery.ScalarQueryParameter("v", "STRING", f"Morning briefing {version}"),
                     bigquery.ScalarQueryParameter("s", "STRING", subject)])).result()
    except Exception as e:  # noqa: BLE001
        print(f"Could not record the send in fact_alert_log: {e}")
