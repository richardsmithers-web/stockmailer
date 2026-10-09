"""
digest_job.py -- portfolio sections of the 07:00 Morning briefing (bundled into stockmailer v13, 8 Oct 2026;
the separate portfolio-digest job is paused). Labels and exits updated to the new model.

Sections:
  1. Portfolio value per position, grouped SWING_90 / LONG_TERM / BONDS, plus cash, then holdings in other
     accounts entered by hand (manual_holdings, e.g. the Barclays ISA; v14), and the total of both ISAs.
  1b. A "What to do" note on every holding line (hold with stop/target/next date, or the action).
  2. Actions and reminders from vw_position_monitor (stop, day 20, time exit, earnings).
  3. Closed trades this month, newest first, with a total; then one line per earlier month and a grand total
     (v14, 8 Oct 2026). US sales are now converted to pounds (vw_closed_trades fix the same evening).
     Buy-side costs (commission, FX fee, stamp duty) are Saxo's actual figures; the sale
     commission is estimated at GBP 5 per sale.
  4. Routine announcements not emailed individually (then marked as included).
  5. Saxo sync health over the last 24 hours.
"""

from datetime import datetime, timezone

import notify_common as nc

PORTFOLIO_WIDTHS = ["22%", "12%", "17%", "17%", "16%", "16%"]
STRATEGY_LABEL = {"SWING_90": "Opportunity (6-month trades)", "LONG_TERM": "Compounders (long-term)", "BONDS": "Reserve (short bonds)",
                  "UNCLASSIFIED": "Unclassified – please tag"}


def section(title, inner):
    return f'<h3 style="margin:22px 0 8px;font-size:15px">{nc.esc(title)}</h3>{inner}'


def directions():
    """One 'what to do' line per ticker+strategy, from vw_position_monitor."""
    rows = nc.query(f"""SELECT ticker, strategy, action, price_unit, model_stop, target, time_exit_date, day20_date,
                               days_to_day20
                        FROM `{nc.DATASET}.vw_position_monitor`
                        ORDER BY ticker, action IS NULL, STARTS_WITH(IFNULL(action, ''), 'FYI'), open_date""")
    out = {}
    for r in rows:
        key = (r["ticker"], r["strategy"])
        if key in out:
            continue  # first row per holding = most urgent lot
        if r["action"] and not r["action"].startswith("FYI"):
            out[key] = f'<b style="color:#C2261C">{nc.esc(r["action"])}</b>'
        elif r["strategy"] == "SWING_90":
            txt = (f'<b>Hold</b> · stop {nc.price(r["model_stop"], r["price_unit"])} · '
                   f'sell by {nc.short_date(r["time_exit_date"])}')
            if r["action"]:
                txt += f' · {nc.esc(r["action"][5:])}'
            out[key] = txt
        else:
            out[key] = "<b>Hold</b>" + (f' · {nc.esc(r["action"][5:])}' if r["action"] else "")
    return out


