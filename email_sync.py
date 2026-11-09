"""Work email → Pipeline Tracker sync (read-only against your mailbox).

Every few minutes this reads new mail in your Gmail / Google Workspace mailbox over IMAP and:
  * logs every email to or from a tracked agency on its card (counts as a touch)
  * moves cards forward when the email shows the next step happened:
      Follow Up      → Info Sent       you emailed the agency
      Follow Up/Info → Submission In   they emailed you a file (ACORD, application, loss runs…)
      Submission In  → Quoted          you emailed them a file, or wrote "quote attached" / "here's the quote"
Cards only ever move forward, and only on emails newer than their last stage change, so dragging a
card back by hand sticks. Appointed and Lost are never set automatically.

The mailbox is opened read-only (EXAMINE + BODY.PEEK): nothing is sent, moved, deleted or marked read.
Config comes from .env: EMAIL_ADDRESS (defaults to TRELLUS_USER_EMAIL) and EMAIL_APP_PASSWORD
(GOOGLE_APP_PASSWORD works too).
"""
import email
import email.utils
import imaplib
import re
import threading
import time
from datetime import datetime, timedelta
from email.header import decode_header, make_header

HOST = "imap.gmail.com"
INTERVAL = 5 * 60      # seconds between syncs
BACKFILL_DAYS = 14     # how far back the first sync looks

FREEMAIL = {"gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com", "msn.com",
            "aol.com", "icloud.com", "me.com", "mac.com", "comcast.net", "att.net", "sbcglobal.net",
            "verizon.net", "bellsouth.net", "cox.net", "charter.net", "protonmail.com", "proton.me", "ymail.com",
            # shared by many separate agents, so only an exact address match counts
            "farmersagent.com", "farmersinsurance.com", "statefarm.com", "allstate.com", "nationwide.com",
            "amfam.com", "libertymutual.com", "goosehead.com", "weinsuregroup.com", "brightway.com",
            "corgi.insure"}
SKIP_ATTACH = re.compile(r"\.(png|jpe?g|gif|bmp|webp|ics|vcf|p7s|asc)$", re.I)  # signature logos, invites…
QUOTE_SENT = re.compile(
    r"(quote|proposal|indication)s?\b[^.\n]{0,40}\b(attached|enclosed|below|here|ready|link)"
    r"|\b(attached|enclosed|here'?s|here is|here are)\b[^.\n]{0,40}\b(quote|proposal|indication)", re.I)

# Files you send everyone. Sending these is "info", not a quote.
COLLATERAL = re.compile(r"appetite|guide|brochure|flyer|overview|one.?pager|deck|w-?9|licen[cs]e|agreement|contract|"
                        r"application|acord|signature|logo|newsletter", re.I)

# One status + lock per user, so a team's syncs never mix. "local" is the single-user Mac setup.
_locks, _statuses = {}, {}


def status_for(user="local"):
    return _statuses.setdefault(user, {"enabled": False, "last_sync": None, "last_error": None, "running": False})


status = status_for("local")  # kept for scripts that read it directly


