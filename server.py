#!/usr/bin/env python3
"""Pipeline Tracker — a tiny local CRM for broker-agency outbound.

No dependencies: Python stdlib + SQLite. Data lives in pipeline.db next to this file.
Run:  python3 server.py   then open http://localhost:8765
"""
import csv
import hashlib
import hmac
import imaplib
import io
import json
import os
import re
import sqlite3
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import email_sync
import trellus_sync

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "pipeline.db"
STATIC = ROOT / "static"
ENV = trellus_sync.load_env(ROOT / ".env")


def _cfg(key, default=""):
    """Server setting: a real environment variable wins, then .env, then the default."""
    return (os.environ.get(key) or ENV.get(key) or default).strip()


# ---------- deployment mode ----------
# local  (default): one person on their own Mac. No login, data in ./pipeline.db, only reachable from this computer.
# header (hosted):  behind the company platform, which signs people in and tells us who they are in a request
#                   header. Each person gets their own database; managers (ADMIN_EMAILS) get a read-only team view.
MODE = _cfg("AUTH_MODE", "local").lower()
HOSTED = MODE == "header"
HOST = _cfg("HOST", "0.0.0.0" if HOSTED else "127.0.0.1")
PORT = next((int(a) for a in sys.argv[1:] if a.isdigit()), int(_cfg("PORT", "8765")))
DATA_DIR = Path(_cfg("DATA_DIR", str(ROOT / "data"))).resolve()
USER_HEADER = _cfg("USER_HEADER", "X-User-Email")
SECRET_HEADER = _cfg("PROXY_SECRET_HEADER", "X-Proxy-Secret")
PROXY_SECRET = _cfg("PROXY_SECRET")
ADMINS = {e.strip().lower() for e in _cfg("ADMIN_EMAILS").split(",") if e.strip()}
DOMAINS = {d.strip().lower().lstrip("@") for d in _cfg("ALLOWED_EMAIL_DOMAINS").split(",") if d.strip()}
# A Trellus API key can see teammates' calls, so on the shared server people may only sync their own Trellus email.
LOCK_TRELLUS_EMAIL = HOSTED and _cfg("ALLOW_OTHER_TRELLUS_EMAIL").lower() not in ("1", "true", "yes")
SYNC_EVERY = 5 * 60
EMAIL_RE = re.compile(r"[a-z0-9._%+'-]+@[a-z0-9.-]+\.[a-z]{2,}")

STAGES = ["lead", "info_sent", "submission", "quoted", "active", "lost"]
FIELDS = [
    "agency", "contact", "title", "email", "phone", "state", "website",
    "lines", "stage", "next_follow_up", "premium", "notes", "twenty_url", "atlas_url",
    "inherited_stage",  # '' = new lead; else the stage it was already in when taken over
    "hours", "tz",
    "gaps",  # what they asked for that we don't offer (e.g. commercial auto market)  # opening hours text ('' = Mon-Fri 9-5) and IANA timezone ('' = from phone/state)
]


def now():
    return datetime.now().isoformat(timespec="seconds")


# Which person's database this request (thread) works on. Set by Handler._identify.
_ctx = threading.local()


def db(path=None):
    path = path or getattr(_ctx, "db_path", None) or (None if HOSTED else DB_PATH)
    if path is None:
        raise RuntimeError("no signed-in user for this request")
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


_ready, _ready_lock = set(), threading.Lock()


def ensure_db(path):
    with _ready_lock:
        if str(path) not in _ready:
            init_db(path)
            _ready.add(str(path))
    return path


