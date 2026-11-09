"""Trellus → Pipeline Tracker sync (read-only against Trellus).

Every few minutes this pulls your calls from the Trellus Call Data API and:
  * sets each day's Dials / Connects from your Trellus calls
  * logs real conversations and voicemails onto the matching agency card
  * puts uncertain matches, and "Interested" calls with no agency yet, into Needs review

Only read calls are made: GET /visible, POST /session-list-v6 (a read query), and, when you open a
call's transcript, GET /session and GET /get-audio-links. Nothing is written to Trellus.
Config comes from .env: TRELLUS_API_KEY and TRELLUS_USER_EMAIL.
"""
import json
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from difflib import SequenceMatcher

LIST_URL = "https://rpt-2.trellus.ai/session-list-v6"
VISIBLE_URL = "https://ext.trellus.ai/visible"
INTERVAL = 5 * 60          # seconds between syncs
BACKFILL_DAYS = 30         # how far back the first sync looks
OVERLAP_HOURS = 24         # re-read this much each sync so later disposition edits are picked up
PENDING_GRACE = 2 * 3600   # wait this long for a disposition before treating a call as dial-only

# What counts as a Connect (spoke to the decision maker), and which calls get logged on cards.
CONNECT = {"connected", "interested", "not interested", "callback", "dq"}
VOICEMAIL = {"left voicemail"}
LOGGABLE = CONNECT | VOICEMAIL

SELECT = ["session_id", "started_at", "customer_address", "customer_data.name",
          "customer_data.company_name", "user_log.disposition", "dial_metric.is_connected",
          "duration", "user_log.notes"]

STOP = {"insurance", "ins", "agency", "agencies", "group", "inc", "llc", "co", "company", "corp",
        "services", "service", "the", "and", "of", "associates", "brokerage", "brokers", "broker",
        "financial", "agcy", "grp"}

# One status + lock per user, so a team's syncs never mix. "local" is the single-user Mac setup.
_locks, _statuses = {}, {}


def status_for(user="local"):
    return _statuses.setdefault(user, {"enabled": False, "last_sync": None, "last_error": None, "running": False})


status = status_for("local")  # kept for scripts that read it directly


# ---------- config ----------
def load_env(path):
    env = {}
    try:
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