def portfolio():
    rows = nc.query(f"SELECT * FROM `{nc.DATASET}.vw_portfolio_positions` ORDER BY strategy DESC, value_gbp DESC")
    direction = directions()
    bal = nc.query(f"""SELECT cash_total, total_value FROM `{nc.DATASET}.raw_saxo_balances`
                        ORDER BY snapshot_ts DESC LIMIT 1""")
    cash = bal[0]["cash_total"] if bal else 0.0
    saxo_total = bal[0]["total_value"] if bal else None
    out, grand_v, grand_p = "", 0.0, 0.0
    for strat in ("SWING_90", "LONG_TERM", "BONDS", "UNCLASSIFIED"):
        rs = [r for r in rows if r["strategy"] == strat]
        if not rs:
            continue
        v = sum(r["value_gbp"] or 0 for r in rs)
        p = sum(r["unrealised_pnl_gbp"] or 0 for r in rs)
        grand_v, grand_p = grand_v + v, grand_p + p
        body = [[f'<b>{nc.esc(r["ticker"])}</b>', f'{r["quantity"]:,.0f}',
                 nc.price(r["avg_cost_price"], r["price_unit"]), nc.price(r["price"], r["price_unit"]),
                 nc.money(r["value_gbp"]),
                 f'<span style="color:{nc.colour(r["unrealised_pnl_gbp"])}">{nc.money(r["unrealised_pnl_gbp"])}</span>']
                for r in rs]
        notes = [direction.get((r["ticker"], strat), "<b>Hold</b>") for r in rs]
        body.append(["<b>Subtotal</b>", "", "", "", f"<b>{nc.money(v)}</b>",
                     f'<b style="color:{nc.colour(p)}">{nc.money(p)}</b>'])
        notes.append(None)
        out += f'<div style="margin:16px 0 6px;font-size:13px;font-weight:700;color:#0A0A0A">{STRATEGY_LABEL.get(strat, strat)}</div>'
        out += nc.table(["Holding", "Units", "Avg cost", "Price", "Value", "P&L"], body,
                        ["left", "right", "right", "right", "right", "right"],
                        widths=PORTFOLIO_WIDTHS, notes=notes)
    total = grand_v + (cash or 0)          # Saxo account
    manual = nc.query(f"""SELECT account, ticker, sleeve, units, value_gbp, as_of
                           FROM `{nc.DATASET}.manual_holdings` ORDER BY account, value_gbp DESC""")
    manual_v = sum(r["value_gbp"] or 0 for r in manual)
    if manual:
        body = [[f'<b>{nc.esc(r["ticker"])}</b>', f'{r["units"]:,.0f}' if r["units"] else "", "", "",
                 nc.money(r["value_gbp"]), ""] for r in manual]
        notes = [f'{nc.esc(r["account"])} · {nc.esc(r["sleeve"])} · value entered by hand, as of '
                 f'{nc.short_date(r["as_of"])}' for r in manual]
        body.append(["<b>Subtotal</b>", "", "", "", f"<b>{nc.money(manual_v)}</b>", ""])
        notes.append(None)
        out += '<div style="margin:16px 0 6px;font-size:13px;font-weight:700;color:#0A0A0A">Other accounts (not in Saxo)</div>'
        out += nc.table(["Holding", "Units", "Avg cost", "Price", "Value", "P&L"], body,
                        ["left", "right", "right", "right", "right", "right"],
                        widths=PORTFOLIO_WIDTHS, notes=notes)
    check = ""
    if saxo_total and abs(total - saxo_total) > max(50.0, 0.005 * saxo_total):
        check = (f'<p style="color:#C2261C;font-size:12px;margin:6px 0 0">Check: Saxo reports the account at '
                 f'{nc.money(saxo_total)}; the lines below add up to {nc.money(total)}. A trade from today may not be '
                 f'reflected yet.</p>')
    def stat(label, value, color="#0A0A0A"):
        return (f'<td style="padding:0 12px 0 0;vertical-align:top">'
                f'<div style="color:#6B6B6B;font-size:11px;text-transform:uppercase;letter-spacing:0.08em;white-space:nowrap">{label}</div>'
                f'<div style="font-size:19px;font-weight:600;color:{color};white-space:nowrap;margin-top:4px">{value}</div></td>')
    head = (f'<table role="presentation" style="border-collapse:collapse;width:100%"><tr>'
            f'{stat("Total, both ISAs", nc.money(total + manual_v))}{stat("Saxo", nc.money(total))}'
            f'{stat("Cash", nc.money(cash))}'
            f'{stat("Unrealised P&amp;L", nc.money(grand_p), nc.colour(grand_p))}</tr></table>'
            '<p style="color:#6B6B6B;font-size:12px;margin:6px 0 0">Saxo = positions + cash; the total adds the other '
            'accounts listed at the bottom (values entered by hand). All figures in £. '
            'US holdings: cost uses the exchange rate on the day you bought, value uses today\'s rate, so P&amp;L '
            'includes currency moves. P&amp;L is before dealing costs (commission, FX fee, stamp duty). '
            'Cash includes sale proceeds that have not settled yet.</p>' + check)
    return head + out