def init_db(path=None):
    with db(path) as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agency TEXT NOT NULL,
                contact TEXT DEFAULT '',
                title TEXT DEFAULT '',
                email TEXT DEFAULT '',
                phone TEXT DEFAULT '',
                state TEXT DEFAULT '',
                website TEXT DEFAULT '',
                lines TEXT DEFAULT '',
                stage TEXT NOT NULL DEFAULT 'lead',
                next_follow_up TEXT DEFAULT '',
                premium TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                stage_changed_at TEXT NOT NULL,
                last_touch_at TEXT
            );
            CREATE TABLE IF NOT EXISTS activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                body TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_act_account ON activities(account_id);
            CREATE TABLE IF NOT EXISTS daily (
                date TEXT PRIMARY KEY,
                dials INTEGER NOT NULL DEFAULT 0,
                connects INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                due TEXT DEFAULT '',          -- YYYY-MM-DD, '' = no date
                follow_up_with TEXT DEFAULT '',  -- who to chase / who it's with
                blocker TEXT DEFAULT '',      -- what's in the way
                blocker_owner TEXT DEFAULT '',  -- who can unblock it
                done_at TEXT,                 -- NULL = open
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_task_account ON tasks(account_id);
            """
        )
        acols = [r["name"] for r in c.execute("PRAGMA table_info(accounts)")]
        for col in ("twenty_url", "atlas_url", "inherited_stage", "hours", "tz", "gaps"):
            if col not in acols:
                c.execute(f"ALTER TABLE accounts ADD COLUMN {col} TEXT DEFAULT ''")
        cols = [r["name"] for r in c.execute("PRAGMA table_info(activities)")]
        if "to_stage" not in cols:
            c.execute("ALTER TABLE activities ADD COLUMN to_stage TEXT")
            for r in c.execute("SELECT id, body FROM activities WHERE kind = 'stage'").fetchall():
                b = r["body"]
                to = b.split("Created in ", 1)[1] if b.startswith("Created in ") else b.split("→")[-1]
                c.execute("UPDATE activities SET to_stage = ? WHERE id = ?", (to.strip(), r["id"]))
        trellus_sync.init_tables(c)
        email_sync.init_tables(c)


def row_to_dict(r):
    return {k: r[k] for k in r.keys()}


def list_accounts():
    with db() as c:
        rows = c.execute(
            """SELECT a.*, (SELECT COUNT(*) FROM activities WHERE account_id = a.id) AS activity_count
               FROM accounts a ORDER BY updated_at DESC"""
        ).fetchall()
    return [row_to_dict(r) for r in rows]


def get_account(aid):
    with db() as c:
        r = c.execute("SELECT * FROM accounts WHERE id = ?", (aid,)).fetchone()
        if not r:
            return None
        acts = c.execute(
            "SELECT * FROM activities WHERE account_id = ? ORDER BY created_at DESC, id DESC", (aid,)
        ).fetchall()
        tasks = c.execute(TASK_ORDER_SQL.format(where="t.account_id = ?"), (aid,)).fetchall()
    d = row_to_dict(r)
    d["activities"] = [row_to_dict(a) for a in acts]
    d["tasks"] = [row_to_dict(t) for t in tasks]
    return d


# ---------- per-client to-dos ----------
TASK_FIELDS = ("title", "due", "follow_up_with", "blocker", "blocker_owner")
# Open first (soonest due first, undated last), then done (most recent first).
TASK_ORDER_SQL = """SELECT t.*, a.agency FROM tasks t JOIN accounts a ON a.id = t.account_id
    WHERE {where}
    ORDER BY (t.done_at IS NOT NULL), CASE WHEN t.due = '' THEN 1 ELSE 0 END, t.due, t.done_at DESC, t.id"""


def _task_values(payload):
    out = {}
    for f in TASK_FIELDS:
        if f in payload:
            v = str(payload[f] or "").strip()
            if f == "due" and v:
                datetime.fromisoformat(v)  # raises ValueError -> 400
            out[f] = v
    return out


def list_open_tasks():
    with db() as c:
        rows = c.execute(TASK_ORDER_SQL.format(where="t.done_at IS NULL")).fetchall()
    return [row_to_dict(r) for r in rows]


def add_task(aid, payload):
    data = _task_values(payload)
    if not data.get("title"):
        raise ValueError("task needs a title")
    with db() as c:
        if not c.execute("SELECT 1 FROM accounts WHERE id = ?", (aid,)).fetchone():
            return None
        cols = list(data) + ["account_id", "created_at"]
        c.execute(f"INSERT INTO tasks ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                  list(data.values()) + [aid, now()])
    return get_account(aid)


def update_task(tid, payload):
    data = _task_values(payload)
    if "title" in data and not data["title"]:
        raise ValueError("task needs a title")
    with db() as c:
        t = c.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        if not t:
            return None
        sets, vals = [f"{k} = ?" for k in data], list(data.values())
        if "done" in payload:
            done = bool(payload["done"])
            if done and not t["done_at"]:
                ts = now()
                sets.append("done_at = ?"); vals.append(ts)
                # Leave a trace on the timeline. Not a touch, so last_touch_at and stats stay put.
                c.execute("INSERT INTO activities (account_id, kind, body, created_at) VALUES (?,?,?,?)",
                          (t["account_id"], "note", f"✓ Done: {data.get('title', t['title'])}", ts))
            elif not done and t["done_at"]:
                sets.append("done_at = NULL")
        if sets:
            c.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", vals + [tid])
    return get_account(t["account_id"])


def delete_task(tid):
    with db() as c:
        c.execute("DELETE FROM tasks WHERE id = ?", (tid,))


def clean(payload):
    out = {}
    for f in FIELDS:
        if f in payload:
            v = payload[f]
            out[f] = "" if v is None else str(v).strip()
    if "stage" in out and out["stage"] not in STAGES:
        raise ValueError("invalid stage")
    if out.get("inherited_stage") and out["inherited_stage"] not in FUNNEL:
        raise ValueError("invalid inherited stage")
    return out


def create_account(payload):
    data = clean(payload)
    if not data.get("agency"):
        raise ValueError("agency is required")
    data.setdefault("stage", "lead")
    ts = now()
    cols = list(data.keys()) + ["created_at", "updated_at", "stage_changed_at", "last_touch_at"]
    vals = list(data.values()) + [ts, ts, ts, ts]
    with db() as c:
        cur = c.execute(
            f"INSERT INTO accounts ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals
        )
        aid = cur.lastrowid
        c.execute(
            "INSERT INTO activities (account_id, kind, body, created_at, to_stage) VALUES (?,?,?,?,?)",
            (aid, "stage", f"Created in {data['stage']}", ts, data["stage"]),
        )
        if payload.get("first_note"):
            c.execute(
                "INSERT INTO activities (account_id, kind, body, created_at) VALUES (?,?,?,?)",
                (aid, "note", str(payload["first_note"]).strip(), ts),
            )
    return get_account(aid)


def update_account(aid, payload):
    data = clean(payload)
    if "agency" in data and not data["agency"]:
        raise ValueError("agency is required")
    ts = now()
    with db() as c:
        cur = c.execute("SELECT stage FROM accounts WHERE id = ?", (aid,)).fetchone()
        if not cur:
            return None
        sets = [f"{k} = ?" for k in data] + ["updated_at = ?"]
        vals = list(data.values()) + [ts]
        if "stage" in data and data["stage"] != cur["stage"]:
            sets += ["stage_changed_at = ?", "last_touch_at = ?"]
            vals += [ts, ts]
            c.execute(
                "INSERT INTO activities (account_id, kind, body, created_at, to_stage) VALUES (?,?,?,?,?)",
                (aid, "stage", f"{cur['stage']} → {data['stage']}", ts, data["stage"]),
            )
        c.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id = ?", vals + [aid])
    return get_account(aid)


def add_activity(aid, payload):
    kind = payload.get("kind", "note")
    if kind not in ("call", "email", "note", "vm", "meeting"):
        raise ValueError("invalid activity kind")
    ts = now()
    with db() as c:
        if not c.execute("SELECT 1 FROM accounts WHERE id = ?", (aid,)).fetchone():
            return None
        c.execute(
            "INSERT INTO activities (account_id, kind, body, created_at) VALUES (?,?,?,?)",
            (aid, kind, str(payload.get("body", "")).strip(), ts),
        )
        sets, vals = ["last_touch_at = ?", "updated_at = ?"], [ts, ts]
        if "next_follow_up" in payload:
            sets.append("next_follow_up = ?")
            vals.append(str(payload["next_follow_up"] or ""))
        c.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id = ?", vals + [aid])
    return get_account(aid)


def delete_account(aid):
    with db() as c:
        c.execute("DELETE FROM accounts WHERE id = ?", (aid,))


def update_activity(act_id, payload):
    """Edit the text or type of a logged activity. Stage changes can't be edited."""
    sets, vals = [], []
    if "body" in payload:
        sets.append("body = ?"); vals.append(str(payload["body"] or "").strip())
    if "kind" in payload:
        if payload["kind"] not in ("call", "email", "note", "vm", "meeting"):
            raise ValueError("invalid activity kind")
        sets.append("kind = ?"); vals.append(payload["kind"])
    with db() as c:
        row = c.execute("SELECT kind FROM activities WHERE id = ?", (act_id,)).fetchone()
        if not row:
            return None
        if row["kind"] == "stage":
            raise ValueError("stage changes can't be edited")
        if sets:
            c.execute(f"UPDATE activities SET {', '.join(sets)} WHERE id = ?", vals + [act_id])
        return row_to_dict(c.execute("SELECT * FROM activities WHERE id = ?", (act_id,)).fetchone())


def delete_activity(act_id):
    with db() as c:
        c.execute("DELETE FROM activities WHERE id = ?", (act_id,))


FUNNEL = ["lead", "info_sent", "submission", "quoted", "active"]
TOUCH_KINDS = ("call", "vm", "email", "meeting")


def _premium(v):
    try:
        return float("".join(ch for ch in str(v) if ch.isdigit() or ch == ".") or 0)
    except ValueError:
        return 0.0


def _milestones(c):
    """First time each account reached each funnel stage.

    Reaching a later stage also credits any earlier stage it skipped (e.g. an agency
    added straight into 'Submission In' still counts as interested + info sent), so
    the funnel never inverts. Dragging a card backwards and forwards counts once.
    """
    rows = c.execute(
        """SELECT a.id AS account_id, a.premium, a.inherited_stage, s.to_stage, s.created_at
           FROM activities s JOIN accounts a ON a.id = s.account_id
           WHERE s.kind = 'stage' ORDER BY s.created_at, s.id"""
    ).fetchall()
    first, premium = {}, {}
    for r in rows:
        aid, st, ts = r["account_id"], r["to_stage"], r["created_at"]
        premium[aid] = _premium(r["premium"])
        m = first.setdefault(aid, {})
        # Inherited accounts were already at inherited_stage when taken over: that stage and
        # the ones before it aren't new work, so only moves beyond it count.
        base = FUNNEL.index(r["inherited_stage"]) + 1 if r["inherited_stage"] in FUNNEL else 0
        if st == "lost":
            m.setdefault("lost", ts)
        elif st in FUNNEL:
            for earlier in FUNNEL[base: FUNNEL.index(st) + 1]:
                m.setdefault(earlier, ts)
    return first, premium


def get_stats(start, end):
    """Per-day performance between start and end (YYYY-MM-DD, inclusive)."""
    from datetime import date, timedelta

    with db() as c:
        first, premium = _milestones(c)
        daily = {r["date"]: r for r in c.execute("SELECT * FROM daily WHERE date BETWEEN ? AND ?", (start, end))}
        touches = {r["d"]: r["n"] for r in c.execute(
            f"""SELECT substr(created_at,1,10) AS d, COUNT(*) AS n FROM activities
                WHERE kind IN ({','.join('?' * len(TOUCH_KINDS))}) AND substr(created_at,1,10) BETWEEN ? AND ?
                GROUP BY d""", (*TOUCH_KINDS, start, end))}
        goal = c.execute("SELECT value FROM settings WHERE key = 'dial_goal'").fetchone()

    days = {}
    d, last = date.fromisoformat(start), date.fromisoformat(end)
    while d <= last:
        k = d.isoformat()
        r = daily.get(k)
        days[k] = {"date": k, "dials": r["dials"] if r else 0, "connects": r["connects"] if r else 0,
                   "touches": touches.get(k, 0), "lead": 0, "info_sent": 0, "submission": 0,
                   "quoted": 0, "active": 0, "lost": 0, "bound_premium": 0.0}
        d += timedelta(days=1)

    durations = {f"{a}>{b}": [] for a, b in zip(FUNNEL, FUNNEL[1:])}
    durations["lead>active"] = []
    for aid, m in first.items():
        for st, ts in m.items():
            day = days.get(ts[:10])
            if day:
                day[st] += 1
                if st == "active":
                    day["bound_premium"] += premium.get(aid, 0)
        for a, b in list(zip(FUNNEL, FUNNEL[1:])) + [("lead", "active")]:
            if a in m and b in m and start <= m[b][:10] <= end:
                secs = (datetime.fromisoformat(m[b]) - datetime.fromisoformat(m[a])).total_seconds()
                durations[f"{a}>{b}"].append(secs / 86400)

    totals = {k: sum(x[k] for x in days.values()) for k in
              ["dials", "connects", "touches", "lead", "info_sent", "submission", "quoted", "active", "lost", "bound_premium"]}
    velocity = {k: (round(sum(v) / len(v), 1) if v else None) for k, v in durations.items()}
    return {"start": start, "end": end, "days": list(days.values()), "totals": totals,
            "velocity": velocity, "dial_goal": int(goal["value"]) if goal else 0}


def first_activity_date():
    with db() as c:
        r = c.execute("""SELECT MIN(d) AS d FROM (SELECT MIN(substr(created_at,1,10)) AS d FROM activities
                         UNION SELECT MIN(date) FROM daily)""").fetchone()
    return r["d"] if r and r["d"] else datetime.now().date().isoformat()


def bump_daily(payload):
    day = str(payload.get("date") or datetime.now().date().isoformat())[:10]
    datetime.fromisoformat(day)  # validates, raises ValueError
    with db() as c:
        c.execute("INSERT OR IGNORE INTO daily (date) VALUES (?)", (day,))
        for k in ("dials", "connects"):
            if k in payload:  # set an exact value
                c.execute(f"UPDATE daily SET {k} = MAX(0, ?) WHERE date = ?", (int(payload[k]), day))
            if f"{k}_delta" in payload:
                c.execute(f"UPDATE daily SET {k} = MAX(0, {k} + ?) WHERE date = ?", (int(payload[f"{k}_delta"]), day))
        r = c.execute("SELECT * FROM daily WHERE date = ?", (day,)).fetchone()
    return row_to_dict(r)


def set_setting(key, value):
    with db() as c:
        c.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                  (key, str(value)))