# ---------- schema ----------
def init_tables(c):
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS emails (
            msg_id TEXT PRIMARY KEY,          -- Gmail X-GM-MSGID
            thread_id TEXT DEFAULT '',
            sent_at TEXT NOT NULL,            -- local ISO time
            direction TEXT NOT NULL,          -- out | in
            sender TEXT DEFAULT '',
            recipients TEXT DEFAULT '',
            subject TEXT DEFAULT '',
            attachments TEXT DEFAULT '',
            account_id INTEGER,
            activity_id INTEGER,
            moved_to TEXT DEFAULT ''          -- stage this email moved the card to, if any
        );
        CREATE INDEX IF NOT EXISTS idx_email_account ON emails(account_id);
        """
    )


def config(env):
    addr = env.get("EMAIL_ADDRESS") or env.get("TRELLUS_USER_EMAIL") or ""
    pw = env.get("EMAIL_APP_PASSWORD") or env.get("GOOGLE_APP_PASSWORD") or ""
    return addr.strip().lower(), re.sub(r"\s", "", pw)


# ---------- parsing ----------
def _dec(v):
    try:
        return str(make_header(decode_header(v or ""))).strip()
    except Exception:
        return str(v or "").strip()


def _addrs(*headers):
    return [a.lower() for _, a in email.utils.getaddresses([h for h in headers if h]) if "@" in a]


def _domain(addr):
    return addr.rsplit("@", 1)[-1].lower() if "@" in addr else ""


def _site_domain(url):
    d = re.sub(r"^(https?://)?(www\.)?", "", str(url or "").strip().lower())
    return d.split("/")[0].split("?")[0]


def new_text(msg):
    """The text you or they actually wrote, without the quoted earlier messages."""
    body = ""
    for part in msg.walk():
        if part.get_content_type() == "text/plain" and not part.get_filename():
            try:
                body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
            except Exception:
                body = ""
            break
    if not body:
        for part in msg.walk():
            if part.get_content_type() == "text/html" and not part.get_filename():
                html = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                html = re.split(r'<div class="gmail_quote|<blockquote', html, 1)[0]
                body = re.sub(r"<br\s*/?>|</p>|</div>", "\n", html, flags=re.I)
                body = re.sub(r"<[^>]+>", "", body).replace("&nbsp;", " ").replace("&amp;", "&")
                break
    out = []
    for line in body.splitlines():
        s = line.strip()
        if s.startswith(">") or re.match(r"^(On .{5,120} wrote:|-+ ?Original Message ?-+|From: .+)$", s):
            break
        out.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def attachments(msg):
    names = []
    for part in msg.walk():
        fn = part.get_filename()
        if fn and part.get_content_maintype() not in ("multipart", "image"):
            fn = _dec(fn)
            if not SKIP_ATTACH.search(fn):
                names.append(fn)
    return names


# ---------- matching ----------
def account_index(c):
    """Exact address → account ids, and company domain → account ids (from email and website)."""
    by_addr, by_dom = {}, {}
    for a in c.execute("SELECT id, email, website FROM accounts"):
        for addr in _addrs(a["email"].replace(";", ",")):
            by_addr.setdefault(addr, set()).add(a["id"])
            if _domain(addr) not in FREEMAIL:
                by_dom.setdefault(_domain(addr), set()).add(a["id"])
        d = _site_domain(a["website"])
        if d and "." in d and d not in FREEMAIL:
            by_dom.setdefault(d, set()).add(a["id"])
    return by_addr, by_dom


def match_account(others, by_addr, by_dom):
    """One account id the email is clearly about, or None (no match, or several agencies)."""
    hits = set()
    for a in others:
        hits |= by_addr.get(a, set())
    if not hits:
        for a in others:
            hits |= by_dom.get(_domain(a), set())
    return next(iter(hits)) if len(hits) == 1 else None


# ---------- applying ----------
def decide_stage(stage, direction, files, text, subject):
    if direction == "out" and stage == "lead":
        return "info_sent"
    if direction == "in" and stage in ("lead", "info_sent") and files:
        return "submission"
    quote_files = [f for f in files if not COLLATERAL.search(f)]
    if direction == "out" and stage == "submission" and (quote_files or QUOTE_SENT.search(text) or QUOTE_SENT.search(subject)):
        return "quoted"
    return None


def _biz_days_after(day, n):
    d, k = datetime.fromisoformat(day), 0
    while k < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            k += 1
    return d.date().isoformat()


def activity_body(direction, who, subject, files, text, moved_to):
    labels = {"info_sent": "Info Sent", "submission": "Submission In", "quoted": "Quoted"}
    head = "Email sent" if direction == "out" else f"Email from {who}"
    lines = [f"✉️ {head} · {subject or '(no subject)'}"]
    if files:
        lines.append("📎 " + ", ".join(files))
    snippet = re.sub(r"\s+", " ", text)[:280]
    if snippet:
        lines.append(snippet + ("…" if len(text) > 280 else ""))
    if moved_to:
        lines.append(f"→ moved to {labels[moved_to]} automatically")
    return "\n".join(lines)


def apply_email(c, rec, msg, account_id):
    acc = c.execute("SELECT stage, stage_changed_at, next_follow_up FROM accounts WHERE id = ?", (account_id,)).fetchone()
    files, text = attachments(msg), new_text(msg)
    moved = None
    if rec["sent_at"] > acc["stage_changed_at"]:
        moved = decide_stage(acc["stage"], rec["direction"], files, text, rec["subject"])
    who = _dec(email.utils.parseaddr(msg.get("From", ""))[0]) or rec["sender"]
    ts = rec["sent_at"]
    if moved:
        c.execute("INSERT INTO activities (account_id, kind, body, created_at, to_stage) VALUES (?,?,?,?,?)",
                  (account_id, "stage", f"{acc['stage']} → {moved}", ts, moved))
        c.execute("UPDATE accounts SET stage = ?, stage_changed_at = ? WHERE id = ?", (moved, ts, account_id))
        if moved == "info_sent" and not acc["next_follow_up"]:
            # Same as moving the card by hand: nudge on the thread after 4 business days.
            c.execute("UPDATE accounts SET next_follow_up = ? WHERE id = ?",
                      (_biz_days_after(rec["sent_at"][:10], 4), account_id))
    cur = c.execute("INSERT INTO activities (account_id, kind, body, created_at) VALUES (?,?,?,?)",
                    (account_id, "email", activity_body(rec["direction"], who, rec["subject"], files, text, moved), ts))
    c.execute("""UPDATE accounts SET last_touch_at = MAX(COALESCE(last_touch_at, ''), ?),
                 updated_at = MAX(updated_at, ?) WHERE id = ?""", (ts, ts, account_id))
    c.execute("""INSERT OR REPLACE INTO emails (msg_id, thread_id, sent_at, direction, sender, recipients, subject,
                 attachments, account_id, activity_id, moved_to) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
              (rec["msg_id"], rec["thread_id"], ts, rec["direction"], rec["sender"], rec["recipients"],
               rec["subject"], ", ".join(files), account_id, cur.lastrowid, moved or ""))
    return moved


