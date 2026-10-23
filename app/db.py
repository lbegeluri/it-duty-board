"""SQLite storage for IT Duty. One file, kept on the host (bind mount), never inside the image."""
import os
import sqlite3

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "itduty.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (          -- local ("offline") admin accounts, NOT AD accounts
  id            INTEGER PRIMARY KEY,
  username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name  TEXT NOT NULL,
  password_hash TEXT NOT NULL,              -- argon2id, never the password itself
  role          TEXT NOT NULL CHECK (role IN ('owner', 'admin')),
  must_change   INTEGER NOT NULL DEFAULT 1, -- 1 = must choose a new password at next login
  failed        INTEGER NOT NULL DEFAULT 0,
  locked_until  TEXT,                       -- ISO time (UTC) while the account is locked
  active        INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL,
  sv            INTEGER NOT NULL DEFAULT 0  -- session version: bumped on every password change/reset, which
                                            -- instantly ends all existing logins of that account
);
CREATE TABLE IF NOT EXISTS staff (          -- the people who can be on duty
  id     INTEGER PRIMARY KEY,
  name   TEXT NOT NULL,
  email  TEXT NOT NULL DEFAULT '',
  phone  TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1,
  branch TEXT NOT NULL DEFAULT ''           -- 'site_a', 'site_b' or '' (not chosen yet)
);
CREATE TABLE IF NOT EXISTS duty (           -- the people on duty per calendar day AND branch (site_a / site_b), one or more
  day      TEXT NOT NULL,                   -- YYYY-MM-DD
  branch   TEXT NOT NULL DEFAULT '',        -- 'site_a', 'site_b'; '' only for days that were assigned before branches existed
  staff_id INTEGER NOT NULL REFERENCES staff(id),
  PRIMARY KEY (day, branch, staff_id)
);
CREATE TABLE IF NOT EXISTS holiday_overrides (   -- the owner's changes to the automatic holiday list
  day  TEXT PRIMARY KEY,                    -- YYYY-MM-DD
  kind TEXT NOT NULL,                       -- 'add' = extra day off, 'off' = automatic holiday switched off
  name TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS audit (          -- append-only history of every change
  id     INTEGER PRIMARY KEY,
  ts     TEXT NOT NULL,                     -- ISO time (UTC)
  actor  TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS login_locks (     -- failed logins per user name AND client address (not per account)
  username     TEXT NOT NULL,
  ip           TEXT NOT NULL,
  failed       INTEGER NOT NULL DEFAULT 0,
  last_fail    TEXT,
  locked_until TEXT,
  PRIMARY KEY (username, ip)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def migrate(conn):
    """Bring an older database up to date. Safe to run on every start."""
    if "branch" not in _columns(conn, "staff"):
        conn.execute("ALTER TABLE staff ADD COLUMN branch TEXT NOT NULL DEFAULT ''")
    if "branch" not in _columns(conn, "duty"):
        # one person per day -> one person per day AND branch. Old days go to the person's branch, or to the
        # "branch not set" slot ('') until the owner or an administrator chooses it on the Staff page.
        conn.executescript("""
            BEGIN;
            ALTER TABLE duty RENAME TO duty_old;
            CREATE TABLE duty (
              day TEXT NOT NULL, branch TEXT NOT NULL DEFAULT '',
              staff_id INTEGER NOT NULL REFERENCES staff(id), PRIMARY KEY (day, branch));
            INSERT INTO duty(day, branch, staff_id)
              SELECT d.day, COALESCE(s.branch, ''), d.staff_id FROM duty_old d JOIN staff s ON s.id = d.staff_id;
            DROP TABLE duty_old;
            COMMIT;
        """)


def migrate_multi(conn):
    """One day and branch may have several people on duty: the primary key gets staff_id as a third part."""
    pk = [r[1] for r in conn.execute("PRAGMA table_info(duty)") if r[5]]
    if "staff_id" in pk:
        return
    conn.executescript("""
        BEGIN;
        ALTER TABLE duty RENAME TO duty_old;
        CREATE TABLE duty (
          day TEXT NOT NULL, branch TEXT NOT NULL DEFAULT '',
          staff_id INTEGER NOT NULL REFERENCES staff(id), PRIMARY KEY (day, branch, staff_id));
        INSERT INTO duty(day, branch, staff_id) SELECT day, branch, staff_id FROM duty_old;
        DROP TABLE duty_old;
        COMMIT;
    """)


def init():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = connect()
    conn.executescript(SCHEMA)
    for table, column, ddl in (("users", "sv", "INTEGER NOT NULL DEFAULT 0"),      # older databases
                               ("users", "last_login", "TEXT"),
                               ("users", "last_ip", "TEXT"),
                               ("users", "branch", "TEXT NOT NULL DEFAULT ''"),   # '' = manages both branches
                               ("audit", "ip", "TEXT NOT NULL DEFAULT ''")):
        if column not in [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    migrate(conn)
    migrate_multi(conn)
    conn.commit()
    conn.close()