def get_orders():
    """Your manual card order per list, e.g. {"board:lead": [12, 4, 9], "sections": ["fu", "toQuote"]}."""
    with db() as c:
        r = c.execute("SELECT value FROM settings WHERE key = 'order'").fetchone()
    try:
        return json.loads(r["value"]) if r else {}
    except ValueError:
        return {}


def set_order(key, ids):
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_:]{1,40}", key):
        raise ValueError("invalid order key")
    if not isinstance(ids, list) or len(ids) > 5000 or not all(isinstance(i, (int, str)) for i in ids):
        raise ValueError("invalid order")
    orders = get_orders()
    orders[key] = ids
    set_setting("order", json.dumps(orders))
    return orders


# ---------- people (hosted mode) ----------
def registry():
    (DATA_DIR / "users").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATA_DIR / "users.db", timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS users (email TEXT PRIMARY KEY, db_file TEXT NOT NULL,
                    created_at TEXT NOT NULL, last_seen TEXT)""")
    return conn


def user_db_path(email, create=False):
    """This person's own database file, or None. create=True signs up someone new on first visit."""
    with registry() as c:
        r = c.execute("SELECT db_file, last_seen FROM users WHERE email = ?", (email,)).fetchone()
        ts = now()
        if r:
            fname = r["db_file"]
            if create and (r["last_seen"] or "")[:16] != ts[:16]:
                c.execute("UPDATE users SET last_seen = ? WHERE email = ?", (ts, email))
        elif create:
            slug = re.sub(r"[^a-z0-9]+", "-", email.split("@")[0]).strip("-")[:30] or "user"
            fname = f"{slug}-{hashlib.sha256(email.encode()).hexdigest()[:10]}.db"
            c.execute("INSERT INTO users (email, db_file, created_at, last_seen) VALUES (?,?,?,?)", (email, fname, ts, ts))
        else:
            return None
    return ensure_db(DATA_DIR / "users" / fname)


