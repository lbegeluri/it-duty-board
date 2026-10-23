"""IT Duty Board: internal site showing who is on IT support duty today.

  /            public page (employees, internal network only)
  /admin/...   admin panel (local accounts: owner + admins)

Security choices, in short:
  * passwords: argon2id hashes, min 7 chars, upper+lower+digit+symbol, temporary passwords force a change at next login
  * 5 wrong passwords lock an account for 15 minutes; per-IP throttle on top of nginx rate limits
  * session cookie: HttpOnly + SameSite=Strict + Secure, 8h lifetime; every POST needs a CSRF token
  * strict Content-Security-Policy (no inline scripts, no external resources: works on an offline LAN)
  * every change is written to the append-only audit table
"""
import calendar
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo

import click
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from flask import (Flask, Response, abort, g, has_request_context, redirect, render_template, request,
                   send_file, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix

from . import db, xlsx_io
from . import holidays as hol_mod

TZ = ZoneInfo(os.environ.get("TZ_NAME", "UTC"))
PW_MIN = 7                      # minimum length; complexity rules are in password_problem()
BRANCHES = ("site_a", "site_b")
BRANCH_NAME = {"site_a": "Site A", "site_b": "Site B"}
LOCK_AFTER = 5
LOCK_MINUTES = 15

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WEEKDAYS_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]

ph = PasswordHasher()                       # argon2id with the library's safe defaults
DUMMY_HASH = ph.hash("timing-equaliser")    # verified when a username does not exist (same cost)


# ---------------------------------------------------------------- app setup
def load_secret():
    """Signing key for session cookies: generated once, stored in the data volume (mode 600)."""
    os.makedirs(db.DATA_DIR, exist_ok=True)
    path = os.path.join(db.DATA_DIR, "secret.key")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    except FileExistsError:
        pass
    for _ in range(50):                     # another worker may still be writing it
        value = open(path).read().strip()
        if value:
            return value
        time.sleep(0.1)
    raise RuntimeError("secret.key is empty")


db.init()
app = Flask(__name__)
app.secret_key = load_secret()
app.config.update(
    SESSION_COOKIE_NAME="itduty",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "1") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    MAX_CONTENT_LENGTH=64 * 1024,
)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)   # trust the nginx proxy only


# ---------------------------------------------------------------- helpers
def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat(timespec="seconds")


def today():
    return datetime.now(TZ).date()


def get_db():
    if "db" not in g:
        g.db = db.connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def q(sql, *args):
    return get_db().execute(sql, args).fetchall()


def q1(sql, *args):
    return get_db().execute(sql, args).fetchone()


def run(sql, *args):
    conn = get_db()
    cur = conn.execute(sql, args)
    conn.commit()
    return cur


def audit(action, detail="", actor=None):
    who = actor or (g.user["username"] if g.get("user") else "-")
    ip = (request.remote_addr or "") if has_request_context() else ""
    run("INSERT INTO audit(ts, actor, action, detail, ip) VALUES (?, ?, ?, ?, ?)", iso(now_utc()), who, action, detail, ip)


def touch():
    run("INSERT INTO meta(key, value) VALUES ('last_updated', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", iso(now_utc()))


def fmt_date(d):
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def fmt_datetime(value):
    if not value:
        return "—"
    dt = datetime.fromisoformat(value).astimezone(TZ)
    return f"{fmt_date(dt.date())}, {dt:%H:%M}"


def initials(name):
    parts = [p for p in name.split() if p]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


def tel(phone):
    return re.sub(r"[^\d+]", "", phone or "")


def cell_label(name, all_names):
    """Short calendar label: first name + surname initial; the surname part grows only when
    another person has the same first name and the same start of surname."""
    parts = (name or "").split()
    if len(parts) < 2:
        return name or ""
    if list(all_names).count(name) > 1:
        return name
    first, surname = parts[0], " ".join(parts[1:])
    rivals = []
    for other in set(all_names):
        op = other.split()
        if other != name and len(op) >= 2 and op[0] == first:
            rivals.append(" ".join(op[1:]))
    n = 1
    while n < len(surname) and any(r[:n] == surname[:n] for r in rivals):
        n += 1
    return f"{first} {surname[:n]}" + ("." if n < len(surname) else "")


@app.template_filter("short")
def short_filter(name):
    names = g.get("_active_names")
    if names is None:
        names = g._active_names = [r["name"] for r in q("SELECT name FROM staff WHERE active = 1")]
    return cell_label(name, names)


_HOSTS = {"stamp": None, "data": {}}


def hostnames():
    """IP -> host name, from <data>/hostnames.json. The file is written by the VM's reverse-DNS helper
    (vm/40-hostnames.sh), because the container itself has no network and cannot ask DNS."""
    path = os.path.join(db.DATA_DIR, "hostnames.json")
    try:
        st = os.stat(path)
    except OSError:
        return {}
    stamp = (st.st_mtime_ns, st.st_size)
    if _HOSTS["stamp"] != stamp:
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            data = {str(k)[:64]: str(v)[:253] for k, v in raw.items()} if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            data = {}
        _HOSTS.update(stamp=stamp, data=data)
    return _HOSTS["data"]


def fmt_phone(phone):
    """Phone number as typed, with the white space tidied up."""
    return " ".join((phone or "").split())


