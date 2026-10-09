"""Month-end moves block for the morning email (build item 6: mix checker). v18: "Pure mono" style.

rows = output of vw_mix_moves (one dict per sleeve). Returns a compact, email-safe HTML snippet that sits inside the
"Mix against target" card: the next month-end date and staged step, one line per sleeve that moves, the shares band,
and either "Do these moves today" (on the month end, or when a crash-ladder step is reached) or a preview note.
"""
from html import escape

INK, SOFT, FAINT, LINE, BAD = "#0A0A0A", "#6B6B6B", "#8F8F8F", "#EBEBEB", "#C2261C"


def _gbp(x):
    return f"£{abs(x):,.0f}"


def _date(d):
    if isinstance(d, str):
        from datetime import date
        d = date.fromisoformat(d[:10])
    return f"{d:%a} {d.day} {d:%b}"


def mix_moves_html(rows, today=None):
    if not rows:
        return ""
    first = rows[0]
    nd = first["next_month_end_date"]
    if isinstance(nd, str):
        from datetime import date as _d
        nd = _d.fromisoformat(nd[:10])
    due = today is not None and nd == today
    crash_now = any(float(r.get("crash_move_gbp_now") or 0) for r in rows)
    step = first.get("staged_step")
    when = (f'<b style="color:{INK};">Month-end moves &middot; {escape(_date(nd))}</b>'
            + (f' <span style="color:{SOFT};">&middot; step {step} of 3</span>' if step else ""))
    lines = ""
    for r in sorted(rows, key=lambda r: r.get("sleeve_sort", 0)):
        move = float(r.get("move_gbp_at_next_month_end") or 0)
        crash = float(r.get("crash_move_gbp_now") or 0)
        if not move and not crash:
            continue
        amt = f'{"+" if (move or crash) > 0 else "&minus;"}{_gbp(move or crash)}'
        lines += (f'<tr><td style="padding:5px 12px 5px 0; font-size:12.5px; color:{INK}; white-space:nowrap; '
                  f'font-weight:600; width:80px; font-variant-numeric:tabular-nums; vertical-align:top;">{amt}</td>'
                  f'<td style="padding:5px 0; font-size:12.5px; color:{INK}; vertical-align:top;">'
                  f'<b>{escape(r["sleeve"])}</b> <span style="color:{SOFT};">{escape(r.get("instruction") or "")}</span></td></tr>')
    if not lines:
        lines = (f'<tr><td style="padding:5px 0; font-size:12.5px; color:{SOFT};">No moves due: every sleeve is within 2% '
                 f'of target.</td></tr>')
    shares = ""
    if first.get("shares_pct_now") is not None:
        shares = (f'<div style="font-size:12px; color:{SOFT}; margin-top:6px;">Shares {first["shares_pct_now"]:.0f}% now, '
                  f'{first["shares_pct_after_move"]:.0f}% after the move (allowed 35&ndash;85%).</div>')
    if due or crash_now:
        foot = (f'<div style="margin-top:10px;"><span style="background:{INK}; color:#FFFFFF; font-size:12px; font-weight:600; '
                f'padding:6px 10px; border-radius:6px;">Do these moves today'
                f'{" (crash-ladder step reached)" if crash_now and not due else ""}</span></div>')
    else:
        foot = (f'<div style="font-size:11px; color:{FAINT}; margin-top:6px;">Preview. Nothing to do until that day\'s email '
                f'says "Do these moves today"; amounts are re-worked from that day\'s prices.</div>')
    return (f'<div style="font-size:12.5px; margin-bottom:6px;">{when}</div>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">'
            f'{lines}</table>{shares}{foot}')