def list_users():
    if not HOSTED:
        return [{"email": "local", "path": DB_PATH, "last_seen": None}]
    with registry() as c:
        rows = c.execute("SELECT * FROM users ORDER BY email").fetchall()
    return [{"email": r["email"], "path": ensure_db(DATA_DIR / "users" / r["db_file"]), "last_seen": r["last_seen"]}
            for r in rows]


# ---------- each person's keys (Settings) ----------
# Saved in that person's own database. Secrets are never sent back to the browser, only "saved, ends in 1234".
SECRET_SETTINGS = {"trellus_api_key": "TRELLUS_API_KEY", "google_app_password": "EMAIL_APP_PASSWORD"}
PLAIN_SETTINGS = {"trellus_user_email": "TRELLUS_USER_EMAIL", "email_address": "EMAIL_ADDRESS"}
ENV_KEYS = ("TRELLUS_API_KEY", "TRELLUS_USER_EMAIL", "EMAIL_ADDRESS", "EMAIL_APP_PASSWORD", "GOOGLE_APP_PASSWORD")


def _settings_map(path=None):
    with db(path) as c:
        return {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM settings")}


def user_env(path=None, email=None):
    """Keys for one person's syncs. On your Mac, .env still works as a fallback; on the server it's never used."""
    s = _settings_map(path)
    env = {} if HOSTED else {k: ENV[k] for k in ENV_KEYS if ENV.get(k)}
    if not env.get("EMAIL_APP_PASSWORD") and env.get("GOOGLE_APP_PASSWORD"):
        env["EMAIL_APP_PASSWORD"] = env["GOOGLE_APP_PASSWORD"]
    for k, e in {**SECRET_SETTINGS, **PLAIN_SETTINGS}.items():
        if s.get(k):
            env[e] = s[k]
    if HOSTED and email:
        env.setdefault("TRELLUS_USER_EMAIL", email)
        env.setdefault("EMAIL_ADDRESS", email)
        if LOCK_TRELLUS_EMAIL:
            env["TRELLUS_USER_EMAIL"] = email
    return env


def public_settings():
    s, env = _settings_map(), user_env(email=_ctx.user if HOSTED else None)

    def secret(k):
        if s.get(k):
            return {"set": True, "source": "settings", "hint": f"ends in {s[k][-4:]}" if k == "trellus_api_key" else ""}
        if env.get(SECRET_SETTINGS[k]):
            return {"set": True, "source": "env", "hint": "from the .env file"}
        return {"set": False, "source": "", "hint": ""}

    return {
        "trellus_user_email": env.get("TRELLUS_USER_EMAIL", ""),
        "trellus_email_locked": LOCK_TRELLUS_EMAIL,
        "email_address": env.get("EMAIL_ADDRESS") or env.get("TRELLUS_USER_EMAIL", ""),
        "trellus_api_key": secret("trellus_api_key"),
        "google_app_password": secret("google_app_password"),
        "dial_goal": int(s.get("dial_goal") or 0),
    }


def save_settings(payload):
    s = _settings_map()
    reset = set()
    with db() as c:
        def put(k, v):
            c.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, v))

        if "dial_goal" in payload:
            put("dial_goal", str(max(0, int(payload["dial_goal"] or 0))))
        for k in PLAIN_SETTINGS:
            if k not in payload or (k == "trellus_user_email" and LOCK_TRELLUS_EMAIL):
                continue
            v = str(payload[k] or "").strip().lower()
            if v and not EMAIL_RE.fullmatch(v):
                raise ValueError(f"'{v}' doesn't look like an email address")
            if v != s.get(k, ""):
                put(k, v)
                reset.add(k)
        for k in SECRET_SETTINGS:
            v = re.sub(r"\s", "", str(payload.get(k) or "")) if k == "google_app_password" else str(payload.get(k) or "").strip()
            if v and v != s.get(k):
                put(k, v)
                reset.add(k)
            if k in (payload.get("clear") or []):
                c.execute("DELETE FROM settings WHERE key = ?", (k,))
                reset.add(k)
        # New Trellus account → look the user up again. New mailbox → start its 14-day look-back fresh.
        if reset & {"trellus_api_key", "trellus_user_email"}:
            c.execute("DELETE FROM settings WHERE key = 'trellus_user_id'")
        if "email_address" in reset:
            c.execute("DELETE FROM settings WHERE key IN ('email_last_uid', 'email_uidvalidity')")
    if reset:
        kick_sync(_ctx.user, _ctx.db_path or DB_PATH)
    return public_settings()