def actions():
    rows = nc.query(f"""SELECT ticker, open_date, action, days_held, price_unit, last_close, model_stop, time_exit_date
                        FROM `{nc.DATASET}.vw_position_monitor` WHERE action IS NOT NULL
                        ORDER BY STARTS_WITH(action, 'FYI'), ticker""")
    if not rows:
        return "<p>No actions due.</p>"
    notes = []
    for r in rows:
        colour = "#6B6B6B" if r["action"].startswith("FYI") else "#C2261C"
        notes.append(f'<span style="color:{colour};font-weight:600">{nc.esc(r["action"])}</span>'
                     f' · lot opened {nc.short_date(r["open_date"])}')
    return nc.table(["Holding", "Close", "Stop", "Sell by"],
                    [[f'<b>{nc.esc(r["ticker"])}</b>', nc.price(r["last_close"], r["price_unit"]),
                      nc.price(r["model_stop"], r["price_unit"]), nc.short_date(r["time_exit_date"]) if r["time_exit_date"] else "–"]
                     for r in rows],
                    ["left", "right", "right", "right"], widths=["34%", "22%", "22%", "22%"], notes=notes)


def closed_trades():
    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    cur = nc.query(f"""SELECT * FROM `{nc.DATASET}.vw_closed_trades`
                       WHERE sale_month = @m ORDER BY sold_at DESC""", m=this_month)
    prior = nc.query(f"""SELECT sale_month, COUNT(*) trades, SUM(gross_pnl_gbp) gross,
                                SUM(commission_gbp + stamp_duty_gbp) costs, SUM(net_margin_gbp) net
                         FROM `{nc.DATASET}.vw_closed_trades` WHERE sale_month < @m
                         GROUP BY sale_month ORDER BY sale_month DESC""", m=this_month)
    out = ""
    def total_row(label, trades, net, gross=None, costs=None, five=True):
        cells = [f'<b>{label}</b>', f'<b>{trades}</b>']
        cells += ([f'<b>{nc.money(gross)}</b>', f'<b>{nc.money(costs)}</b>'] if gross is not None else ["", ""])
        cells.append(f'<b style="color:{nc.colour(net)}">{nc.money(net)}</b>')
        return cells
    if cur:
        notes = []
        for r in cur:
            inferred = r["price_source"] != "SAXO_CLOSING_ROW"
            notes.append(f'Sold {nc.uk_time(r["sold_at"])} · {STRATEGY_LABEL.get(r["strategy"], r["strategy"])}'
                         f' · est. costs {nc.money(r["commission_gbp"] + r["stamp_duty_gbp"])}'
                         + (" · sale price inferred, not a confirmed fill" if inferred else ""))
        out += nc.table(["Holding", "Qty", "Buy", "Sell", "Net"],
                        [[f'<b>{nc.esc(r["ticker"])}</b>', f'{r["quantity"]:,.0f}',
                          nc.price(r["buy_price"], r["price_unit"]), nc.price(r["sell_price"], r["price_unit"]),
                          f'<span style="color:{nc.colour(r["net_margin_gbp"])}">{nc.money(r["net_margin_gbp"])}</span>']
                         for r in cur] + [total_row(f"Total {datetime.now(timezone.utc):%b}", f"{len(cur)} sales", sum(r["net_margin_gbp"] or 0 for r in cur))],
                        ["left", "right", "right", "right", "right"],
                        widths=["26%", "14%", "20%", "20%", "20%"], notes=notes + [None])
    else:
        out += "<p>No trades closed this month yet.</p>"
    from datetime import date
    cur_sum = {"trades": len(cur), "gross": sum(r["gross_pnl_gbp"] or 0 for r in cur),
               "costs": sum((r["commission_gbp"] or 0) + (r["stamp_duty_gbp"] or 0) for r in cur),
               "net": sum(r["net_margin_gbp"] or 0 for r in cur)}
    months = [{"label": f'{datetime.now(timezone.utc):%b %Y} (so far)', **cur_sum}] if cur else []
    months += [{"label": f'{date.fromisoformat(r["sale_month"] + "-01"):%b %Y}', "trades": r["trades"],
                "gross": r["gross"] or 0, "costs": r["costs"] or 0, "net": r["net"] or 0} for r in prior]
    if months:
        out += '<div style="margin:16px 0 6px;font-size:13px;font-weight:700;color:#0A0A0A">By month</div>'
        body = [[m["label"], nc.esc(m["trades"]), nc.money(m["gross"]), nc.money(m["costs"]),
                 f'<b style="color:{nc.colour(m["net"])}">{nc.money(m["net"])}</b>'] for m in months]
        body.append(total_row("All months", sum(m["trades"] for m in months), sum(m["net"] for m in months),
                              sum(m["gross"] for m in months), sum(m["costs"] for m in months)))
        out += nc.table(["Month", "Sales", "Gross", "Costs", "Net margin"], body,
                        ["left", "right", "right", "right", "right"],
                        widths=["26%", "14%", "20%", "20%", "20%"])
        out += ('<p style="color:#6B6B6B;font-size:12px;margin:6px 0 0">Sales are counted from 28 Sep 2026, when '
                'trade tracking began. Costs: Saxo\'s actual buy costs plus an estimated £5 per sale.</p>')
    return out


