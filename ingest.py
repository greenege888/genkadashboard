#!/usr/bin/env python3
"""
Genka Deals - email ingestion helpers for the daily Claude Code routine.

Python 3.9+, standard library only. No pip installs needed.

Environment variables (put them in the routine's .env or shell profile):
  GENKA_SUPABASE_URL          https://xxxx.supabase.co
  GENKA_SUPABASE_SERVICE_KEY  service_role key (Settings > API). Server-side only.
  GENKA_IMAP_EMAIL            the dedicated inbox address
  GENKA_IMAP_APP_PASSWORD     Gmail app password (requires 2FA on that account)
  GENKA_IMAP_HOST             default imap.gmail.com
  GENKA_IMAP_FOLDER           default INBOX
  GENKA_LOOKBACK_DAYS         default 14

Commands:
  python3 ingest.py fetch
      Pull recent messages not yet in ingest_log into ./inbox_work/<key>/
      (meta.json, body.txt, attachments/). Prints NEW <dir> per item.
  python3 ingest.py projects
      Print current projects and deals as JSON (for matching).
  python3 ingest.py docs <project_id>
      List a project's existing documents (name, size, category) for dedup.
  python3 ingest.py upload <local_file> <project_id> <category>
      Upload a file to the project's data room and insert its documents row.
  python3 ingest.py insert <table> '<json>'         (or @file.json)
  python3 ingest.py update <table> <id> '<json>'    (or @file.json)
  python3 ingest.py review <workdir>
      Read <workdir>/payload.json (written by the routine), upload the
      item's attachments under inbox/<key>/, insert an inbox_items row.
  python3 ingest.py done <workdir> <filed|review|skipped>
      Write ingest_log and mark the email as read on the server.
"""

import os, sys, json, re, time, hashlib, imaplib, email, mimetypes
import datetime as dt
import urllib.request, urllib.error
from email.header import decode_header, make_header

ROOT = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(ROOT, "inbox_work")

SB_URL = os.environ.get("GENKA_SUPABASE_URL", "").rstrip("/")
SB_KEY = os.environ.get("GENKA_SUPABASE_SERVICE_KEY", "")
IMAP_HOST = os.environ.get("GENKA_IMAP_HOST", "imap.gmail.com")
IMAP_USER = os.environ.get("GENKA_IMAP_EMAIL", "")
IMAP_PASS = os.environ.get("GENKA_IMAP_APP_PASSWORD", "")
IMAP_FOLDER = os.environ.get("GENKA_IMAP_FOLDER", "INBOX")
LOOKBACK_DAYS = int(os.environ.get("GENKA_LOOKBACK_DAYS", "14"))


def die(msg):
    print("ERROR: " + msg)
    sys.exit(1)


# ----------------------------- supabase -----------------------------