def test_connection(payload):
    """Check keys before (or after) saving. Uses what's typed in the form, falling back to what's saved."""
    env = user_env(email=_ctx.user if HOSTED else None)
    service = payload.get("service")
    if service == "trellus":
        key = str(payload.get("trellus_api_key") or "").strip() or env.get("TRELLUS_API_KEY")
        email = env["TRELLUS_USER_EMAIL"] if LOCK_TRELLUS_EMAIL else \
            (str(payload.get("trellus_user_email") or "").strip() or env.get("TRELLUS_USER_EMAIL", ""))
        if not key or not email:
            return {"ok": False, "message": "Add your Trellus email and API key first."}
        try:
            trellus_sync.resolve_user_id(key, email)
            return {"ok": True, "message": f"Connected to Trellus as {email}."}
        except Exception as e:
            msg = str(e)
            if "HTTP 401" in msg or "HTTP 403" in msg:
                msg = "Trellus didn't accept that API key."
            return {"ok": False, "message": msg}
    if service == "email":
        me = (str(payload.get("email_address") or "").strip() or env.get("EMAIL_ADDRESS") or env.get("TRELLUS_USER_EMAIL", "")).lower()
        pw = re.sub(r"\s", "", str(payload.get("google_app_password") or "")) or env.get("EMAIL_APP_PASSWORD", "")
        if not me or not pw:
            return {"ok": False, "message": "Add your work email and Google app password first."}
        try:
            M = imaplib.IMAP4_SSL(email_sync.HOST, timeout=30)
            try:
                M.login(me, pw)
            finally:
                try:
                    M.logout()
                except Exception:
                    pass
            return {"ok": True, "message": f"Connected to {me}. Emails will start syncing."}
        except imaplib.IMAP4.error:
            return {"ok": False, "message": "Google didn't accept that email and app password. Make sure it's an app password, not your normal password."}
        except Exception as e:
            return {"ok": False, "message": f"Couldn't reach Gmail: {e}"}
    raise ValueError("unknown service")