# ---------- IMAP ----------
def _all_mail(M):
    typ, boxes = M.list()
    for b in boxes or []:
        line = b.decode(errors="replace")
        if "\\All" in line:
            return line.rsplit(' "/" ', 1)[-1].strip()
    return '"[Gmail]/All Mail"'


def _fetch(M, uids, what):
    """{uid: (gm_msgid, gm_thrid, raw bytes)} for the given UIDs."""
    out = {}
    for i in range(0, len(uids), 200):
        chunk = b",".join(uids[i:i + 200])
        typ, data = M.uid("FETCH", chunk, f"(UID X-GM-MSGID X-GM-THRID {what})")
        for item in data or []:
            if not isinstance(item, tuple):
                continue
            meta = item[0].decode(errors="replace")
            uid = re.search(r"UID (\d+)", meta)
            if uid:
                out[int(uid.group(1))] = (re.search(r"X-GM-MSGID (\d+)", meta).group(1),
                                          re.search(r"X-GM-THRID (\d+)", meta).group(1), item[1])
    return out


def sync_once(db, env, user="local"):
    me, pw = config(env)
    if not me or not pw:
        raise RuntimeError("Add your work email and Google app password in Settings")
    status, lock = status_for(user), _locks.setdefault(user, threading.Lock())
    if not lock.acquire(blocking=False):
        return {"skipped": "a sync is already running"}
    status["running"] = True
    try:
        M = imaplib.IMAP4_SSL(HOST, timeout=60)
        try:
            try:
                M.login(me, pw)
            except imaplib.IMAP4.error as e:
                raise RuntimeError(f"Email login failed for {me}: {e}. Check the Google app password in Settings.")
            M.select(_all_mail(M), readonly=True)
            validity = M.response("UIDVALIDITY")[1][0].decode()
            with db() as c:
                st = {r["key"]: r["value"] for r in c.execute(
                    "SELECT key, value FROM settings WHERE key IN ('email_uidvalidity', 'email_last_uid')")}
            if st.get("email_uidvalidity") == validity and st.get("email_last_uid"):
                typ, d = M.uid("SEARCH", None, f"UID {int(st['email_last_uid']) + 1}:*")
            else:
                since = (datetime.now() - timedelta(days=BACKFILL_DAYS)).strftime("%d-%b-%Y")
                typ, d = M.uid("SEARCH", None, f"SINCE {since}")
            last = int(st.get("email_last_uid") or 0) if st.get("email_uidvalidity") == validity else 0
            uids = [u for u in (d[0] or b"").split() if int(u) > last]

            # Headers first, then full messages only for mail involving a tracked agency.
            heads = _fetch(M, uids, "BODY.PEEK[HEADER.FIELDS (FROM TO CC DATE SUBJECT)]")
            with db() as c:
                by_addr, by_dom = account_index(c)
                seen = {r[0] for r in c.execute("SELECT msg_id FROM emails")}
            todo = []
            for uid, (gid, thr, raw) in heads.items():
                h = email.message_from_bytes(raw)
                frm = (_addrs(h.get("From")) or [""])[0]
                rcpts = _addrs(h.get("To"), h.get("Cc"))
                direction = "out" if frm == me else "in"
                others = [a for a in (rcpts if direction == "out" else [frm]) if a != me]
                aid = match_account(others, by_addr, by_dom) if gid not in seen else None
                if not aid:
                    continue
                try:
                    when = email.utils.parsedate_to_datetime(h.get("Date")).astimezone().replace(tzinfo=None)
                except Exception:
                    when = datetime.now()
                todo.append((when, uid, {"msg_id": gid, "thread_id": thr, "direction": direction, "sender": frm,
                                         "recipients": ", ".join(rcpts), "subject": _dec(h.get("Subject")),
                                         "sent_at": when.isoformat(timespec="seconds")}, aid))

            bodies = _fetch(M, [str(u).encode() for _, u, _, _ in todo], "BODY.PEEK[]") if todo else {}
            logged, moved = 0, []
            with db() as c:
                for _, uid, rec, aid in sorted(todo, key=lambda t: t[0]):  # oldest first, so stages step in order
                    if uid not in bodies:
                        continue
                    m = apply_email(c, rec, email.message_from_bytes(bodies[uid][2]), aid)
                    logged += 1
                    if m:
                        moved.append(m)
                top = max([int(u) for u in uids] + [last])
                c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('email_uidvalidity', ?)", (validity,))
                c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('email_last_uid', ?)", (str(top),))
        finally:
            try:
                M.logout()
            except Exception:
                pass
        status.update(last_sync=datetime.now().isoformat(timespec="seconds"), last_error=None)
        return {"scanned": len(uids), "logged": logged, "moved": len(moved)}
    except Exception as e:
        status["last_error"] = str(e)
        raise
    finally:
        status["running"] = False
        lock.release()


