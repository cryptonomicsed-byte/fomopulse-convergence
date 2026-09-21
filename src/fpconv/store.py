"""SQLite persistence, in the shapes Vantage already reads.

`alpha_clusters` and `alpha_signals_log` are Vantage's existing tables (see
backend/routers/alpha_hunter.py). Writing into those shapes means the Vantage
side needs no new reader — a signal from here appears in `/clusters` and
`/events` beside whatever alpha_hunter already found.

Also keeps the engine's own tables (snapshots, runs) so a scan is auditable:
what the tape said, what we scored, and what we emitted, all on one row.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB = Path.home() / ".fpconv" / "fpconv.db"

SCHEMA = """
-- Vantage-compatible: read by backend/routers/alpha_hunter.py
CREATE TABLE IF NOT EXISTS alpha_clusters (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at INTEGER NOT NULL,
    token       TEXT,
    symbol      TEXT,
    kind        TEXT,
    score       REAL,
    wallets_json TEXT,
    meta_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_clusters_detected ON alpha_clusters(detected_at DESC);

CREATE TABLE IF NOT EXISTS alpha_signals_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER,
    kind    TEXT,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON alpha_signals_log(ts DESC);

-- The engine's own record: one row per scan, so a signal can be explained later
CREATE TABLE IF NOT EXISTS fp_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   INTEGER,
    finished_at  INTEGER,
    window       TEXT,
    tape_lag_s   INTEGER,
    fills_seen   INTEGER,
    tokens_seen  INTEGER,
    converged    INTEGER,
    emitted      INTEGER,
    health_json  TEXT,
    roster_json  TEXT,
    error        TEXT
);

-- One row per token per scan: the longitudinal series the anomaly detector
-- needs across runs, not just within one window.
CREATE TABLE IF NOT EXISTS fp_token_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER,
    observed_at     INTEGER,
    token           TEXT,
    symbol          TEXT,
    distinct_buyers INTEGER,
    proven_buyers   INTEGER,
    weight_sum      REAL,
    net_buy_usd     REAL,
    buy_fills       INTEGER,
    sell_fills      INTEGER,
    attention_z     REAL,
    acceleration    REAL,
    score           REAL,
    verdict         TEXT,
    blocked         INTEGER,
    payload         TEXT
);
CREATE INDEX IF NOT EXISTS idx_hist_token ON fp_token_history(token, observed_at DESC);

-- Paper-trader sidecar. Additive on purpose: the paper trader's own portfolio
-- and trades tables keep their exact schema and are only touched when an
-- operator explicitly runs with --execute.
CREATE TABLE IF NOT EXISTS fpconv_signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    emitted_at    INTEGER,
    token         TEXT,
    symbol        TEXT,
    name          TEXT,
    score         REAL,
    verdict       TEXT,
    actionable    INTEGER,
    distinct_buyers INTEGER,
    proven_buyers INTEGER,
    best_rank     INTEGER,
    attention_z   REAL,
    acceleration  REAL,
    early_lag     INTEGER,
    net_buy_usd   REAL,
    price         REAL,
    reasons_json  TEXT,
    filters_json  TEXT,
    wallets_json  TEXT,
    executed      INTEGER DEFAULT 0,
    executed_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_fpconv_emitted ON fpconv_signals(emitted_at DESC);
CREATE INDEX IF NOT EXISTS idx_fpconv_token ON fpconv_signals(token);