def sync_state(module, path=None, email=None):
    env = user_env(path, email)
    on = bool(env.get("TRELLUS_API_KEY") and env.get("TRELLUS_USER_EMAIL")) if module is trellus_sync \
        else all(email_sync.config(env))
    return {**module.status_for(email or _ctx.user), "enabled": on}


# ---------- background syncs: every person with keys, every 5 minutes ----------
def _log(msg):
    print(f"[{datetime.now():%H:%M}] {msg}", flush=True)


def sync_user(user, path):
    env = user_env(path, None if user == "local" else user)
    who = "" if user == "local" else f"{user} · "
    for name, module, ready in (("Trellus", trellus_sync, env.get("TRELLUS_API_KEY") and env.get("TRELLUS_USER_EMAIL")),
                                ("Email", email_sync, all(email_sync.config(env)))):
        if not ready:
            continue
        try:
            _log(f"{who}{name} sync: {module.sync_once(lambda: db(path), env, user)}")
        except Exception as e:
            _log(f"{who}{name} sync failed: {e}")


def kick_sync(user, path):
    threading.Thread(target=sync_user, args=(user, path), daemon=True).start()


def scheduler():
    while True:
        for u in list_users():
            sync_user(u["email"], u["path"])
        time.sleep(SYNC_EVERY)


# ---------- manager view ----------
def team_overview(start, end):
    out = []
    for u in list_users():
        _ctx.db_path = u["path"]
        try:
            st = get_stats(start, end)["totals"]
            with db() as c:
                stages = {r["stage"]: r["n"] for r in c.execute("SELECT stage, COUNT(*) AS n FROM accounts GROUP BY stage")}
                open_tasks = c.execute("SELECT COUNT(*) FROM tasks WHERE done_at IS NULL").fetchone()[0]
            env = user_env(u["path"], u["email"])
            out.append({"email": u["email"], "last_seen": u["last_seen"], "totals": st, "stages": stages,
                        "open_tasks": open_tasks,
                        "trellus": bool(env.get("TRELLUS_API_KEY")), "email_sync": all(email_sync.config(env)),
                        "sync_error": bool(trellus_sync.status_for(u["email"])["last_error"]
                                           or email_sync.status_for(u["email"])["last_error"])})
        finally:
            _ctx.db_path = _ctx.me_path
    return {"start": start, "end": end, "people": out}