def routine_news():
    rows = nc.query(f"""SELECT alert_key, ticker, headline, ai_note FROM `{nc.DATASET}.fact_alert_log`
                        WHERE email_status = 'DIGEST_ONLY' AND digest_included_at IS NULL
                        ORDER BY ticker, created_at""")
    if not rows:
        return "<p>None.</p>", []
    return nc.table(["Holding", "Announcement"],
                    [[f'<b>{nc.esc(r["ticker"])}</b>', nc.esc(r["headline"])] for r in rows],
                    widths=["26%", "74%"], notes=[nc.esc(r["ai_note"]) for r in rows]), \
        [r["alert_key"] for r in rows]


def sync_status():
    r = nc.query(f"""SELECT COUNT(*) runs, COUNTIF(status = 'OK') ok, MAX(IF(status = 'OK', started_at, NULL)) last_ok,
                            COUNTIF(build_status = 'ERROR') build_errors
                     FROM `{nc.DATASET}.ops_saxo_sync_log`
                     WHERE started_at > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)""")[0]
    return (f"<p>Last 24 hours: {r['ok']} of {r['runs']} Saxo syncs OK; last success {nc.uk_time(r['last_ok'])} UK time; "
            f"position-build errors: {r['build_errors']}.</p>")


def main():
    news_html, news_keys = routine_news()
    today = datetime.now(timezone.utc).strftime("%a %d %b %Y")
    inner = (section("Portfolio", portfolio()) + section("Actions and reminders", actions())
             + section("Closed trades", closed_trades()) + section("Routine announcements", news_html)
             + section("System", sync_status()))
    subject = f"Portfolio – {today}"
    nc.send_email(subject, nc.page(subject, inner))
    if news_keys:
        nc.bq.query(f"""UPDATE `{nc.DATASET}.fact_alert_log` SET digest_included_at = CURRENT_TIMESTAMP()
                        WHERE alert_key IN UNNEST(@keys)""",
                    job_config=nc.bigquery.QueryJobConfig(query_parameters=[
                        nc.bigquery.ArrayQueryParameter("keys", "STRING", news_keys)])).result()
    print(f"digest sent: {subject}")


if __name__ == "__main__":
    main()