# ---------- schema ----------
def init_tables(c):
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS trellus_calls (
            session_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,        -- local ISO time
            phone TEXT DEFAULT '',
            name TEXT DEFAULT '',
            company TEXT DEFAULT '',
            disposition TEXT DEFAULT '',
            duration REAL DEFAULT 0,
            notes TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',  -- pending | dial_only | unmatched | review | logged | dismissed
            account_id INTEGER,              -- matched or suggested agency
            activity_id INTEGER,
            reason TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_tc_status ON trellus_calls(status);
        CREATE INDEX IF NOT EXISTS idx_tc_day ON trellus_calls(substr(started_at,1,10));
        """
    )


# ---------- Trellus API ----------
def _request(url, headers, body=None, tries=4):
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < tries - 1:
                time.sleep(float(e.headers.get("Retry-After") or 5))
                continue
            raise RuntimeError(f"Trellus HTTP {e.code}: {e.read()[:200].decode(errors='replace')}")
    raise RuntimeError("Trellus rate limit: gave up after retries")


def resolve_user_id(key, email):
    d = _request(VISIBLE_URL, {"api_key": key})
    for uid, em in (d.get("user_id_to_email") or {}).items():
        if em.lower() == email.lower():
            return uid
    raise RuntimeError(f"No Trellus user with email {email} is visible to this API key")


def fetch_calls(key, user_id, start, end):
    """All of the user's calls in [start, end), as dicts. Splits the window if it hits the 1,000-row cap."""
    body = {
        "start": int(start.timestamp() * 1e6), "end": int(end.timestamp() * 1e6), "limit": 1000,
        "cnf": [[{"column_name": "conversation_type", "operator": "EQ", "value_safe": 1}],
                [{"column_name": "resource_id", "operator": "EQ", "value_safe": user_id}]],
        "select": SELECT,
    }
    rows = _request(LIST_URL, {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, body)
    time.sleep(2.5)  # stay under 2 requests / 4 s
    if len(rows) >= 1000 and end - start > timedelta(minutes=10):
        mid = start + (end - start) / 2
        return fetch_calls(key, user_id, start, mid) + fetch_calls(key, user_id, mid, end)
    return [dict(zip(SELECT, r)) for r in rows]


# ---------- matching ----------
def norm_phone(p):
    d = re.sub(r"\D", "", str(p or ""))
    return d[-10:] if len(d) >= 10 else ""


def norm_name(s):
    s = str(s or "").lower().replace("&", " and ")
    s = re.split(r"\s[-–(]\s?", s)[0]  # "Douglas Maritime - Acquired by …" → "douglas maritime"
    toks = [t for t in re.findall(r"[a-z0-9]+", s) if t not in STOP]
    return " ".join(toks)


def name_sim(a, b):
    a, b = norm_name(a), norm_name(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = set(a.split()), set(b.split())
    jac = len(ta & tb) / len(ta | tb)
    return max(SequenceMatcher(None, a, b).ratio(), jac)


def first_name(s):
    m = re.findall(r"[a-z]+", str(s or "").lower())
    return m[0] if m and len(m[0]) >= 3 else ""


def match(call, accounts):
    """Return (decision, account_id, reason). decision: 'auto' | 'review' | 'none'."""
    phone = norm_phone(call["phone"])
    labels = [x for x in (call["company"], call["name"]) if x]
    cfirst = first_name(call["name"])
    scored = []
    for a in accounts:
        score, why = 0.0, []
        if phone and phone == norm_phone(a["phone"]):
            score += 3; why.append("phone")
        sim = max((name_sim(l, a["agency"]) for l in labels), default=0)
        if sim >= 0.92:
            score += 3; why.append("agency name")
        elif sim >= 0.75:
            score += 1.5; why.append("similar agency name")
        if cfirst and cfirst == first_name(a["contact"]):
            score += 1; why.append("contact name")
        if score:
            scored.append((score, a["id"], " + ".join(why)))
    if not scored:
        return "none", None, ""
    scored.sort(reverse=True)
    best = scored[0]
    tied = len(scored) > 1 and scored[1][0] == best[0]
    if best[0] >= 3 and not tied:
        return "auto", best[1], best[2]
    if best[0] >= 1.5:
        return "review", best[1], ("Several agencies match: " if tied else "Weak match: ") + best[2]
    return "none", None, ""


# ---------- applying ----------
JUNK_NAME = re.compile(r"^(office contact|unknown prospect|unknown|n/?a|none|\+?[\d\s()\-.]+)$", re.I)


def clean_names(name, company):
    """Drop placeholder names and split 'Agency Inc. : Person Name' into company + contact."""
    name, company = (name or "").strip(), (company or "").strip()
    for field in (name, company):
        if " : " in field:
            co, person = [x.strip() for x in field.split(" : ", 1)]
            company, name = company if company and company != field else co, person
            break
    if JUNK_NAME.match(name):
        name = ""
    if JUNK_NAME.match(company):
        company = ""
    return name, company


def _local_iso(ts):
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _fmt_dur(sec):
    sec = int(sec or 0)
    return f"{sec // 60}m {sec % 60:02d}s" if sec >= 60 else f"{sec}s"


def activity_body(call):
    who = call["name"] if call["name"] and call["name"] != call["company"] else ""
    head = " · ".join(x for x in ["Trellus", call["disposition"], _fmt_dur(call["duration"]), who] if x)
    return head + (f"\n{call['notes']}" if call["notes"] else "")


def log_call(c, call, account_id):
    kind = "vm" if call["disposition"].lower() in VOICEMAIL else "call"
    cur = c.execute("INSERT INTO activities (account_id, kind, body, created_at) VALUES (?,?,?,?)",
                    (account_id, kind, activity_body(call), call["started_at"]))
    c.execute("""UPDATE accounts SET last_touch_at = MAX(COALESCE(last_touch_at, ''), ?),
                 updated_at = MAX(updated_at, ?) WHERE id = ?""",
              (call["started_at"], call["started_at"], account_id))
    c.execute("UPDATE trellus_calls SET status='logged', account_id=?, activity_id=? WHERE session_id=?",
              (account_id, cur.lastrowid, call["session_id"]))


def process_pending(c):
    """Decide what to do with every call that hasn't been handled yet."""
    accounts = [dict(r) for r in c.execute("SELECT id, agency, contact, phone FROM accounts")]
    cutoff = (datetime.now() - timedelta(seconds=PENDING_GRACE)).isoformat(timespec="seconds")
    for r in c.execute("SELECT * FROM trellus_calls WHERE status IN ('pending', 'unmatched')").fetchall():
        call = dict(r)
        disp = call["disposition"].lower()
        if not disp and call["started_at"] > cutoff:
            continue  # disposition probably not entered yet
        if disp not in LOGGABLE:
            c.execute("UPDATE trellus_calls SET status='dial_only' WHERE session_id=?", (call["session_id"],))
            continue
        decision, aid, reason = match(call, accounts)
        if decision == "auto":
            log_call(c, call, aid)
        elif decision == "review" or disp == "interested":
            c.execute("UPDATE trellus_calls SET status='review', account_id=?, reason=? WHERE session_id=?",
                      (aid, reason or "Interested, but no matching agency in the tracker", call["session_id"]))
        else:
            # Re-checked on later syncs, so it gets logged if you add the agency afterwards.
            c.execute("UPDATE trellus_calls SET status='unmatched' WHERE session_id=?", (call["session_id"],))


def recompute_daily(c, days):
    for day in days:
        r = c.execute(
            f"""SELECT COUNT(*) AS dials,
                       SUM(CASE WHEN lower(disposition) IN ({','.join('?' * len(CONNECT))}) THEN 1 ELSE 0 END) AS connects
                FROM trellus_calls WHERE substr(started_at,1,10) = ?""", (*CONNECT, day)).fetchone()
        if not r["dials"]:
            continue
        c.execute("INSERT OR IGNORE INTO daily (date) VALUES (?)", (day,))
        c.execute("UPDATE daily SET dials = ?, connects = ? WHERE date = ?", (r["dials"], r["connects"] or 0, day))


def sync_once(db, env, user="local"):
    """Run one sync. db is a zero-arg function returning a sqlite3 connection (Row factory)."""
    key, email = env.get("TRELLUS_API_KEY"), env.get("TRELLUS_USER_EMAIL")
    if not key or not email:
        raise RuntimeError("Add your Trellus API key and Trellus email in Settings")
    status, lock = status_for(user), _locks.setdefault(user, threading.Lock())
    if not lock.acquire(blocking=False):
        return {"skipped": "a sync is already running"}
    status["running"] = True
    try:
        with db() as c:
            row = c.execute("SELECT value FROM settings WHERE key='trellus_user_id'").fetchone()
            user_id = row["value"] if row else None
            synced = c.execute("SELECT value FROM settings WHERE key='trellus_synced_until'").fetchone()
        if not user_id:
            user_id = resolve_user_id(key, email)
            with db() as c:
                c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('trellus_user_id', ?)", (user_id,))

        end = datetime.now()
        start = (datetime.fromisoformat(synced["value"]) - timedelta(hours=OVERLAP_HOURS)) if synced \
            else (end - timedelta(days=BACKFILL_DAYS)).replace(hour=0, minute=0, second=0, microsecond=0)

        calls, t = [], start
        while t < end:  # one request per day keeps each well under the 1,000-row cap
            nxt = min(t + timedelta(days=1), end)
            calls += fetch_calls(key, user_id, t, nxt)
            t = nxt

        new = 0
        with db() as c:
            for x in calls:
                name, company = clean_names(str(x["customer_data.name"] or ""), str(x["customer_data.company_name"] or ""))
                rec = (x["session_id"], _local_iso(x["started_at"]), str(x["customer_address"] or ""), name, company,
                       str(x["user_log.disposition"] or "").strip(), float(x["duration"] or 0),
                       str(x["user_log.notes"] or "").strip())
                cur = c.execute("""INSERT OR IGNORE INTO trellus_calls
                                   (session_id, started_at, phone, name, company, disposition, duration, notes)
                                   VALUES (?,?,?,?,?,?,?,?)""", rec)
                if cur.rowcount:
                    new += 1
                else:  # dispositions and notes can be edited after the call; refresh calls we haven't acted on
                    c.execute("""UPDATE trellus_calls SET name=?, company=?, disposition=?, duration=?, notes=?,
                                 status = CASE WHEN status IN ('dial_only', 'unmatched') THEN 'pending' ELSE status END
                                 WHERE session_id=? AND status IN ('pending', 'dial_only', 'unmatched')
                                   AND (disposition != ? OR notes != ?)""",
                              (rec[3], rec[4], rec[5], rec[6], rec[7], rec[0], rec[5], rec[7]))
            process_pending(c)
            days = sorted({r["d"] for r in c.execute(
                "SELECT DISTINCT substr(started_at,1,10) AS d FROM trellus_calls WHERE started_at >= ?",
                (start.isoformat(timespec="seconds"),))})
            recompute_daily(c, days)
            c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('trellus_synced_until', ?)",
                      (end.isoformat(timespec="seconds"),))
        status.update(last_sync=datetime.now().isoformat(timespec="seconds"), last_error=None)
        return {"fetched": len(calls), "new": new}
    except Exception as e:
        status["last_error"] = str(e)
        raise
    finally:
        status["running"] = False
        lock.release()


# ---------- review queue ----------
SESSION_URL = "https://rpt-2.trellus.ai/session"
AUDIO_URL = "https://api.trellus.ai/get-audio-links"


def call_detail(env, session_id):
    """Transcript turns and recording links for one call (read-only). Recording links expire after an hour."""
    if not re.fullmatch(r"[0-9a-f]{20,80}", session_id or ""):
        raise ValueError("bad session id")
    key = env.get("TRELLUS_API_KEY")
    if not key:
        raise RuntimeError("Set TRELLUS_API_KEY in .env")
    s = _request(SESSION_URL, {"Authorization": f"Bearer {key}", "x-session-id": session_id, "x-proto-as-json": "true"})
    td = s.get("turnData") or {}
    turns = [{"me": not cust, "start": round(st or 0), "text": tx}
             for st, tx, cust in zip(td.get("starts") or [], td.get("texts") or [], td.get("isCustomers") or [])]
    audio, offset = [], 0.0
    try:
        a = _request(AUDIO_URL, {"api_key": key, "session_id": session_id})
        pairs = [(m, u) for m, u in zip(a.get("audio") or [], a.get("audio_links") or [])
                 if (m or {}).get("audio_status") == "UPLOADED" and u]
        audio = [u for _, u in pairs]
        # Recordings start when the line connects; transcript times count from the start of the call.
        if pairs and pairs[0][0].get("start") and s.get("startedAt"):
            offset = max(0.0, pairs[0][0]["start"] / 1e6 - float(s["startedAt"]))
    except RuntimeError:
        pass  # recording past retention or unavailable; the transcript still shows
    return {"turns": turns, "audio": audio, "offset": round(offset, 1), "duration": s.get("duration") or 0}


def list_review(c):
    rows = c.execute("""SELECT t.*, a.agency AS suggested_agency FROM trellus_calls t
                        LEFT JOIN accounts a ON a.id = t.account_id
                        WHERE t.status = 'review' ORDER BY t.started_at DESC""").fetchall()
    return [dict(r) for r in rows]


def get_review(c, session_id):
    call = c.execute("SELECT * FROM trellus_calls WHERE session_id=? AND status='review'", (session_id,)).fetchone()
    if not call:
        raise ValueError("review item not found")
    return dict(call)


def resolve_review(c, session_id, action, account_id=None):
    """action: 'dismiss', or 'link' to log the call on account_id (also used after creating a new lead)."""
    call = get_review(c, session_id)
    if action == "dismiss":
        c.execute("UPDATE trellus_calls SET status='dismissed' WHERE session_id=?", (session_id,))
    elif action == "link":
        if not c.execute("SELECT 1 FROM accounts WHERE id=?", (int(account_id or 0),)).fetchone():
            raise ValueError("agency not found")
        log_call(c, call, int(account_id))
    else:
        raise ValueError("invalid action")


def pretty_phone(p):
    d = norm_phone(p)
    return f"+1 ({d[:3]}) {d[3:6]}-{d[6:]}" if d else str(p or "")