def export_csv():
    rows = list_accounts()
    buf = io.StringIO()
    cols = ["id"] + FIELDS + ["created_at", "updated_at", "stage_changed_at", "last_touch_at"]
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep the terminal quiet

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _parts(self):
        return [p for p in urlparse(self.path).path.split("/") if p]

    def _identify(self, p, method):
        """Work out who's asking and which database they get. Returns (code, message) to refuse, or None."""
        _ctx.user = _ctx.db_path = _ctx.me_path = _ctx.viewing = None
        _ctx.admin = False
        if not HOSTED:
            _ctx.user, _ctx.db_path, _ctx.me_path = "local", DB_PATH, DB_PATH
        else:
            got = (self.headers.get(SECRET_HEADER) or "").encode()
            if not hmac.compare_digest(got, PROXY_SECRET.encode()):
                return 401, "Open the tracker through the company platform."
            email = (self.headers.get(USER_HEADER) or "").strip().lower()
            if not EMAIL_RE.fullmatch(email):
                return 401, "The platform didn't say who you are."
            if DOMAINS and email.rsplit("@", 1)[1] not in DOMAINS:
                return 403, "This tracker is only for company accounts."
            _ctx.user, _ctx.admin = email, email in ADMINS
            _ctx.db_path = _ctx.me_path = user_db_path(email, create=True)
        # Managers can look at someone else's tracker, read-only: ?as=person@company.com
        target = (parse_qs(urlparse(self.path).query).get("as") or [""])[0].strip().lower()
        if target and p and p[0] == "api" and p[1:2] not in (["me"], ["settings"], ["team"]) and target != _ctx.user:
            if not _ctx.admin:
                return 403, "Only managers can view someone else's tracker."
            if method != "GET":
                return 403, "Read-only: you're viewing someone else's tracker."
            path = user_db_path(target)
            if not path:
                return 404, "No tracker for that person."
            _ctx.db_path, _ctx.viewing = path, target
        return None

    def _route(self, method):
        p = self._parts()
        if p == ["healthz"]:  # for the platform's uptime checks; reveals nothing
            return self._send(200, "ok", "text/plain")
        try:
            refused = self._identify(p, method)
            if refused:
                if p and p[0] == "api":
                    return self._send(refused[0], {"error": refused[1]})
                return self._send(refused[0], refused[1], "text/plain; charset=utf-8")
            if not p or p[0] != "api":
                return self._static(method)
            if p == ["api", "me"] and method == "GET":
                return self._send(200, {"email": _ctx.user, "admin": _ctx.admin, "mode": MODE})
            if p == ["api", "settings"] and method == "GET":
                return self._send(200, public_settings())
            if p == ["api", "settings", "test"] and method == "POST":
                return self._send(200, test_connection(self._json()))
            if p == ["api", "team"] and method == "GET":
                if not _ctx.admin:
                    return self._send(403, {"error": "managers only"})
                q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                start, end = q.get("from") or datetime.now().date().isoformat(), q.get("to") or datetime.now().date().isoformat()
                datetime.fromisoformat(start); datetime.fromisoformat(end)
                return self._send(200, team_overview(start, end))
            if p == ["api", "accounts"]:
                if method == "GET":
                    return self._send(200, list_accounts())
                if method == "POST":
                    return self._send(201, create_account(self._json()))
            if len(p) >= 3 and p[1] == "accounts" and p[2].isdigit():
                aid = int(p[2])
                if len(p) == 3:
                    if method == "GET":
                        r = get_account(aid)
                    elif method == "PATCH":
                        r = update_account(aid, self._json())
                    elif method == "DELETE":
                        delete_account(aid)
                        return self._send(200, {"ok": True})
                    else:
                        r = None
                    return self._send(200, r) if r else self._send(404, {"error": "not found"})
                if len(p) == 4 and p[3] == "tasks" and method == "POST":
                    r = add_task(aid, self._json())
                    return self._send(201, r) if r else self._send(404, {"error": "not found"})
                if len(p) == 4 and p[3] == "activities" and method == "POST":
                    r = add_activity(aid, self._json())
                    return self._send(201, r) if r else self._send(404, {"error": "not found"})
            if len(p) == 3 and p[1] == "activities" and p[2].isdigit() and method == "PATCH":
                r = update_activity(int(p[2]), self._json())
                return self._send(200, r) if r else self._send(404, {"error": "not found"})
            if len(p) == 3 and p[1] == "activities" and p[2].isdigit() and method == "DELETE":
                delete_activity(int(p[2]))
                return self._send(200, {"ok": True})
            if p == ["api", "tasks"] and method == "GET":
                return self._send(200, list_open_tasks())
            if len(p) == 3 and p[1] == "tasks" and p[2].isdigit():
                if method == "PATCH":
                    r = update_task(int(p[2]), self._json())
                    return self._send(200, r) if r else self._send(404, {"error": "not found"})
                if method == "DELETE":
                    delete_task(int(p[2]))
                    return self._send(200, {"ok": True})
            if p == ["api", "stats"] and method == "GET":
                q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                today = datetime.now().date().isoformat()
                start = q.get("from") or first_activity_date()
                end = q.get("to") or today
                datetime.fromisoformat(start); datetime.fromisoformat(end)
                return self._send(200, get_stats(start, end))
            if p == ["api", "daily"] and method == "POST":
                return self._send(200, bump_daily(self._json()))
            if p == ["api", "order"] and method == "GET":
                return self._send(200, get_orders())
            if p == ["api", "order"] and method == "POST":
                body = self._json()
                return self._send(200, set_order(body.get("key"), body.get("ids")))
            if p == ["api", "settings"] and method == "POST":
                return self._send(200, save_settings(self._json()))
            if p == ["api", "sync"] and method == "GET":
                with db() as c:
                    n = c.execute("SELECT COUNT(*) FROM trellus_calls WHERE status='review'").fetchone()[0]
                return self._send(200, {**sync_state(trellus_sync, email=_ctx.viewing or _ctx.user), "review_count": n})
            if p == ["api", "sync"] and method == "POST":
                try:
                    return self._send(200, trellus_sync.sync_once(db, user_env(email=_ctx.user), _ctx.user))
                except RuntimeError as e:
                    return self._send(502, {"error": str(e)})
            if p == ["api", "email-sync"] and method == "GET":
                return self._send(200, sync_state(email_sync, email=_ctx.viewing or _ctx.user))
            if p == ["api", "email-sync"] and method == "POST":
                try:
                    return self._send(200, email_sync.sync_once(db, user_env(email=_ctx.user), _ctx.user))
                except RuntimeError as e:
                    return self._send(502, {"error": str(e)})
            if p == ["api", "review"] and method == "GET":
                with db() as c:
                    return self._send(200, trellus_sync.list_review(c))
            if len(p) == 3 and p[1] == "calls" and method == "GET":
                with db() as c:
                    if not c.execute("SELECT 1 FROM trellus_calls WHERE session_id=?", (p[2],)).fetchone():
                        return self._send(404, {"error": "call not found"})
                try:
                    return self._send(200, trellus_sync.call_detail(user_env(email=_ctx.viewing or _ctx.user), p[2]))
                except RuntimeError as e:
                    return self._send(502, {"error": str(e)})
            if len(p) == 3 and p[1] == "review" and method == "POST":
                body = self._json()
                action, aid = body.get("action"), body.get("account_id")
                if action == "create":
                    with db() as c:
                        call = trellus_sync.get_review(c, p[2])
                    aid = create_account({
                        "agency": call["company"] or call["name"] or trellus_sync.pretty_phone(call["phone"]),
                        "contact": call["name"] if call["name"] != call["company"] else "",
                        "phone": trellus_sync.pretty_phone(call["phone"]),
                        "stage": "lead", "next_follow_up": body.get("next_follow_up", ""),
                    })["id"]
                    action = "link"
                with db() as c:
                    trellus_sync.resolve_review(c, p[2], action, aid)
                return self._send(200, {"ok": True, "account_id": aid})
            if p == ["api", "export.csv"] and method == "GET":
                fname = f"pipeline-{datetime.now():%Y-%m-%d}.csv"
                return self._send(200, export_csv(), "text/csv",
                                  {"Content-Disposition": f'attachment; filename="{fname}"'})
            return self._send(404, {"error": "not found"})
        except ValueError as e:
            return self._send(400, {"error": str(e)})

    def _static(self, method):
        if method != "GET":
            return self._send(405, {"error": "method not allowed"})
        rel = urlparse(self.path).path.lstrip("/") or "index.html"
        target = (STATIC / rel).resolve()
        if STATIC not in target.parents or not target.is_file():
            return self._send(404, "Not found", "text/plain")
        types = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css"}
        return self._send(200, target.read_bytes(), types.get(target.suffix, "application/octet-stream"))

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PATCH(self):
        self._route("PATCH")

    def do_DELETE(self):
        self._route("DELETE")