def parse_day(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        abort(400)


def temp_password():
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no look-alike characters
    return "".join(secrets.choice(alphabet) for _ in range(16))


def password_problem(new, again, username, current=None):
    if new != again:
        return "The passwords do not match"
    if len(new) < PW_MIN:
        return f"The password must have at least {PW_MIN} characters"
    if not (re.search(r"[a-z]", new) and re.search(r"[A-Z]", new)
            and re.search(r"[0-9]", new) and re.search(r"[^A-Za-z0-9]", new)):
        return "The password must contain an upper-case letter, a lower-case letter, a digit and a symbol"
    if new.lower() == username.lower() or (current is not None and new == current):
        return "The new password must differ from the current one and from the user name"
    return None


# ---------------------------------------------------------------- sessions, CSRF, headers
def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


@app.context_processor
def inject():
    return {"csrf": csrf_token, "fmt_date": fmt_date, "fmt_datetime": fmt_datetime,
            "initials": initials, "tel": tel, "fmt_phone": fmt_phone, "host_for": lambda ip: hostnames().get(ip or "", ""), "user": g.get("user"),
            "weekdays": WEEKDAYS, "weekdays_short": WEEKDAYS_SHORT, "months": MONTHS, "branch_name": BRANCH_NAME,
            "my_scope": scope() if g.get("user") else ""}


@app.before_request
def csrf_guard():
    if request.method == "POST":
        sent = request.form.get("_csrf", "")
        if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
            abort(400)


@app.after_request
def headers(resp):
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Frame-Options"] = "DENY"
    if request.path.startswith("/admin") or request.path.startswith("/api"):
        resp.headers["Cache-Control"] = "no-store"
    elif request.path == "/":
        resp.headers["Cache-Control"] = "no-cache"
    return resp


def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    u = q1("SELECT * FROM users WHERE id = ? AND active = 1", uid)
    return u if u and u["sv"] == session.get("sv") else None      # sv changed = password was reset


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        g.user = current_user()
        if g.user is None:
            session.pop("uid", None)
            return redirect(url_for("login"))
        if g.user["must_change"] and request.endpoint not in ("settings", "logout"):
            return redirect(url_for("settings"))
        return view(*args, **kwargs)
    return wrapper


def owner_required(view):
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if g.user["role"] != "owner":
            abort(403)
        return view(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------- login throttling
FAILS = {}   # ip -> timestamps of recent failed logins (per worker; nginx adds its own limit)


def too_many(ip):
    now = time.time()
    FAILS[ip] = [t for t in FAILS.get(ip, []) if now - t < 600]
    return len(FAILS[ip]) >= 20


LOCK_WINDOW = timedelta(minutes=LOCK_MINUTES)       # failures older than this are forgotten


def lock_row(username, ip):
    return q1("SELECT * FROM login_locks WHERE username = ? AND ip = ?", username, ip)


def lock_active(row):
    return bool(row and row["locked_until"] and datetime.fromisoformat(row["locked_until"]) > now_utc())


def authenticate(username, password, ip):
    """Return the user row on success, None otherwise.
    Failures are counted per user name AND client address: five wrong passwords lock that name for that address
    only, so one person's typing (or an attacker) cannot lock everybody else out. Sets g.login_locked when the
    attempt comes from a locked (user, address) pair."""
    g.login_locked = False
    u = q1("SELECT * FROM users WHERE username = ?", username)
    row = lock_row(u["username"], ip) if u else None
    if lock_active(row):
        g.login_locked = True
        try:
            ph.verify(DUMMY_HASH, password)          # the same work as a real check, so timing reveals nothing
        except VerificationError:
            pass
        return None
    ok = False
    try:
        ph.verify(u["password_hash"] if (u and u["active"]) else DUMMY_HASH, password)
        ok = bool(u and u["active"])
    except (VerificationError, InvalidHashError):
        ok = False
    if ok:
        if ph.check_needs_rehash(u["password_hash"]):
            run("UPDATE users SET password_hash = ? WHERE id = ?", ph.hash(password), u["id"])
        run("DELETE FROM login_locks WHERE username = ? AND ip = ?", u["username"], ip)
        run("UPDATE users SET last_login = ? WHERE id = ?", iso(now_utc()), u["id"])
        return u
    if u:                                            # only real accounts get a row: random names cannot fill the table
        now = now_utc()
        recent = bool(row and row["last_fail"] and now - datetime.fromisoformat(row["last_fail"]) < LOCK_WINDOW)
        failed = row["failed"] + 1 if recent else 1
        if failed >= LOCK_AFTER:
            run("INSERT INTO login_locks(username, ip, failed, last_fail, locked_until) VALUES (?, ?, 0, ?, ?) "
                "ON CONFLICT(username, ip) DO UPDATE SET failed = 0, last_fail = excluded.last_fail, "
                "locked_until = excluded.locked_until",
                u["username"], ip, iso(now), iso(now + timedelta(minutes=LOCK_MINUTES)))
            audit("login.locked", f"{LOCK_AFTER} failed attempts from {ip}", actor=u["username"])
            g.login_locked = True
        else:
            run("INSERT INTO login_locks(username, ip, failed, last_fail, locked_until) VALUES (?, ?, ?, ?, NULL) "
                "ON CONFLICT(username, ip) DO UPDATE SET failed = excluded.failed, last_fail = excluded.last_fail, "
                "locked_until = NULL", u["username"], ip, failed, iso(now))
        run("DELETE FROM login_locks WHERE last_fail < ? AND (locked_until IS NULL OR locked_until < ?)",
            iso(now - timedelta(days=1)), iso(now))
    return None


# ---------------------------------------------------------------- holidays and duty slots
def holiday_map(start, end):
    """date -> name of every non-working holiday between two dates: the automatic list (Labour Code and the Orthodox
    Easter days), plus the owner's extra days, minus the automatic days the owner switched off."""
    out = {}
    for year in range(start.year, end.year + 1):
        out.update(hol_mod.auto_holidays(year))
    for r in q("SELECT day, kind, name FROM holiday_overrides"):
        d = date.fromisoformat(r["day"])
        if r["kind"] == "off":
            out.pop(d, None)
        elif r["kind"] == "add":
            out[d] = r["name"] or "Day off"
    return {d: n for d, n in out.items() if start <= d <= end}


def slots_between(start, end):
    """(day, branch) -> list of staff rows (one or more people) for every assigned slot between two dates."""
    out = {}
    for r in q("SELECT d.day, d.branch AS slot, s.* FROM duty d JOIN staff s ON s.id = d.staff_id WHERE d.day BETWEEN ? AND ? "
               "ORDER BY s.name COLLATE NOCASE", start.isoformat(), end.isoformat()):
        out.setdefault((r["day"], r["slot"]), []).append(r)
    return out


# ---------------------------------------------------------------- public pages
def week_groups(today, slots, hol):
    """Split the days after today into 'this week' (today..Sunday) and the full next week (Mon..Sun).
    slots: (day, branch) -> staff row; hol: date -> holiday name."""
    def mk(d):
        iso = d.isoformat()
        return {"date": d, "site_a": slots.get((iso, "site_a"), []), "site_b": slots.get((iso, "site_b"), []),
                "weekend": d.weekday() >= 5, "holiday": hol.get(d),
                "is_today": d == today, "is_tomorrow": d == today + timedelta(days=1)}
    sunday = today + timedelta(days=6 - today.weekday())
    this_week = [mk(today + timedelta(days=i)) for i in range((sunday - today).days + 1)]
    next_monday = sunday + timedelta(days=1)
    next_week = [mk(next_monday + timedelta(days=i)) for i in range(7)]
    return this_week, next_week


def meta_get(key, default=""):
    r = q1("SELECT value FROM meta WHERE key = ?", key)
    return r["value"] if r and r["value"] is not None else default


@app.route("/")
def public():
    t = today()
    end = t + timedelta(days=13)
    slots = slots_between(t, end)
    hol = holiday_map(t, end)
    this_week, next_week = week_groups(t, slots, hol)
    contact = {"phone": meta_get("contact_phone")}
    return render_template("public.html", today=t, site_a=slots.get((t.isoformat(), "site_a"), []),
                           site_b=slots.get((t.isoformat(), "site_b"), []), today_holiday=hol.get(t),
                           this_week=this_week, next_week=next_week, contact=contact,
                           site_title=meta_get("site_title", "IT Duty"),
                           site_footer=meta_get("site_footer") or "IT department",
                           last_updated=meta_get("last_updated") or None)


@app.route("/api/today")
def api_today():
    t = today()
    slots = slots_between(t, t)

    def pack(rows):
        return [{"name": r["name"], "phone": r["phone"]} for r in rows]
    return {"date": t.isoformat(), "on_duty": {b: pack(slots.get((t.isoformat(), b), [])) for b in BRANCHES}}


@app.route("/healthz")
def healthz():
    q1("SELECT 1")
    return "ok", 200, {"Content-Type": "text/plain"}


# ---------------------------------------------------------------- admin: login / logout
@app.route("/admin/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = request.remote_addr or "?"
        if too_many(ip):
            abort(429)
        username = request.form.get("username", "").strip()[:64]
        password = request.form.get("password", "")[:256]
        u = authenticate(username, password, ip)
        if u:
            session.clear()
            session["uid"] = u["id"]
            session["sv"] = u["sv"]
            session.permanent = True
            csrf_token()
            audit("login", actor=u["username"])
            run("UPDATE users SET last_ip = ? WHERE id = ?", ip, u["id"])
            return redirect(url_for("settings" if u["must_change"] else "schedule"))
        FAILS.setdefault(ip, []).append(time.time())
        audit("login.failed", username[:64] or "-", actor="-")
        if g.get("login_locked"):
            return render_template("login.html", error="Too many wrong attempts from this computer, so this user is locked here for 15 minutes. "
                                   "If you are sure of the password, try again later or ask the owner to unlock it."), 401
        return render_template("login.html", error="Wrong user name or password"), 401
    return render_template("login.html", error=None)


@app.route("/admin/logout", methods=["POST"])
@login_required
def logout():
    audit("logout")
    session.clear()
    return redirect(url_for("public"))


@app.route("/admin")
@login_required
def admin_home():
    return redirect(url_for("schedule"))


# ---------------------------------------------------------------- admin: settings (own password)
@app.route("/admin/settings", methods=["GET", "POST"])
@login_required
def settings():
    error = done = None
    if request.method == "POST":
        current = request.form.get("current", "")
        new = request.form.get("new", "")
        again = request.form.get("again", "")
        try:
            ph.verify(g.user["password_hash"], current)
            error = password_problem(new, again, g.user["username"], current)
        except (VerificationError, InvalidHashError):
            error = "The current password is wrong"
        if not error:
            run("UPDATE users SET password_hash = ?, must_change = 0, sv = sv + 1 WHERE id = ?", ph.hash(new), g.user["id"])
            session["sv"] = q1("SELECT sv FROM users WHERE id = ?", g.user["id"])["sv"]   # keep THIS session, end the others
            audit("password.change")
            g.user = current_user()
            done = "Password changed"
    return settings_page(error=error, done=done)


def meta_set(key, value):
    run("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", key, value)


def backup_folder():
    return os.path.join(db.DATA_DIR, "backups")


def backup_files():
    folder = backup_folder()
    return sorted(f for f in os.listdir(folder) if f.startswith("itduty-")) if os.path.isdir(folder) else []


def make_backup(keep=14):
    """Consistent copy of the database into <data>/backups (safe while the app is running)."""
    os.makedirs(backup_folder(), exist_ok=True)
    target = os.path.join(backup_folder(), f"itduty-{datetime.now(TZ):%Y%m%d-%H%M%S}.db")
    src, dst = sqlite3.connect(db.DB_PATH), sqlite3.connect(target)
    src.backup(dst)
    dst.close()
    src.close()
    for old in backup_files()[:-keep]:
        os.remove(os.path.join(backup_folder(), old))
    return target


def holiday_rows():
    """Holidays for this and next year as the owner sees them: automatic ones (with on/off state) and his own."""
    t = today()
    off = {r["day"] for r in q("SELECT day FROM holiday_overrides WHERE kind = 'off'")}
    auto = []
    for year in (t.year, t.year + 1):
        for d, name in sorted(hol_mod.auto_holidays(year).items()):
            if d >= t:
                auto.append({"day": d.isoformat(), "date": d, "name": name, "off": d.isoformat() in off})
    custom = [{"day": r["day"], "date": date.fromisoformat(r["day"]), "name": r["name"]}
              for r in q("SELECT day, name FROM holiday_overrides WHERE kind = 'add' ORDER BY day")]
    return auto, custom


@app.route("/admin/holidays/add", methods=["POST"])
@owner_required
def holidays_add():
    day = parse_day(request.form.get("day", ""))
    name = " ".join(request.form.get("name", "").split())[:60] or "Day off"
    run("INSERT INTO holiday_overrides(day, kind, name) VALUES (?, 'add', ?) "
        "ON CONFLICT(day) DO UPDATE SET kind = 'add', name = excluded.name", day.isoformat(), name)
    audit("holiday.add", f"{day.isoformat()} {name}")
    touch()
    return redirect(url_for("settings") + "#holidays")


@app.route("/admin/holidays/toggle", methods=["POST"])
@owner_required
def holidays_toggle():
    """Switch an automatic holiday off or back on."""
    day = parse_day(request.form.get("day", ""))
    if day not in hol_mod.auto_holidays(day.year):
        abort(400)
    if request.form.get("off") == "1":
        run("INSERT INTO holiday_overrides(day, kind, name) VALUES (?, 'off', '') "
            "ON CONFLICT(day) DO UPDATE SET kind = 'off', name = ''", day.isoformat())
        audit("holiday.off", day.isoformat())
    else:
        run("DELETE FROM holiday_overrides WHERE day = ? AND kind = 'off'", day.isoformat())
        audit("holiday.on", day.isoformat())
    touch()
    return redirect(url_for("settings") + "#holidays")


@app.route("/admin/holidays/delete", methods=["POST"])
@owner_required
def holidays_delete():
    day = parse_day(request.form.get("day", ""))
    run("DELETE FROM holiday_overrides WHERE day = ? AND kind = 'add'", day.isoformat())
    audit("holiday.delete", day.isoformat())
    touch()
    return redirect(url_for("settings") + "#holidays")


def settings_page(error=None, done=None, site_error=None, status=200, site=None):
    files = backup_files()
    last = None
    if files:
        path = os.path.join(backup_folder(), files[-1])
        last = {"name": files[-1], "time": datetime.fromtimestamp(os.path.getmtime(path), timezone.utc).isoformat(),
                "kb": max(1, os.path.getsize(path) // 1024), "count": len(files)}
    values = site or {"title": meta_get("site_title", "IT Duty"), "footer": meta_get("site_footer", "IT department"),
                      "contact_phone": meta_get("contact_phone")}
    info = {"admin_nets": os.environ.get("ADMIN_NETS", ""),
            "session_hours": int(app.config["PERMANENT_SESSION_LIFETIME"].total_seconds() // 3600)}
    auto_h, custom_h = holiday_rows()
    return render_template("settings.html", active="settings", error=error, done=done, site_error=site_error,
                           site=values, backup=last, info=info, auto_holidays=auto_h, custom_holidays=custom_h), status


@app.route("/admin/settings/site", methods=["POST"])
@owner_required
def settings_site():
    form = {k: " ".join(request.form.get(k, "").split()) for k in ("title", "footer", "contact_phone")}
    error = None
    if not form["title"] or len(form["title"]) > 80:
        error = "A title is required (80 characters at most)"
    elif len(form["footer"]) > 80:
        error = "Footer: 80 characters at most"
    elif form["contact_phone"] and not xlsx_io.PHONE_RE.fullmatch(form["contact_phone"]):
        error = "The phone number is not valid"
    if error:
        return settings_page(site_error=error, status=400, site=form)
    for key, meta_key in (("title", "site_title"), ("footer", "site_footer"), ("contact_phone", "contact_phone")):
        meta_set(meta_key, form[key])
    audit("settings.site", f"title: {form['title']}")
    touch()
    return settings_page(done="Site texts saved")


@app.route("/admin/backup", methods=["POST"])
@owner_required
def backup_create():
    name = os.path.basename(make_backup())
    audit("backup.create", name)
    return settings_page(done=f"Backup created: {name}")


@app.route("/admin/backup/download")
@owner_required
def backup_download():
    files = backup_files()
    path = os.path.join(backup_folder(), files[-1]) if files else make_backup()
    audit("backup.download", os.path.basename(path))
    return send_file(path, as_attachment=True, download_name=os.path.basename(path), mimetype="application/octet-stream")


# ---------------------------------------------------------------- admin: schedule
def month_days(anchor):
    first = anchor.replace(day=1)
    last = anchor.replace(day=calendar.monthrange(anchor.year, anchor.month)[1])
    start = first - timedelta(days=first.weekday())
    end = last + timedelta(days=6 - last.weekday())
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def week_days(anchor):
    start = anchor - timedelta(days=anchor.weekday())
    return [start + timedelta(days=i) for i in range(7)]


def back_to_schedule():
    return redirect(url_for("schedule", view=request.form.get("view", "month"), d=request.form.get("d", "")))


def active_staff():
    return q("SELECT * FROM staff WHERE active = 1 ORDER BY name COLLATE NOCASE")


def scope():
    """Which branch the signed-in user manages: '' = both (the owner and unrestricted administrators)."""
    u = g.get("user")
    if u is None or u["role"] == "owner":
        return ""
    return u["branch"] or ""


def allowed(branch):
    return scope() == "" or branch == scope()


def need(branch):
    if not allowed(branch):
        abort(403)


def person_access(person):
    """A branch administrator may touch only people of their branch (or people with no branch yet)."""
    if person["branch"]:
        need(person["branch"])


def person_for_slot(staff_id):
    """An active person who has a branch: only such a person can be put on duty (Site A or Site B slot of a day)."""
    person = q1("SELECT * FROM staff WHERE id = ? AND active = 1", staff_id)
    if person is None or person["branch"] not in BRANCHES:
        abort(400)
    need(person["branch"])
    return person


def add_slot(day, branch, staff_id):
    """Put one more person on duty in that day's branch slot (already there: nothing changes)."""
    run("INSERT OR IGNORE INTO duty(day, branch, staff_id) VALUES (?, ?, ?)", day.isoformat(), branch, staff_id)


def clear_slot(day, branch):
    run("DELETE FROM duty WHERE day = ? AND branch = ?", day.isoformat(), branch)


def form_branch():
    branch = request.form.get("branch", "")
    if branch not in BRANCHES:
        abort(400)
    return branch


@app.route("/admin/schedule")
@login_required
def schedule():
    view = "week" if request.args.get("view") == "week" else "month"
    anchor = parse_day(request.args["d"]) if request.args.get("d") else today()
    days = week_days(anchor) if view == "week" else month_days(anchor)
    slots = slots_between(days[0], days[-1])
    hol = holiday_map(days[0], days[-1])
    cells = [{"date": d, "iso": d.isoformat(), "in_month": d.month == anchor.month or view == "week",
              "today": d == today(), "weekend": d.weekday() >= 5, "holiday": hol.get(d),
              "site_a": slots.get((d.isoformat(), "site_a"), []), "site_b": slots.get((d.isoformat(), "site_b"), []),
              "legacy": slots.get((d.isoformat(), ""), [])} for d in days]
    if view == "month":
        first = anchor.replace(day=1)
        prev_d = (first - timedelta(days=1)).replace(day=1)
        next_d = (first + timedelta(days=32)).replace(day=1)
        title = f"{MONTHS[anchor.month - 1]} {anchor.year}"
        range_from = anchor.replace(day=1)
        range_to = anchor.replace(day=calendar.monthrange(anchor.year, anchor.month)[1])
    else:
        prev_d, next_d = anchor - timedelta(days=7), anchor + timedelta(days=7)
        title = f"{fmt_date(days[0])} – {fmt_date(days[-1])}"
        range_from, range_to = days[0], days[-1]
    in_range = [p["id"] for c in cells if c["in_month"] for k in ("site_a", "site_b", "legacy") for p in c[k]]
    counts = {sid: in_range.count(sid) for sid in set(in_range)}
    staff = active_staff()
    return render_template("schedule.html", active="schedule", view=view, anchor=anchor, cells=cells,
                           title=title, prev_d=prev_d, next_d=next_d, staff=staff, today=today(),
                           site_a_staff=[p for p in staff if p["branch"] == "site_a" and allowed("site_a")],
                           site_b_staff=[p for p in staff if p["branch"] == "site_b" and allowed("site_b")],
                           free_staff=[p for p in staff if p["branch"] not in BRANCHES],
                           can_site_a=allowed("site_a"), can_site_b=allowed("site_b"),
                           counts=counts, range_from=range_from, range_to=range_to)


@app.route("/admin/schedule/assign", methods=["POST"])
@login_required
def schedule_assign():
    """Change who is on duty in one day's branch slot (several people are possible):
    mode=set + staff_ids: exactly these people (the tick list); remove=1 + staff_id: take that person off;
    empty staff_id: empty the slot; otherwise: add the person to their own branch's slot."""
    day = parse_day(request.form.get("day"))
    sid = request.form.get("staff_id", "")
    branch = request.form.get("branch", "")
    if request.form.get("mode") == "set":
        if branch not in BRANCHES + ("",):
            abort(400)
        ids = list(dict.fromkeys(request.form.getlist("staff_ids")))
        people = []
        if branch == "":
            if ids:
                abort(400)
        else:
            need(branch)
            people = [person_for_slot(i) for i in ids]
            if any(p["branch"] != branch for p in people):
                abort(400)
        clear_slot(day, branch)
        for p in people:
            add_slot(day, branch, p["id"])
        audit("duty.set", f"{day.isoformat()} {BRANCH_NAME.get(branch, '—')}: {', '.join(p['name'] for p in people) or '—'}")
    elif request.form.get("remove") == "1":
        person = q1("SELECT * FROM staff WHERE id = ?", sid) or abort(400)
        if branch not in BRANCHES + ("",):
            abort(400)
        if branch:
            need(branch)
        run("DELETE FROM duty WHERE day = ? AND branch = ? AND staff_id = ?", day.isoformat(), branch, person["id"])
        audit("duty.remove", f"{day.isoformat()} {BRANCH_NAME.get(branch, '—')}: {person['name']}")
    elif sid == "":
        if branch not in BRANCHES + ("",):
            abort(400)
        if branch:
            need(branch)
        clear_slot(day, branch)
        audit("duty.clear", f"{day.isoformat()} {BRANCH_NAME.get(branch, '—')}")
    else:
        person = person_for_slot(sid)
        if branch and branch != person["branch"]:
            abort(400)                               # a person fills only the slot of their own branch
        add_slot(day, person["branch"], person["id"])
        audit("duty.assign", f"{day.isoformat()} → {person['name']} ({BRANCH_NAME[person['branch']]})")
    touch()
    return back_to_schedule()


@app.route("/admin/schedule/swap", methods=["POST"])
@login_required
def schedule_swap():
    """Swap the people of two days in one branch; if one day is empty the people simply move there."""
    a, b = parse_day(request.form.get("a")), parse_day(request.form.get("b"))
    branch = form_branch()
    need(branch)
    if a == b:
        abort(400)
    sql = "SELECT s.id, s.name FROM duty d JOIN staff s ON s.id = d.staff_id WHERE d.day = ? AND d.branch = ? ORDER BY s.name"
    pa, pb = q(sql, a.isoformat(), branch), q(sql, b.isoformat(), branch)
    if not pa and not pb:
        abort(400)
    for day, people in ((a, pb), (b, pa)):
        clear_slot(day, branch)
        for person in people:
            add_slot(day, branch, person["id"])
    names = lambda rows: ", ".join(p["name"] for p in rows) or "—"
    audit("duty.swap", f"{a.isoformat()} ({names(pa)}) ⇄ {b.isoformat()} ({names(pb)}), {BRANCH_NAME[branch]}")
    touch()
    return back_to_schedule()


@app.route("/admin/schedule/bulk", methods=["POST"])
@login_required
def schedule_bulk():
    """Put one person on several selected days (their branch's slot), or, with an empty staff_id, empty one branch's slot."""
    raw = [x for x in request.form.get("days", "").split(",") if x.strip()]
    days = sorted({parse_day(x.strip()) for x in raw})
    if not days or len(days) > 100:
        abort(400)
    sid = request.form.get("staff_id", "")
    if sid == "":
        branch = form_branch()
        need(branch)
        for d in days:
            clear_slot(d, branch)
        audit("duty.bulk", f"cleared {BRANCH_NAME[branch]} on {len(days)} days: {days[0].isoformat()} … {days[-1].isoformat()}")
    else:
        person = person_for_slot(sid)
        for d in days:
            add_slot(d, person["branch"], person["id"])
        audit("duty.bulk", f"{person['name']} ({BRANCH_NAME[person['branch']]}) → {len(days)} days: {days[0].isoformat()} … {days[-1].isoformat()}")
    touch()
    return back_to_schedule()


@app.route("/admin/schedule/import", methods=["POST"])
@login_required
def schedule_import():
    """Two steps: 'preview' shows what would change (writes nothing), 'confirm' writes the good rows."""
    text = request.form.get("text", "")
    if len(text) > 100_000:
        abort(400)
    staff = [{"id": s["id"], "name": s["name"], "branch": s["branch"]} for s in active_staff()
             if allowed(s["branch"]) or s["branch"] == ""]
    try:
        parsed = xlsx_io.parse_paste(text, staff)
    except xlsx_io.TooManyRows:
        abort(400)
    info = {s["id"]: s for s in staff}
    if request.form.get("step") == "confirm":
        for day, sid in parsed["ok"]:
            add_slot(day, info[sid]["branch"], sid)
        if parsed["ok"]:
            audit("duty.import", f"{len(parsed['ok'])} days imported, {len(parsed['bad'])} lines skipped "
                                 f"({parsed['ok'][0][0].isoformat()} … {parsed['ok'][-1][0].isoformat()})")
            touch()
        return redirect(url_for("schedule", view="month", d=parsed["ok"][0][0].isoformat() if parsed["ok"] else ""))
    current = {}
    if parsed["ok"]:
        lo, hi = min(d for d, _ in parsed["ok"]), max(d for d, _ in parsed["ok"])
        current = {k: ", ".join(p["name"] for p in v) for k, v in slots_between(lo, hi).items()}
    rows = [{"date": d, "name": info[sid]["name"], "branch": info[sid]["branch"],
             "current": current.get((d.isoformat(), info[sid]["branch"]))} for d, sid in sorted(parsed["ok"])]
    return render_template("import_preview.html", active="schedule", rows=rows, bad=parsed["bad"], text=text)


@app.route("/admin/export.xlsx")
@login_required
def export_xlsx():
    """Assigned days of a period (from/to) or of the shown week/month (view/d) as an Excel file."""
    if request.args.get("from") or request.args.get("to"):
        start, end = parse_day(request.args.get("from")), parse_day(request.args.get("to"))
    else:
        anchor = parse_day(request.args["d"]) if request.args.get("d") else today()
        if request.args.get("view") == "week":
            start = anchor - timedelta(days=anchor.weekday())
            end = start + timedelta(days=6)
        else:
            start = anchor.replace(day=1)
            end = anchor.replace(day=calendar.monthrange(anchor.year, anchor.month)[1])
    if end < start or (end - start).days > 366:
        abort(400)
    rows = q("SELECT d.day, d.branch AS slot, s.name, s.phone FROM duty d JOIN staff s ON s.id = d.staff_id "
             "WHERE d.day BETWEEN ? AND ? ORDER BY d.day, d.branch, s.name", start.isoformat(), end.isoformat())
    data = xlsx_io.build_xlsx(
        (r["day"], WEEKDAYS[date.fromisoformat(r["day"]).weekday()], BRANCH_NAME.get(r["slot"], "No site chosen"),
         r["name"], fmt_phone(r["phone"])) for r in rows)
    audit("duty.export", f"{start.isoformat()} … {end.isoformat()} ({len(rows)} rows)")
    return Response(data, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="duty_{start.isoformat()}_{end.isoformat()}.xlsx"'})


@app.route("/admin/schedule/range", methods=["POST"])
@login_required
def schedule_range():
    """One person on every day from one date to another (their branch's slot): weekends and holidays included."""
    start, end = parse_day(request.form.get("from")), parse_day(request.form.get("to"))
    person = person_for_slot(request.form.get("staff_id", ""))
    if end < start or (end - start).days > 366:
        abort(400)
    count, d = 0, start
    while d <= end:
        add_slot(d, person["branch"], person["id"])
        count += 1
        d += timedelta(days=1)
    audit("duty.range", f"{start.isoformat()} … {end.isoformat()} → {person['name']} ({BRANCH_NAME[person['branch']]}, {count} days)")
    touch()
    return back_to_schedule()


@app.route("/admin/schedule/copy-week", methods=["POST"])
@login_required
def schedule_copy_week():
    monday = parse_day(request.form.get("d") or today().isoformat())
    monday -= timedelta(days=monday.weekday())
    copied = 0
    for i in range(7):
        for r in q("SELECT branch, staff_id FROM duty WHERE day = ?",
                   (monday - timedelta(days=7) + timedelta(days=i)).isoformat()):
            if not allowed(r["branch"]):
                continue
            add_slot(monday + timedelta(days=i), r["branch"], r["staff_id"])
            copied += 1
    audit("duty.copy_week", f"week of {monday.isoformat()}: {copied} slots copied from the previous week")
    touch()
    return back_to_schedule()


# ---------------------------------------------------------------- admin: staff
@app.route("/admin/staff")
@login_required
def staff_list():
    t = today().isoformat()
    counts = {r["staff_id"]: r for r in q(
        "SELECT staff_id, SUM(day >= ?) AS future, SUM(day < ?) AS past FROM duty GROUP BY staff_id", t, t)}
    staff = [p for p in q("SELECT * FROM staff ORDER BY active DESC, name COLLATE NOCASE")
             if allowed(p["branch"]) or p["branch"] == ""]
    warn_days = sum(int(counts[s["id"]]["future"] or 0) for s in staff if not s["active"] and s["id"] in counts)
    return render_template("staff.html", active="staff", counts=counts, staff=staff, warn_days=warn_days)


@app.route("/admin/staff/import", methods=["POST"])
@login_required
def staff_import():
    """Two steps like the schedule import: 'preview' writes nothing, 'confirm' adds the good rows."""
    text = request.form.get("text", "")
    if len(text) > 50_000:
        abort(400)
    existing = [{"name": r["name"]} for r in q("SELECT name FROM staff")]
    try:
        parsed = xlsx_io.parse_staff_paste(text, existing)
    except xlsx_io.TooManyRows:
        abort(400)
    if scope():                                        # a branch administrator adds only to their own branch
        parsed["ok"] = [(n, p, scope()) for n, p, _ in parsed["ok"]]
    if request.form.get("step") == "confirm":
        for name, phone, branch in parsed["ok"]:
            run("INSERT INTO staff(name, email, phone, branch) VALUES (?, '', ?, ?)", name, phone, branch)
        if parsed["ok"]:
            audit("staff.import", f"{len(parsed['ok'])} people added, {len(parsed['bad'])} lines skipped")
            touch()
        return redirect(url_for("staff_list"))
    return render_template("import_staff_preview.html", active="staff", rows=parsed["ok"], bad=parsed["bad"], text=text)


@app.route("/admin/staff/save", methods=["POST"])
@login_required
def staff_save():
    name = request.form.get("name", "").strip()[:80]
    phone = request.form.get("phone", "").strip()[:30]
    if not name:
        abort(400)
    sid = request.form.get("id", "")
    if sid:
        person_access(q1("SELECT * FROM staff WHERE id = ?", sid) or abort(404))
        run("UPDATE staff SET name = ?, phone = ? WHERE id = ?", name, phone, sid)
        audit("staff.edit", name)
    else:
        branch = scope() or request.form.get("branch", "")
        run("INSERT INTO staff(name, email, phone, branch) VALUES (?, '', ?, ?)", name, phone,
            branch if branch in BRANCHES else "")
        audit("staff.add", name)
    touch()
    return redirect(url_for("staff_list"))


@app.route("/admin/staff/<int:sid>/branch", methods=["POST"])
@login_required
def staff_branch(sid):
    """The Site A/Site B switch (any administrator). Moving a person to the other branch empties THEIR duty days from
    today on in the old branch (days before today stay as history). A person with no branch yet takes over the days
    that were assigned before branches existed."""
    person = q1("SELECT * FROM staff WHERE id = ?", sid) or abort(404)
    person_access(person)
    new = request.form.get("branch", "")
    if new not in BRANCHES:
        abort(400)
    old = person["branch"]
    if new == old:
        return redirect(url_for("staff_list"))
    t = today().isoformat()
    cleared = 0
    if old in BRANCHES:
        cleared = q1("SELECT COUNT(*) AS n FROM duty WHERE staff_id = ? AND branch = ? AND day >= ?", sid, old, t)["n"]
        run("DELETE FROM duty WHERE staff_id = ? AND branch = ? AND day >= ?", sid, old, t)
    run("UPDATE staff SET branch = ? WHERE id = ?", new, sid)
    for r in q("SELECT day FROM duty WHERE staff_id = ? AND branch = ''", sid):
        run("INSERT OR IGNORE INTO duty(day, branch, staff_id) VALUES (?, ?, ?)", r["day"], new, sid)
    run("DELETE FROM duty WHERE staff_id = ? AND branch = ''", sid)
    audit("staff.branch", f"{person['name']}: {BRANCH_NAME.get(old, 'no site')} → {BRANCH_NAME[new]}"
                          + (f" ({cleared} future days cleared)" if cleared else ""))
    touch()
    return redirect(url_for("staff_list"))


@app.route("/admin/staff/<int:sid>/toggle", methods=["POST"])
@login_required
def staff_toggle(sid):
    person = q1("SELECT * FROM staff WHERE id = ?", sid) or abort(404)
    person_access(person)
    run("UPDATE staff SET active = ? WHERE id = ?", 0 if person["active"] else 1, sid)
    audit("staff.deactivate" if person["active"] else "staff.activate", person["name"])
    touch()
    return redirect(url_for("staff_list"))


@app.route("/admin/staff/<int:sid>/delete", methods=["POST"])
@login_required
def staff_delete(sid):
    """Remove a person for good, together with every duty day that points at them."""
    person = q1("SELECT * FROM staff WHERE id = ?", sid) or abort(404)
    person_access(person)
    t = today().isoformat()
    c = q1("SELECT COALESCE(SUM(day >= ?), 0) AS future, COALESCE(SUM(day < ?), 0) AS past FROM duty WHERE staff_id = ?",
           t, t, sid)
    run("DELETE FROM duty WHERE staff_id = ?", sid)
    run("DELETE FROM staff WHERE id = ?", sid)
    audit("staff.delete", f"{person['name']}: {c['future']} future and {c['past']} past duty days removed")
    touch()
    return redirect(url_for("staff_list"))


# ---------------------------------------------------------------- admin: users (owner only)
def users_page(new_password=None, for_user=None, error=None):
    rows = q("SELECT * FROM users ORDER BY role, username")
    locks = q("SELECT username, ip, locked_until FROM login_locks WHERE locked_until IS NOT NULL AND locked_until > ? "
              "ORDER BY locked_until DESC", iso(now_utc()))
    locked = {r["id"] for r in rows if r["username"] in {l["username"] for l in locks}}
    history = q("SELECT ts, actor, action, detail, ip FROM audit "
                "WHERE action IN ('login', 'login.failed', 'login.locked') ORDER BY id DESC LIMIT 50")
    return render_template("users.html", active="users", users=rows, locked=locked, locks=locks, history=history,
                           new_password=new_password, for_user=for_user, error=error)


@app.route("/admin/users")
@owner_required
def users():
    return users_page()


@app.route("/admin/users/create", methods=["POST"])
@owner_required
def users_create():
    username = request.form.get("username", "").strip().lower()
    display = request.form.get("display_name", "").strip()[:80]
    if not re.fullmatch(r"[a-z0-9._-]{3,32}", username) or not display:
        return users_page(error="User name: 3-32 characters (a-z, 0-9, . _ -); the full name is required"), 400
    if q1("SELECT 1 FROM users WHERE username = ?", username):
        return users_page(error="That user already exists"), 400
    temp = temp_password()
    branch = request.form.get("branch", "")
    branch = branch if branch in BRANCHES else ""
    run("INSERT INTO users(username, display_name, password_hash, role, must_change, created_at, branch) "
        "VALUES (?, ?, ?, 'admin', 1, ?, ?)", username, display, ph.hash(temp), iso(now_utc()), branch)
    audit("user.create", f"{username} ({BRANCH_NAME.get(branch, 'both sites')})")
    return users_page(new_password=temp, for_user=username)


@app.route("/admin/users/<int:uid>/scope", methods=["POST"])
@owner_required
def users_scope(uid):
    """Which branch an administrator manages: site_a, site_b, or both (empty)."""
    target = q1("SELECT * FROM users WHERE id = ?", uid) or abort(404)
    branch = request.form.get("branch", "")
    if target["role"] == "owner" or branch not in BRANCHES + ("",):
        abort(400)
    run("UPDATE users SET branch = ? WHERE id = ?", branch, uid)
    audit("user.scope", f"{target['username']}: {BRANCH_NAME.get(branch, 'both sites')}")
    return redirect(url_for("users"))


@app.route("/admin/users/<int:uid>/reset", methods=["POST"])
@owner_required
def users_reset(uid):
    target = q1("SELECT * FROM users WHERE id = ?", uid) or abort(404)
    if target["id"] == g.user["id"]:
        abort(400)                                  # your own password: use Settings
    temp = temp_password()
    run("UPDATE users SET password_hash = ?, must_change = 1, failed = 0, locked_until = NULL, sv = sv + 1 WHERE id = ?",
        ph.hash(temp), uid)
    run("DELETE FROM login_locks WHERE username = ?", target["username"])
    audit("user.password_reset", target["username"])
    return users_page(new_password=temp, for_user=target["username"])


@app.route("/admin/users/<int:uid>/unlock", methods=["POST"])
@owner_required
def users_unlock(uid):
    target = q1("SELECT * FROM users WHERE id = ?", uid) or abort(404)
    run("DELETE FROM login_locks WHERE username = ?", target["username"])
    audit("user.unlock", target["username"])
    return redirect(url_for("users"))


@app.route("/admin/locks/unlock", methods=["POST"])
@owner_required
def locks_unlock():
    """Unlock one user on one computer. The owner decides: a colleague who mistyped, or somebody guessing."""
    username = request.form.get("username", "").strip()[:64]
    ip = request.form.get("ip", "").strip()[:64]
    if not username or not ip:
        abort(400)
    run("DELETE FROM login_locks WHERE username = ? AND ip = ?", username, ip)
    audit("user.unlock", f"{username} from {ip}")
    return redirect(url_for("users"))


@app.route("/admin/users/<int:uid>/toggle", methods=["POST"])
@owner_required
def users_toggle(uid):
    target = q1("SELECT * FROM users WHERE id = ?", uid) or abort(404)
    if target["id"] == g.user["id"]:
        abort(400)
    run("UPDATE users SET active = ? WHERE id = ?", 0 if target["active"] else 1, uid)
    audit("user.deactivate" if target["active"] else "user.activate", target["username"])
    return redirect(url_for("users"))


# ---------------------------------------------------------------- admin: audit log
@app.route("/admin/audit")
@login_required
def audit_log():
    where, args, f = audit_filter()
    total = q1(f"SELECT COUNT(*) AS n FROM audit{where}", *args)["n"]
    rows = q(f"SELECT * FROM audit{where} ORDER BY id DESC LIMIT 300", *args)
    return render_template("audit.html", active="audit", rows=rows, total=total, f=f, filtered=any(f.values()),
                           users=[r["actor"] for r in q("SELECT DISTINCT actor FROM audit ORDER BY actor")]
                           if g.user["role"] == "owner" else [g.user["username"]],
                           actions=[r["action"] for r in (q("SELECT DISTINCT action FROM audit ORDER BY action")
                                    if g.user["role"] == "owner" else
                                    q("SELECT DISTINCT action FROM audit WHERE actor = ? ORDER BY action", g.user["username"]))])


def day_start(d):
    """UTC timestamp (same text format as the audit column) of 00:00 local time of day d."""
    return datetime(d.year, d.month, d.day, tzinfo=TZ).astimezone(timezone.utc).isoformat(timespec="seconds")


def audit_filter(source=None):
    source = request.args if source is None else source
    f = {k: source.get(k, "").strip() for k in ("user", "action", "q", "from", "to")}
    where, args = [], []
    if f["user"]:
        where.append("actor = ?"); args.append(f["user"])
    if f["action"]:
        where.append("action = ?"); args.append(f["action"])
    if f["q"]:
        like = "%" + f["q"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(detail LIKE ? ESCAPE '\\' OR action LIKE ? ESCAPE '\\')"); args += [like, like]
    if f["from"]:
        where.append("ts >= ?"); args.append(day_start(parse_day(f["from"])))
    if f["to"]:
        where.append("ts < ?"); args.append(day_start(parse_day(f["to"]) + timedelta(days=1)))
    if g.user["role"] != "owner":                         # administrators see only what they did themselves
        where.append("actor = ?"); args.append(g.user["username"])
    return (" WHERE " + " AND ".join(where)) if where else "", args, f


@app.route("/admin/audit/delete", methods=["POST"])
@owner_required
def audit_delete():
    """Owner only. Removes log rows for good and writes NOTHING about it: no audit entry, no trace on the site.
    mode: selected (ids), filtered (the current filter), all (needs the typed word)."""
    mode = request.form.get("mode", "")
    if mode == "selected":
        ids = [int(x) for x in request.form.getlist("ids") if x.isdigit()][:5000]
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            run(f"DELETE FROM audit WHERE id IN ({','.join('?' * len(chunk))})", *chunk)
    elif mode == "filtered":
        where, args, f = audit_filter(request.form)
        if not where:
            abort(400)
        run(f"DELETE FROM audit{where}", *args)
    elif mode == "all":
        if request.form.get("word", "").strip() != "delete":
            abort(400)
        run("DELETE FROM audit")
    else:
        abort(400)
    return redirect(url_for("audit_log"))


@app.route("/admin/audit.xlsx")
@login_required
def audit_export():
    where, args, f = audit_filter()
    rows = q(f"SELECT * FROM audit{where} ORDER BY id DESC LIMIT 5000", *args)
    data = xlsx_io.build_xlsx(
        ((datetime.fromisoformat(r["ts"]).astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S"), r["actor"], r["action"], r["detail"], r["ip"])
         for r in rows),
        headers=("Time", "User", "Action", "Details", "IP"), title="Audit log", widths=(20, 18, 22, 60, 16))
    return Response(data, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="audit.xlsx"'})


# ---------------------------------------------------------------- errors
@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(429)
def error_page(err):
    texts = {400: "Bad request", 403: "Access denied",
             404: "Page not found", 429: "Too many attempts, try again later"}
    return render_template("error.html", code=err.code, text=texts.get(err.code, "Error")), err.code


# ---------------------------------------------------------------- command line (docker exec ...)
@app.cli.command("create-owner")
@click.option("--username", prompt="Owner username")
@click.option("--name", "display_name", prompt="Display name")
@click.password_option(prompt="Password (min 7 chars: upper, lower, digit, symbol)", confirmation_prompt=True)
def cli_create_owner(username, display_name, password):
    """Create the first (or an additional) owner account."""
    username = username.strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]{3,32}", username):
        raise click.ClickException("username: 3-32 characters of a-z 0-9 . _ -")
    problem = password_problem(password, password, username)
    if problem:
        raise click.ClickException(f"password rejected: min {PW_MIN} characters with an uppercase letter, "
                                   "a lowercase letter, a digit and a special character")
    if q1("SELECT 1 FROM users WHERE username = ?", username):
        raise click.ClickException("that username already exists (use reset-password)")
    run("INSERT INTO users(username, display_name, password_hash, role, must_change, created_at) "
        "VALUES (?, ?, ?, 'owner', 0, ?)", username, display_name, ph.hash(password), iso(now_utc()))
    audit("user.create_owner", username, actor="cli")
    click.echo(f"Owner '{username}' created.")


@app.cli.command("bootstrap-admin")
def cli_bootstrap_admin():
    """First run only: if there are NO accounts yet, create owner 'admin' with the temporary
    password 'admin'. The account is flagged must_change, so the first login FORCES a new password."""
    if q1("SELECT 1 FROM users"):
        click.echo("accounts already exist: nothing to do")
        return
    run("INSERT INTO users(username, display_name, password_hash, role, must_change, created_at) "
        "VALUES ('admin', 'Administrator', ?, 'owner', 1, ?)", ph.hash("admin"), iso(now_utc()))
    audit("user.bootstrap_admin", "admin", actor="cli")
    click.echo("Initial owner created: username 'admin', temporary password 'admin' (must be changed at first login).")


@app.cli.command("reset-password")
@click.option("--username", prompt="Username to reset")
def cli_reset_password(username):
    """Break-glass: set a new temporary password for ANY account (incl. the owner)."""
    u = q1("SELECT * FROM users WHERE username = ?", username.strip())
    if not u:
        raise click.ClickException("no such user")
    temp = temp_password()
    run("UPDATE users SET password_hash = ?, must_change = 1, failed = 0, locked_until = NULL, active = 1, sv = sv + 1 WHERE id = ?",
        ph.hash(temp), u["id"])
    run("DELETE FROM login_locks WHERE username = ?", u["username"])
    audit("user.password_reset", u["username"], actor="cli")
    click.echo(f"Temporary password for {u['username']} (shown once, must be changed at login):\n  {temp}")


@app.cli.command("unlock-user")
@click.option("--username", prompt="Username to unlock")
def cli_unlock_user(username):
    """Break-glass: clear every login lock of an account (all computers)."""
    u = q1("SELECT * FROM users WHERE username = ?", username.strip())
    if not u:
        raise click.ClickException("no such user")
    run("DELETE FROM login_locks WHERE username = ?", u["username"])
    audit("user.unlock", u["username"], actor="cli")
    click.echo(f"All login locks of {u['username']} were cleared.")


@app.cli.command("list-users")
def cli_list_users():
    """Show accounts (no secrets)."""
    for u in q("SELECT * FROM users ORDER BY role, username"):
        state = "active" if u["active"] else "disabled"
        click.echo(f"{u['username']:<20} {u['role']:<6} {state:<9} must_change={u['must_change']}  {u['display_name']}")


@app.cli.command("login-ips")
def cli_login_ips():
    """Client addresses seen in login events of the last 60 days, one per line (used by vm/40-hostnames.sh)."""
    cutoff = iso(now_utc() - timedelta(days=60))
    for r in q("SELECT DISTINCT ip FROM audit WHERE action IN ('login', 'login.failed', 'login.locked') "
               "AND ip != '' AND ts >= ? ORDER BY ip", cutoff):
        click.echo(r["ip"])


@app.cli.command("backup")
@click.option("--keep", default=14, show_default=True, help="how many backups to keep")
def cli_backup(keep):
    """Consistent copy of the database into <data>/backups (safe while the app is running)."""
    click.echo(make_backup(keep))


@app.cli.command("seed-demo")
def cli_seed_demo():
    """Fill sample staff (placeholder names, both branches) and two weeks of duty (for testing only)."""
    if q1("SELECT 1 FROM staff"):
        raise click.ClickException("staff already exist: refusing to add demo data")
    people = [(f"Person {i}", f"person{i}@example.com", "555 0100", BRANCHES[i % 2]) for i in range(1, 7)]
    ids = {b: [] for b in BRANCHES}
    for name, email, phone, branch in people:
        ids[branch].append(run("INSERT INTO staff(name, email, phone, branch) VALUES (?, ?, ?, ?)", name, email, phone, branch).lastrowid)
    t = today()
    for i in range(-3, 14):
        d = t + timedelta(days=i)
        for branch in BRANCHES:
            add_slot(d, branch, ids[branch][d.toordinal() % len(ids[branch])])
    touch()
    click.echo("demo data added")
