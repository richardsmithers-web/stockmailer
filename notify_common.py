"""
notify_common.py -- shared by alerts_job.py and digest_job.py.

Secret `mail-config` (Secret Manager) JSON:
  {"smtp_user": "you@gmail.com", "app_password": "<new Gmail app password>",
   "to": "you@gmail.com", "saxo_login_link": "https://saxo-auth-.../login?k=..."}

Gemini is called through Vertex AI with the job's service account (no API key).
Model and location come from env vars GEMINI_MODEL / GEMINI_LOCATION.
"""

import html
import json
import os
import smtplib
from email.message import EmailMessage

from google.cloud import bigquery, secretmanager

PROJECT_ID = "project-e042f011-a587-4cbe-8f7"
DATASET = f"{PROJECT_ID}.Market_Data_Project"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_LOCATION = os.environ.get("GEMINI_LOCATION", "global")

bq = bigquery.Client(project=PROJECT_ID, location="europe-west2")
_sm = secretmanager.SecretManagerServiceClient()
_genai = None


def mail_config():
    name = f"projects/{PROJECT_ID}/secrets/mail-config/versions/latest"
    return json.loads(_sm.access_secret_version(name=name).payload.data.decode())


def query(sql, **params):
    cfg = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter(k, "STRING", v) for k, v in params.items()])
    return [dict(r) for r in bq.query(sql, job_config=cfg).result()]


def log_alerts(rows):
    """Append decisions to fact_alert_log (load job, so rows are immediately updatable)."""
    if not rows:
        return
    tbl = bq.get_table(f"{DATASET}.fact_alert_log")
    cfg = bigquery.LoadJobConfig(
        schema=tbl.schema,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        create_disposition=bigquery.CreateDisposition.CREATE_NEVER,
    )
    bq.load_table_from_json(rows, tbl, job_config=cfg).result()


def send_email(subject, html_body, attachments=()):
    """attachments: iterable of (filename, bytes, maintype, subtype)."""
    cfg = mail_config()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["smtp_user"]
    msg["To"] = cfg["to"]
    msg.set_content("This email needs an HTML-capable mail client.")
    msg.add_alternative(html_body, subtype="html")
    for name, data, maintype, subtype in attachments:
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(cfg["smtp_user"], cfg["app_password"])
        s.send_message(msg)


def gemini_json(prompt):
    """Return parsed JSON from Gemini, or None on any failure (callers fail open)."""
    global _genai
    try:
        from google import genai
        from google.genai import types
        if _genai is None:
            _genai = genai.Client(vertexai=True, project=PROJECT_ID, location=GEMINI_LOCATION)
        resp = _genai.models.generate_content(
            model=GEMINI_MODEL, contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1))
        return json.loads(resp.text)
    except Exception as e:  # noqa: BLE001
        print(f"gemini failed: {e}")
        return None


# ---------------------------------------------------------------- formatting

def esc(x):
    return html.escape("" if x is None else str(x))


def money(x, unit="£"):
    if x is None:
        return "–"
    sign = "-" if x < 0 else ""
    return f"{sign}{unit}{abs(x):,.0f}"


def price(x, price_unit):
    if x is None:
        return "–"
    if price_unit == "GBp":
        txt = f"{x:,.2f}".rstrip("0").rstrip(".")   # 448.8p, 146.55p, 5,028p
        return f"{txt}p"
    if price_unit == "USD":
        return f"${x:,.2f}"
    return f"£{x:,.2f}"


def uk_time(ts):
    """Timestamp -> '28 Sep 21:45' in UK time."""
    if ts is None:
        return "–"
    from zoneinfo import ZoneInfo
    t = ts.astimezone(ZoneInfo("Europe/London"))
    return f"{t.day} {t:%b %H:%M}"


def short_date(d):
    if d is None:
        return "–"
    if isinstance(d, str):
        from datetime import date
        d = date.fromisoformat(d[:10])
    return f"{d.day} {d:%b}"


def colour(x):
    return "#0A7A3E" if (x or 0) >= 0 else "#C2261C"   # v18 mono: colour only for gains and losses


def page(title, inner):
    return f"""<!doctype html><html><body style="margin:0;background:#f5f5f4">
<div style="max-width:680px;margin:0 auto;padding:16px;font-family:-apple-system,Segoe UI,Arial,sans-serif;color:#1c1917;font-size:14px;line-height:1.45">
<h2 style="margin:0 0 12px;font-size:18px">{esc(title)}</h2>{inner}
<p style="color:#78716c;font-size:12px;margin-top:24px">Generated from BigQuery ({DATASET}). Prices: daily closes; Saxo prices are 15-minute delayed.</p>
</div></body></html>"""


def table(headers, rows, align=None, widths=None, notes=None):
    """rows: list of cell lists. notes: optional list (same length) of HTML shown on a
    full-width line under each row. widths: optional list of CSS widths (e.g. '18%')."""
    align = align or ["left"] * len(headers)
    widths = widths or [""] * len(headers)
    notes = notes or [None] * len(rows)
    cols = "".join(f'<col style="width:{w}">' if w else "<col>" for w in widths)
    th = "".join(f'<th style="text-align:{a};padding:0 6px 6px;border-bottom:1px solid #0A0A0A;font-size:11px;'
                 f'font-weight:600;text-transform:uppercase;letter-spacing:0.06em;color:#6B6B6B;white-space:nowrap">{esc(h)}</th>'
                 for h, a in zip(headers, align))
    trs = ""
    for r, note in zip(rows, notes):
        line = "" if note else "border-bottom:1px solid #EBEBEB;"
        tds = "".join(f'<td style="text-align:{a};padding:7px 6px 3px;font-size:13px;color:#0A0A0A;{line}'
                      f'{"white-space:nowrap;" if a == "right" else ""}vertical-align:top">{c}</td>'
                      for c, a in zip(r, align))
        trs += f"<tr>{tds}</tr>"
        if note:
            trs += (f'<tr><td colspan="{len(headers)}" style="padding:0 6px 8px;border-bottom:1px solid #EBEBEB;'
                    f'font-size:12px;color:#6B6B6B">{note}</td></tr>')
    return (f'<table role="presentation" style="border-collapse:collapse;width:100%;table-layout:fixed;">'
            f"<colgroup>{cols}</colgroup><tr>{th}</tr>{trs}</table>")