-- Which wallets we are following, and why. Lets the roster be reviewed.
CREATE TABLE IF NOT EXISTS fp_roster (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at INTEGER,
    handle      TEXT,
    address     TEXT,
    rank        INTEGER,
    score       REAL,
    record      TEXT,
    followers   INTEGER,
    win_rate    REAL,
    trips       INTEGER,
    realized    REAL,
    open_value  REAL,
    payload     TEXT
);
CREATE INDEX IF NOT EXISTS idx_roster_addr ON fp_roster(address, observed_at DESC);
"""


class Store:
    def __init__(self, path: Path | str = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ── runs ─────────────────────────────────────────────────────────────

    def start_run(self, window: str, tape_lag_s: int) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO fp_runs (started_at, window, tape_lag_s) VALUES (?,?,?)",
                (int(time.time()), window, int(tape_lag_s)),
            )
            return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        fills: int,
        tokens: int,
        converged: int,
        emitted: int,
        health: dict,
        roster: dict,
        error: str | None = None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """UPDATE fp_runs SET finished_at=?, fills_seen=?, tokens_seen=?,
                   converged=?, emitted=?, health_json=?, roster_json=?, error=?
                   WHERE id=?""",
                (
                    int(time.time()),
                    fills,
                    tokens,
                    converged,
                    emitted,
                    json.dumps(health),
                    json.dumps(roster),
                    error,
                    run_id,
                ),
            )

    # ── signals ──────────────────────────────────────────────────────────

    def save_signals(self, run_id: int, observed_at: int, signals: list) -> int:
        """Persist scored tokens in three places: the Vantage-compatible shapes,
        the engine's own sidecar, and the longitudinal history."""
        n = 0
        with self._conn() as c:
            for s in signals:
                ev = s.evidence
                c.execute(
                    """INSERT INTO fpconv_signals
                       (emitted_at, token, symbol, name, score, verdict, actionable,
                        distinct_buyers, proven_buyers, best_rank, attention_z,
                        acceleration, early_lag, net_buy_usd, price,
                        reasons_json, filters_json, wallets_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        observed_at, s.token, s.symbol, s.name, s.score, s.verdict,
                        int(s.actionable), ev.get("distinct_buyers"), ev.get("proven_buyers"),
                        ev.get("best_rank"), ev.get("attention_z"), ev.get("acceleration"),
                        ev.get("early_lag"), ev.get("net_buy_usd"), ev.get("price"),
                        json.dumps(s.reasons), json.dumps(s.filters.to_dict()),
                        json.dumps(ev.get("buyer_handles", [])),
                    ),
                )
                c.execute(
                    "INSERT INTO fp_token_history (run_id, observed_at, token, symbol,"
                    " distinct_buyers, proven_buyers, weight_sum, net_buy_usd, buy_fills,"
                    " sell_fills, attention_z, acceleration, score, verdict, blocked, payload)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id, observed_at, s.token, s.symbol, ev.get("distinct_buyers"),
                        ev.get("proven_buyers"), ev.get("weight_sum"), ev.get("net_buy_usd"),
                        ev.get("buy_fills"), ev.get("sell_fills"), ev.get("attention_z"),
                        ev.get("acceleration"), s.score, s.verdict, int(s.filters.blocked),
                        json.dumps(s.to_dict()),
                    ),
                )
                if s.actionable:
                    cl = s.as_cluster(observed_at)
                    c.execute(
                        "INSERT INTO alpha_clusters (detected_at, token, symbol, kind, score,"
                        " wallets_json, meta_json) VALUES (?,?,?,?,?,?,?)",
                        (
                            cl["detected_at"], cl["token"], cl["symbol"], cl["kind"],
                            cl["score"], cl["wallets_json"], cl["meta_json"],
                        ),
                    )
                    ev_row = s.as_event()
                    ev_row["ts"] = observed_at
                    c.execute(
                        "INSERT INTO alpha_signals_log (ts, kind, payload) VALUES (?,?,?)",
                        (ev_row["ts"], ev_row["kind"], ev_row["payload"]),
                    )
                n += 1
        return n

    def save_roster(self, observed_at: int, scores: list) -> int:
        with self._conn() as c:
            for s in scores:
                c.execute(
                    """INSERT INTO fp_roster (observed_at, handle, address, rank, score,
                       record, followers, win_rate, trips, realized, open_value, payload)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        observed_at, s.handle, s.address, s.rank, s.score, s.record,
                        s.followers, s.win_rate, s.trips, s.realized,
                        s.detail.get("open_value", 0.0) if s.detail else 0.0,
                        json.dumps(s.to_dict()),
                    ),
                )
        return len(scores)

    # ── reads ────────────────────────────────────────────────────────────

    def token_series(self, token: str, limit: int = 200) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM fp_token_history WHERE token=? ORDER BY observed_at DESC LIMIT ?",
                (token.lower(), limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def last_run(self) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM fp_runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(r) if r else None

    def counts(self) -> dict:
        with self._conn() as c:
            def one(q):
                try:
                    return c.execute(q).fetchone()[0]
                except sqlite3.Error:
                    return 0

            return {
                "runs": one("SELECT COUNT(*) FROM fp_runs"),
                "signals": one("SELECT COUNT(*) FROM fpconv_signals"),
                "history": one("SELECT COUNT(*) FROM fp_token_history"),
                "clusters": one("SELECT COUNT(*) FROM alpha_clusters"),
                "events": one("SELECT COUNT(*) FROM alpha_signals_log"),
                "roster": one("SELECT COUNT(*) FROM fp_roster"),
            }
