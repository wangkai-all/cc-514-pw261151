"""SQLite persistence: observations and completions live in separate tables.

Key invariants
--------------
* ``observation_versions`` is append-only.  A late correction adds a version;
  nothing is overwritten, which makes point-in-time replay possible.
* ``completions`` is keyed by (run, ts) and never replaces an observation.
  When real data arrives for a completed slot, the estimate is retained so it
  can be scored against the truth ("late-arrival backtest").
* ``runs`` pins the strategy version and params used, so replay is exact.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS series (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    unit             TEXT NOT NULL DEFAULT '',
    timezone         TEXT NOT NULL,
    interval_kind    TEXT NOT NULL DEFAULT 'fixed',
    interval_seconds INTEGER,
    window_start     INTEGER NOT NULL,
    window_end       INTEGER NOT NULL,
    baseline         REAL NOT NULL DEFAULT 0,
    description      TEXT NOT NULL DEFAULT '',
    created_ts       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS observation_versions (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id   TEXT NOT NULL REFERENCES series(id),
    ts          INTEGER NOT NULL,
    value       REAL NOT NULL,
    received_ts INTEGER NOT NULL,
    batch_id    INTEGER,
    source      TEXT NOT NULL DEFAULT 'stream',
    note        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS obs_series_ts ON observation_versions(series_id, ts, seq);
CREATE INDEX IF NOT EXISTS obs_received ON observation_versions(series_id, received_ts);

CREATE TABLE IF NOT EXISTS strategy_registry (
    strategy_id TEXT NOT NULL,
    version     INTEGER NOT NULL,
    label       TEXT NOT NULL,
    params_json TEXT NOT NULL,
    changelog   TEXT NOT NULL DEFAULT '',
    created_ts  INTEGER NOT NULL,
    PRIMARY KEY (strategy_id, version)
);

CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id       TEXT NOT NULL REFERENCES series(id),
    strategy_id     TEXT NOT NULL,
    version         INTEGER NOT NULL,
    params_json     TEXT NOT NULL,
    params_hash     TEXT NOT NULL,
    created_ts      INTEGER NOT NULL,
    last_updated_ts INTEGER NOT NULL,
    last_batch_id   INTEGER,
    data_cutoff_ts  INTEGER,
    status          TEXT NOT NULL DEFAULT 'active',
    UNIQUE(series_id, strategy_id, version, params_hash)
);

CREATE TABLE IF NOT EXISTS completions (
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    ts          INTEGER NOT NULL,
    idx         INTEGER NOT NULL,
    value       REAL NOT NULL,
    quality     TEXT NOT NULL DEFAULT 'estimated',
    computed_at INTEGER NOT NULL,
    context_start_idx INTEGER NOT NULL,
    context_end_idx   INTEGER NOT NULL,
    batch_id    INTEGER,
    PRIMARY KEY (run_id, ts)
);
CREATE INDEX IF NOT EXISTS comp_idx ON completions(run_id, idx);

CREATE TABLE IF NOT EXISTS ingest_batches (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id          TEXT NOT NULL REFERENCES series(id),
    created_ts         INTEGER NOT NULL,
    kind               TEXT NOT NULL,
    value_count        INTEGER NOT NULL,
    min_ts             INTEGER,
    max_ts             INTEGER,
    affected_start_ts  INTEGER,
    affected_end_ts    INTEGER,
    gaps_touched       INTEGER NOT NULL DEFAULT 0,
    runs_updated       INTEGER NOT NULL DEFAULT 0,
    rows_written       INTEGER NOT NULL DEFAULT 0,
    rows_preserved     INTEGER NOT NULL DEFAULT 0,
    duration_ms        REAL NOT NULL DEFAULT 0,
    note               TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS replay_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id),
    created_ts  INTEGER NOT NULL,
    as_of_ts    INTEGER NOT NULL,
    matched     INTEGER NOT NULL,
    mismatched  INTEGER NOT NULL,
    only_stored INTEGER NOT NULL,
    only_replay INTEGER NOT NULL,
    max_abs_diff REAL,
    detail_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS off_grid_points (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id   TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    ts          INTEGER NOT NULL,
    value       REAL NOT NULL,
    received_ts INTEGER NOT NULL,
    source      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS off_grid_series ON off_grid_points(series_id, ts);

CREATE TABLE IF NOT EXISTS gap_eval_marks (
    run_id     INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    ts         INTEGER NOT NULL,
    truth      REAL NOT NULL,
    estimate   REAL,
    origin     TEXT NOT NULL,
    noted_ts   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, ts)
);
"""

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_db_path: str | None = None


def connect(path: str | None = None) -> sqlite3.Connection:
    global _conn, _db_path
    with _lock:
        if path is None:
            path = _db_path or os.environ.get("SPARSE_DB", os.path.join(os.getcwd(), "sparse.db"))
        if _conn is not None and path == _db_path:
            return _conn
        if _conn is not None:
            _conn.close()
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        _conn = conn
        _db_path = path
        return conn


def now_ms() -> int:
    return int(time.time() * 1000)


def reset_for_tests(path: str) -> None:
    global _conn, _db_path
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None
        _db_path = None
        if os.path.exists(path):
            os.remove(path)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)
        connect(path)


def rows(cur: sqlite3.Cursor) -> list[sqlite3.Row]:
    return cur.fetchall()


def json_load(text: str, default=None):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default
