import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


def new_id():
    return uuid.uuid4().hex


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, starter TEXT NOT NULL,
    opening_prompt TEXT, seats TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS branches (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
    head_id TEXT REFERENCES turns(id), source_id TEXT REFERENCES branches(id),
    kind TEXT NOT NULL, anchor TEXT, revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
    parent_id TEXT REFERENCES turns(id), seat TEXT NOT NULL, text TEXT NOT NULL,
    origin TEXT NOT NULL, attempt_id TEXT REFERENCES attempts(id), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
    branch_id TEXT NOT NULL REFERENCES branches(id), mode TEXT NOT NULL,
    turn_limit INTEGER NOT NULL, committed_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL, ended_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs(session_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS branch_runs ON runs(branch_id);
CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
    branch_id TEXT NOT NULL REFERENCES branches(id), seat TEXT NOT NULL,
    starting_head_id TEXT REFERENCES turns(id), request TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, reason TEXT,
    metadata TEXT, created_at TEXT NOT NULL, ended_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_attempt ON attempts(run_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS branch_attempts ON attempts(branch_id);
CREATE INDEX IF NOT EXISTS session_branches ON branches(session_id);
"""

JSON_COLUMNS = {"seats", "anchor", "request", "metadata"}


def record(row):
    if row is None:
        return None
    return {
        key: json.loads(value) if key in JSON_COLUMNS and value is not None else value
        for key, value in dict(row).items()
    }


class Store:
    def __init__(self, path):
        self.lease = None
        if str(path) != ":memory:":
            path = Path(path).resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            self.lease = sqlite3.connect(str(path) + ".lock", timeout=0, check_same_thread=False)
            try:
                self.lease.execute("BEGIN EXCLUSIVE")
            except sqlite3.Error as error:
                self.lease.close()
                if error.sqlite_errorcode in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise OSError("This ReplyLab database is already open in another service") from None
                raise
        try:
            self.db = sqlite3.connect(path, check_same_thread=False)
        except sqlite3.Error:
            if self.lease is not None:
                self.lease.close()
            raise
        try:
            self._initialize()
        except sqlite3.Error:
            self.close()
            raise

    def _initialize(self):
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript(SCHEMA)
        now = timestamp()
        with self.db:
            self.write(
                "UPDATE attempts SET status = 'interrupted', reason = 'service_restart', ended_at = ?"
                " WHERE status = 'active'", now
            )
            self.write(
                "UPDATE runs SET status = 'interrupted', reason = 'service_restart', ended_at = ?"
                " WHERE status = 'active'", now
            )

    def close(self):
        self.db.close()
        if self.lease is not None:
            self.lease.close()

    def one(self, sql, *params):
        return record(self.db.execute(sql, params).fetchone())

    def all(self, sql, *params):
        return [record(row) for row in self.db.execute(sql, params)]

    def write(self, sql, *params):
        self.db.execute(sql, params)

    def history(self, head_id):
        return self.all("""
            WITH RECURSIVE lineage(id, depth) AS (
                SELECT ?, 0
                UNION ALL
                SELECT t.parent_id, l.depth + 1 FROM lineage l
                JOIN turns t ON t.id = l.id WHERE t.parent_id IS NOT NULL
            )
            SELECT t.* FROM lineage l JOIN turns t ON t.id = l.id ORDER BY l.depth DESC
        """, head_id)