def import_db(src, email):
    """Copy an existing pipeline.db into someone's account on the shared server (replaces what's there)."""
    email = email.strip().lower()
    if not EMAIL_RE.fullmatch(email):
        sys.exit(f"'{email}' isn't an email address")
    dest = user_db_path(email, create=True)
    with sqlite3.connect(f"file:{Path(src).resolve()}?mode=ro", uri=True) as a, sqlite3.connect(dest) as b:
        a.backup(b)
    _ready.discard(str(dest))
    ensure_db(dest)  # adds any newer tables
    with db(dest) as c:  # keys and sync bookmarks stay with the old copy; they enter their keys again in Settings
        c.execute("""DELETE FROM settings WHERE key IN ('trellus_api_key', 'google_app_password', 'trellus_user_id',
                     'email_last_uid', 'email_uidvalidity')""")
        n = c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    print(f"Imported {n} agencies into {email}'s tracker ({dest.name}).")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "import":
        if not HOSTED:
            sys.exit("Run the import on the shared server (AUTH_MODE=header), e.g.\n"
                     "  AUTH_MODE=header DATA_DIR=/data python3 server.py import pipeline.db you@company.com")
        import_db(sys.argv[2], sys.argv[3])
        sys.exit(0)
    if HOSTED:
        if len(PROXY_SECRET) < 16:
            sys.exit("AUTH_MODE=header needs PROXY_SECRET (16+ random characters) so only the platform can reach the "
                     "tracker. See DEPLOY.md.")
        registry().close()
        where = f"Data folder: {DATA_DIR}  (one database per person)\nManagers: {', '.join(sorted(ADMINS)) or 'none'}"
    else:
        ensure_db(DB_PATH)
        where = f"Data file: {DB_PATH}"
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"Pipeline Tracker running at {url}  ({'hosted, sign-in via platform' if HOSTED else 'local'}; Ctrl+C to stop)\n{where}", flush=True)
    if not HOSTED:
        env = user_env(DB_PATH)
        print("Trellus sync " + ("on (every 5 min)" if env.get("TRELLUS_API_KEY") else "off (add keys in Settings)"), flush=True)
        print("Email sync " + ("on (every 5 min)" if all(email_sync.config(env)) else "off (add keys in Settings)"), flush=True)
    threading.Thread(target=scheduler, daemon=True, name="sync").start()
    if not HOSTED and "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
