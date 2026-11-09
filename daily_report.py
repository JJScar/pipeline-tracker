#!/usr/bin/env python3
"""End-of-day performance report, read straight from pipeline.db (read-only).

Usage:  python3 daily_report.py [YYYY-MM-DD]     (defaults to today)

Funnel counts use the same rules as the Stats tab (server.get_stats): an agency counts
once per stage, and reaching a later stage also credits the earlier ones.
"""
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

import server
import trellus_sync

STALE = {"lead": 7, "info_sent": 4, "submission": 2, "quoted": 7}  # mirrors static/index.html
STAGE_LABEL = {"lead": "Follow Up", "info_sent": "Info Sent", "submission": "Submission In",
               "quoted": "Quoted", "active": "Appointed", "lost": "Lost"}
FUNNEL_KEYS = ["dials", "connects", "lead", "info_sent", "submission", "quoted", "active", "lost",
               "touches", "bound_premium"]


def pct(a, b):
    return f"{100 * a / b:.1f}%" if b else "n/a"


def mmss(sec):
    sec = int(sec or 0)
    return f"{sec // 60}m {sec % 60:02d}s"


def money(v):
    return f"${v:,.0f}"


def avg_of(days):
    """Average per working day (days with any dials), so weekends don't drag it down."""
    worked = [d for d in days if d["dials"] > 0]
    if not worked:
        return None, 0
    return {k: sum(d[k] for d in worked) / len(worked) for k in FUNNEL_KEYS}, len(worked)


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    d = date.fromisoformat(day)
    out = []
    p = out.append

    today = server.get_stats(day, day)
    T = today["totals"]
    week = server.get_stats((d - timedelta(days=7)).isoformat(), (d - timedelta(days=1)).isoformat())
    base, worked_days = avg_of(week["days"])
    # Most recent earlier working day
    prev = next((x for x in reversed(week["days"]) if x["dials"] > 0), None)

    conn = sqlite3.connect(f"file:{server.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    calls = conn.execute("SELECT * FROM trellus_calls WHERE substr(started_at,1,10) = ? ORDER BY started_at",
                         (day,)).fetchall()
    accounts = {r["id"]: r for r in conn.execute("SELECT * FROM accounts")}
    acts = conn.execute(
        """SELECT s.*, a.agency FROM activities s JOIN accounts a ON a.id = s.account_id
           WHERE substr(s.created_at,1,10) = ? ORDER BY s.created_at""", (day,)).fetchall()

    # ---------- Headline ----------
    p(f"# Daily report: {d.strftime('%A %d %b %Y')}\n")
    p("## Headline numbers")
    p("| Metric | Today | 7-day avg | Last working day |")
    p("|---|---|---|---|")
    rows = [("Dials", "dials"), ("Connects", "connects"), ("Interested (new leads)", "lead"),
            ("Info sent", "info_sent"), ("Submissions", "submission"), ("Quotes", "quoted"),
            ("Binds / Appointed", "active"), ("Lost", "lost"), ("Touches logged", "touches")]
    for label, k in rows:
        a = f"{base[k]:.1f}" if base else "-"
        pv = str(prev[k]) if prev else "-"
        p(f"| {label} | **{T[k]}** | {a} | {pv} |")
    p(f"| Bound premium | **{money(T['bound_premium'])}** | "
      f"{money(base['bound_premium']) if base else '-'} | {money(prev['bound_premium']) if prev else '-'} |")
    p(f"\n7-day avg covers {worked_days} working day(s) before today. Last working day = "
      f"{prev['date'] if prev else 'none'}.")
    goal = today.get("dial_goal") or 0
    if goal:
        p(f"Dial goal: {goal} → {pct(T['dials'], goal)} of goal.")

    # ---------- Conversion ----------
    p("\n## Conversion rates")
    p(f"- Connect rate: **{pct(T['connects'], T['dials'])}**"
      + (f" (7-day: {pct(base['connects'], base['dials'])})" if base else ""))
    p(f"- Interested per connect: **{pct(T['lead'], T['connects'])}**"
      + (f" (7-day: {pct(base['lead'], base['connects'])})" if base else ""))
    p(f"- Dials per connect: **{T['dials'] / T['connects']:.1f}**" if T["connects"] else "- Dials per connect: n/a")
    p(f"- Dials per new lead: **{T['dials'] / T['lead']:.1f}**" if T["lead"] else "- Dials per new lead: n/a")
    if T["lead"] > T["connects"]:
        p("- Note: more new leads than connects today, so some cards were entered from earlier work "
          "(backfill or email). Per-call rates for the later stages are inflated.")
    # 0.0 means the card was added straight into a later stage, so it says nothing about speed
    v = {k: x for k, x in today["velocity"].items() if x}
    if v:
        p("- Avg days between stages (for moves made today): "
          + ", ".join(f"{k.replace('>', ' → ')} {x}d" for k, x in v.items()))

    # ---------- Calls (Trellus) ----------
    if calls:
        p("\n## Call breakdown (Trellus)")
        def label(c):
            x = (c["disposition"] or "").strip()
            return "Gatekeeper" if x.lower().replace(" ", "") == "gatekeeper" else (x or "No disposition")
        disp = Counter(label(c) for c in calls)
        p("| Disposition | Calls | Share |")
        p("|---|---|---|")
        for k, n in disp.most_common():
            p(f"| {k} | {n} | {pct(n, len(calls))} |")

        conn_calls = [c for c in calls if (c["disposition"] or "").lower() in trellus_sync.CONNECT]
        talk = sum(c["duration"] or 0 for c in calls)
        conn_talk = sum(c["duration"] or 0 for c in conn_calls)
        gk = disp.get("Gatekeeper", 0)
        p(f"\n- Total time on the phone: **{mmss(talk)}** across {len(calls)} calls")
        if conn_calls:
            p(f"- Avg connect length: **{mmss(conn_talk / len(conn_calls))}**, "
              f"longest: {mmss(max(c['duration'] or 0 for c in conn_calls))}")
        p(f"- Gatekeeper rate: {pct(gk, len(calls))} of dials; "
          f"got past to a decision maker on {pct(len(conn_calls), len(conn_calls) + gk)} of human answers")
        first, last = calls[0]["started_at"][11:16], calls[-1]["started_at"][11:16]
        p(f"- First dial {first}, last dial {last}")

        # Hour by hour
        hours = defaultdict(lambda: [0, 0])
        for c in calls:
            h = int(c["started_at"][11:13])
            hours[h][0] += 1
            if (c["disposition"] or "").lower() in trellus_sync.CONNECT:
                hours[h][1] += 1
        p("\n| Hour | Dials | Connects | Connect % |")
        p("|---|---|---|---|")
        for h in sorted(hours):
            dl, cn = hours[h]
            p(f"| {h:02d}:00 | {dl} | {cn} | {pct(cn, dl)} |")

        # Notable conversations
        good = [c for c in conn_calls if (c["disposition"] or "").lower() in {"interested", "callback", "connected"}]
        if good:
            p("\n**Positive conversations:**")
            for c in good:
                who = " · ".join(x for x in [c["name"], c["company"]] if x) or trellus_sync.pretty_phone(c["phone"])
                note = (c["notes"] or "").replace("\n", " ").strip()
                p(f"- {c['started_at'][11:16]} {c['disposition']} ({mmss(c['duration'])}): {who}"
                  + (f": {note[:160]}" if note else ""))
    else:
        p("\n_No Trellus calls synced for this day._")

    # ---------- Existing accounts (inherited book) ----------
    p("\n## Existing accounts (account management)")
    mine = defaultdict(list)
    for a in acts:
        acc = accounts[a["account_id"]]
        if a["kind"] != "stage" and acc["inherited_stage"]:
            mine[a["account_id"]].append(a)
    if mine:
        p("Touches today on inherited accounts (not counted in the funnel):")
        for aid, items in mine.items():
            acc = accounts[aid]
            who = " · ".join(x for x in [acc["agency"], acc["contact"]] if x)
            p(f"- **{who}** ({STAGE_LABEL[acc['stage']]}, inherited in {STAGE_LABEL[acc['inherited_stage']]}):")
            for a in items:
                body = (a["body"] or "").replace("\n", " ").strip()
                p(f"  - {a['created_at'][11:16]} {a['kind']}: {body[:240]}")
    else:
        p("- No touches on inherited accounts.")

    # ---------- Pipeline movement ----------
    p("\n## Pipeline movement today")
    moves = [a for a in acts if a["kind"] == "stage"]
    if moves:
        for a in moves:
            p(f"- {a['created_at'][11:16]} **{a['agency'][:60]}**: {a['body']}")
    else:
        p("- No stage changes.")
    kinds = Counter(a["kind"] for a in acts if a["kind"] != "stage")
    if kinds:
        p("- Logged on cards: " + ", ".join(f"{n} {k}" for k, n in kinds.most_common()))

    # ---------- Pipeline health ----------
    p("\n## Pipeline right now")
    live = [a for a in accounts.values() if a["stage"] != "lost"]
    by_stage = Counter(a["stage"] for a in accounts.values())
    p(" · ".join(f"{STAGE_LABEL[s]}: {by_stage.get(s, 0)}" for s in server.STAGES))
    tomorrow = (d + timedelta(days=1)).isoformat()
    overdue = [a for a in live if a["stage"] != "active" and a["next_follow_up"] and a["next_follow_up"] < tomorrow]
    due_tmrw = [a for a in live if a["stage"] != "active" and a["next_follow_up"] == tomorrow]
    no_fu = [a for a in live if a["stage"] != "active" and not a["next_follow_up"]]
    ref = datetime.combine(d, datetime.max.time())
    stale = [a for a in live if a["stage"] in STALE and
             (ref - datetime.fromisoformat(a["stage_changed_at"])).days >= STALE[a["stage"]]]
    review = conn.execute("SELECT COUNT(*) FROM trellus_calls WHERE status = 'review'").fetchone()[0]

    def names(lst):
        return ", ".join(a["agency"][:40] for a in lst) or "none"

    p(f"- Follow-ups due by today (not done): {len(overdue)}: {names(overdue)}")
    p(f"- Follow-ups due tomorrow: {len(due_tmrw)}: {names(due_tmrw)}")
    p(f"- Live cards with no follow-up date: {len(no_fu)}: {names(no_fu)}")
    p(f"- Stale cards: {len(stale)}: {names(stale)}")
    p(f"- Trellus calls in Needs review: {review}")
    p(f"- Cards missing an Atlas link (in Quoted or later): "
      f"{sum(1 for a in live if a['stage'] in ('quoted', 'active') and not a['atlas_url'])}")
    conn.close()
    print("\n".join(out))


if __name__ == "__main__":
    main()