def sb_req(method, path, body=None, headers=None, raw=False):
    if not SB_URL or not SB_KEY:
        die("Set GENKA_SUPABASE_URL and GENKA_SUPABASE_SERVICE_KEY")
    h = {"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY}
    if not raw:
        h["Content-Type"] = "application/json"
    if headers:
        h.update(headers)
    data = body if raw else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(SB_URL + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            t = r.read().decode("utf-8", "replace")
            return json.loads(t) if t.strip() else None
    except urllib.error.HTTPError as e:
        die("%s %s -> %s: %s" % (method, path, e.code, e.read().decode("utf-8", "replace")[:300]))


def rest_select(q):
    return sb_req("GET", "/rest/v1/" + q)


def rest_insert(table, row, on_conflict=None):
    path = "/rest/v1/" + table
    prefer = "return=representation"
    if on_conflict:
        path += "?on_conflict=" + on_conflict
        prefer = "resolution=merge-duplicates,return=representation"
    return sb_req("POST", path, row, {"Prefer": prefer})


def rest_update(table, id_, patch):
    return sb_req("PATCH", "/rest/v1/%s?id=eq.%s" % (table, id_), patch,
                  {"Prefer": "return=representation"})


def storage_upload(dest, local):
    ctype = mimetypes.guess_type(local)[0] or "application/octet-stream"
    with open(local, "rb") as f:
        data = f.read()
    sb_req("POST", "/storage/v1/object/documents/" + dest, data,
           {"Content-Type": ctype}, raw=True)
    return dest


# ------------------------------- imap -------------------------------

def imap_connect():
    if not IMAP_USER or not IMAP_PASS:
        die("Set GENKA_IMAP_EMAIL and GENKA_IMAP_APP_PASSWORD")
    m = imaplib.IMAP4_SSL(IMAP_HOST)
    m.login(IMAP_USER, IMAP_PASS)
    m.select(IMAP_FOLDER)
    return m


def dec(s):
    try:
        return str(make_header(decode_header(s or "")))
    except Exception:
        return s or ""


def msg_key(mid, uid):
    base = (mid or "").strip() or ("uid-" + str(uid))
    return hashlib.sha1(base.encode()).hexdigest()[:16]


def body_text(msg):
    plain, html = "", ""
    for part in msg.walk():
        cd = str(part.get("Content-Disposition") or "")
        if "attachment" in cd:
            continue
        ct = part.get_content_type()
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            payload = None
        if payload is None:
            continue
        cs = part.get_content_charset() or "utf-8"
        try:
            txt = payload.decode(cs, "replace")
        except Exception:
            txt = payload.decode("utf-8", "replace")
        if ct == "text/plain":
            plain += txt + "\n"
        elif ct == "text/html":
            html += txt + "\n"
    if plain.strip():
        return plain.strip()
    t = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", html, flags=re.I)
    t = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]{2,}", " ", t)).strip()


def save_attachments(msg, adir):
    names = []
    for part in msg.walk():
        fn = part.get_filename()
        if not fn:
            continue
        cd = str(part.get("Content-Disposition") or "")
        if "attachment" not in cd and part.get_content_maintype() == "text":
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        os.makedirs(adir, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", dec(fn))[:120] or "attachment"
        p = os.path.join(adir, safe)
        i = 1
        while os.path.exists(p):
            p = os.path.join(adir, str(i) + "_" + safe)
            i += 1
        with open(p, "wb") as f:
            f.write(payload)
        names.append(os.path.basename(p))
    return names


# ------------------------------ commands ------------------------------

def cmd_fetch():
    os.makedirs(WORK, exist_ok=True)
    done = {r["message_id"] for r in (rest_select("ingest_log?select=message_id") or [])}
    m = imap_connect()
    since = (dt.date.today() - dt.timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")
    typ, data = m.search(None, '(SINCE "%s")' % since)
    uids = data[0].split() if data and data[0] else []
    new = 0
    for uid in uids:
        typ, md = m.fetch(uid, "(RFC822)")
        if typ != "OK" or not md or not md[0]:
            continue
        msg = email.message_from_bytes(md[0][1])
        mid = (msg.get("Message-ID") or "").strip()
        key = msg_key(mid, uid.decode() if isinstance(uid, bytes) else uid)
        if mid in done:
            continue
        wdir = os.path.join(WORK, key)
        if os.path.exists(os.path.join(wdir, "meta.json")):
            continue
        os.makedirs(wdir, exist_ok=True)
        atts = save_attachments(msg, os.path.join(wdir, "attachments"))
        meta = {
            "key": key,
            "message_id": mid or ("uid-" + key),
            "uid": uid.decode() if isinstance(uid, bytes) else str(uid),
            "from": dec(msg.get("From")),
            "to": dec(msg.get("To")),
            "date": dec(msg.get("Date")),
            "subject": dec(msg.get("Subject")),
            "attachments": atts,
        }
        with open(os.path.join(wdir, "meta.json"), "w") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
        with open(os.path.join(wdir, "body.txt"), "w", encoding="utf-8") as f:
            f.write(body_text(msg)[:60000])
        print("NEW " + wdir)
        new += 1
    m.logout()
    print("fetched %d new item(s)" % new)


def cmd_projects():
    projects = rest_select("projects?select=id,name,country,technology,capacity_mwp,development_status&order=name.asc") or []
    deals = rest_select("deals?select=id,project_id,deal_type,stage,status,next_action,next_action_due&order=created_at.desc") or []
    print(json.dumps({"projects": projects, "deals": deals}, indent=2, ensure_ascii=False))


def cmd_docs(project_id):
    rows = rest_select("documents?select=id,category,file_name,file_size,created_at&project_id=eq.%s&order=created_at.desc" % project_id) or []
    print(json.dumps(rows, indent=2, ensure_ascii=False))


def _read_json_arg(arg):
    if arg.startswith("@"):
        with open(arg[1:]) as f:
            return json.load(f)
    return json.loads(arg)


def cmd_upload(local, project_id, category):
    if not os.path.exists(local):
        die("no such file: " + local)
    name = os.path.basename(local)
    dest = "%s/%d_%s" % (project_id, int(time.time()), re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120])
    storage_upload(dest, local)
    row = rest_insert("documents", {
        "project_id": project_id, "category": category, "file_name": name,
        "storage_path": dest, "file_size": os.path.getsize(local),
    })
    print(json.dumps(row, indent=2, ensure_ascii=False))


def cmd_insert(table, arg):
    print(json.dumps(rest_insert(table, _read_json_arg(arg)), indent=2, ensure_ascii=False))


def cmd_update(table, id_, arg):
    print(json.dumps(rest_update(table, id_, _read_json_arg(arg)), indent=2, ensure_ascii=False))


def cmd_review(wdir):
    with open(os.path.join(wdir, "meta.json")) as f:
        meta = json.load(f)
    pf = os.path.join(wdir, "payload.json")
    if not os.path.exists(pf):
        die("write %s first (summary, suggested_type, suggested_project_id, payload)" % pf)
    with open(pf) as f:
        spec = json.load(f)
    payload = spec.get("payload") or {}
    docs = payload.get("docs") or []
    cats = spec.get("doc_categories") or {}
    adir = os.path.join(wdir, "attachments")
    if os.path.isdir(adir):
        for name in sorted(os.listdir(adir)):
            dest = "inbox/%s/%s" % (meta["key"], name)
            storage_upload(dest, os.path.join(adir, name))
            docs.append({
                "file_name": name, "storage_path": dest,
                "file_size": os.path.getsize(os.path.join(adir, name)),
                "category": cats.get(name, "other"),
            })
    payload["docs"] = docs
    row = rest_insert("inbox_items", {
        "message_id": meta["message_id"],
        "received_at": _iso_date(meta.get("date")),
        "from_addr": meta.get("from"),
        "subject": meta.get("subject"),
        "summary": spec.get("summary"),
        "suggested_project_id": spec.get("suggested_project_id"),
        "suggested_type": spec.get("suggested_type") or "activity",
        "payload": payload,
        "status": "pending",
    })
    print(json.dumps(row, indent=2, ensure_ascii=False))


def _iso_date(raw):
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(raw).isoformat()
    except Exception:
        return None


def cmd_done(wdir, outcome):
    with open(os.path.join(wdir, "meta.json")) as f:
        meta = json.load(f)
    rest_insert("ingest_log", {
        "message_id": meta["message_id"], "subject": meta.get("subject"), "outcome": outcome,
    }, on_conflict="message_id")
    try:
        m = imap_connect()
        m.store(meta["uid"].encode(), "+FLAGS", "\\Seen")
        m.logout()
    except Exception as e:
        print("note: could not mark read on server (%s)" % e)
    print("done %s -> %s" % (meta["key"], outcome))


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    c = args[0]
    if c == "fetch":
        cmd_fetch()
    elif c == "projects":
        cmd_projects()
    elif c == "docs" and len(args) == 2:
        cmd_docs(args[1])
    elif c == "upload" and len(args) == 4:
        cmd_upload(args[1], args[2], args[3])
    elif c == "insert" and len(args) == 3:
        cmd_insert(args[1], args[2])
    elif c == "update" and len(args) == 4:
        cmd_update(args[1], args[2], args[3])
    elif c == "review" and len(args) == 2:
        cmd_review(args[1])
    elif c == "done" and len(args) == 3:
        cmd_done(args[1], args[2])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
