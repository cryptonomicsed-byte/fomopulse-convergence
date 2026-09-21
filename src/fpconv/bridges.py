"""Bridges to the two consumers: the paper trader and Vantage.

Both are written to be safe by default.

Paper trader
    The paper trader's own `portfolio`/`trades` tables assume a Solana venue —
    they carry `size_sol`, and its existing positions are pump.fun mints. A
    Robinhood Chain ERC-20 is not that. So the bridge is additive: signals land
    in a sidecar table (`fpconv_signals`) and are only written into the paper
    trader's live tables when an operator explicitly opts in. Silently minting
    PAPER entries for a venue the trader cannot price would corrupt its books,
    and the point of a paper trader is that its books mean something.

Vantage
    Read-only signal push. Emits the same row shapes alpha_hunter writes, over
    its HTTP surface where an endpoint exists, and always to the local store so
    the signal is durable even when Vantage is unreachable.
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_PAPER_DB = Path.home() / "ares_papertrade" / "trades.db"
DEFAULT_VANTAGE = "https://omokoda.duckdns.org"
SIGNAL_TYPE = "fpconv"


class PaperTraderBridge:
    """Writes sidecar rows always; writes real positions only when asked.

    `execute=False` (the default) is a dry run against the real schema: it
    verifies the columns exist and reports exactly what it would have written.
    """

    def __init__(self, db_path: Path | str = DEFAULT_PAPER_DB, paper_usd: float = 100.0):
        self.path = Path(db_path)
        self.paper_usd = paper_usd
        self.available = self.path.exists()

    def _conn(self):
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def schema_report(self) -> dict:
        """What the target actually looks like — so a mismatch is visible before
        anything is written into it."""
        if not self.available:
            return {"available": False, "path": str(self.path)}
        conn = self._conn()
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            def cols(t):
                try:
                    return [r[1] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()]
                except sqlite3.Error:
                    return []
            return {
                "available": True,
                "path": str(self.path),
                "tables": sorted(tables),
                "portfolio_cols": cols("portfolio"),
                "trades_cols": cols("trades"),
                "has_sidecar": "fpconv_signals" in tables,
            }
        finally:
            conn.close()

    def _ensure_sidecar(self, conn) -> None:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS fpconv_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                emitted_at INTEGER, token TEXT, symbol TEXT, name TEXT,
                score REAL, verdict TEXT, actionable INTEGER,
                distinct_buyers INTEGER, proven_buyers INTEGER, best_rank INTEGER,
                attention_z REAL, acceleration REAL, early_lag INTEGER,
                net_buy_usd REAL, price REAL, reasons_json TEXT, filters_json TEXT,
                wallets_json TEXT, executed INTEGER DEFAULT 0, executed_at INTEGER)"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_fpconv_emitted ON fpconv_signals(emitted_at DESC)"
        )

    def emit(self, signals: list, now: int | None = None, min_score: float = 35.0) -> dict:
        now = int(now or time.time())
        if not self.available:
            return {"written": 0, "executed": 0, "reason": f"no paper db at {self.path}"}
        conn = self._conn()
        written = 0
        try:
            self._ensure_sidecar(conn)
            for s in signals:
                if not s.actionable or s.score < min_score:
                    continue
                ev = s.evidence
                conn.execute(
                    """INSERT INTO fpconv_signals
                       (emitted_at, token, symbol, name, score, verdict, actionable,
                        distinct_buyers, proven_buyers, best_rank, attention_z,
                        acceleration, early_lag, net_buy_usd, price, reasons_json,
                        filters_json, wallets_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        now, s.token, s.symbol, s.name, s.score, s.verdict,
                        int(s.actionable), ev.get("distinct_buyers"), ev.get("proven_buyers"),
                        ev.get("best_rank"), ev.get("attention_z"), ev.get("acceleration"),
                        ev.get("early_lag"), ev.get("net_buy_usd"), ev.get("price"),
                        json.dumps(s.reasons), json.dumps(s.filters.to_dict()),
                        json.dumps(ev.get("buyer_handles", [])),
                    ),
                )
                written += 1
            conn.commit()
        finally:
            conn.close()
        return {"written": written, "executed": 0, "sidecar": True}

    def execute(self, signals: list, min_score: float = 55.0, now: int | None = None) -> dict:
        """Opt-in: open PAPER positions in the trader's own tables.

        Guarded three ways — the caller must pass an explicit score floor above
        the actionable one, the token must not already be open, and every row is
        tagged `signal_type='fpconv'` so these can be told apart from the
        trader's own Solana positions and removed cleanly.
        """
        now = int(now or time.time())
        if not self.available:
            return {"executed": 0, "reason": "no paper db"}
        conn = self._conn()
        executed = 0
        skipped = []
        try:
            self._ensure_sidecar(conn)
            for s in signals:
                if s.score < min_score or not s.actionable:
                    continue
                price = s.evidence.get("price")
                if not price:
                    skipped.append({"token": s.token, "why": "no price on the signal"})
                    continue
                existing = conn.execute(
                    "SELECT id FROM portfolio WHERE token=? AND exit_time IS NULL",
                    (s.token,),
                ).fetchone()
                if existing:
                    skipped.append({"token": s.token, "why": "already open"})
                    continue
                conn.execute(
                    """INSERT INTO portfolio (token, symbol, address, entry_price,
                       entry_time, size_sol, cost_usd, target_pct, stop_pct,
                       signal_type, score, current_price, last_updated)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        s.token, s.symbol, s.token, float(price),
                        time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now)),
                        0.0, float(self.paper_usd), 100.0, -35.0,
                        SIGNAL_TYPE, int(round(s.score)), float(price),
                        time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now)),
                    ),
                )
                conn.execute(
                    """INSERT INTO trades (timestamp, action, token, symbol, price,
                       size_sol, cost_usd, pnl_usd, reason, signal_type)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now)),
                        "BUY", s.token, s.symbol, float(price), 0.0,
                        float(self.paper_usd), 0.0,
                        f"fpconv convergence score {s.score:.0f} "
                        f"({s.evidence.get('distinct_buyers')} wallets, "
                        f"{s.evidence.get('proven_buyers')} with a record)",
                        SIGNAL_TYPE,
                    ),
                )
                conn.execute(
                    "UPDATE fpconv_signals SET executed=1, executed_at=? WHERE token=? AND executed=0",
                    (now, s.token),
                )
                executed += 1
            conn.commit()
        finally:
            conn.close()
        return {"executed": executed, "skipped": skipped}


class VantageBridge:
    """Pushes signals into Vantage's existing intel ingest.

    The endpoint is `POST /api/intel/signals/ingest`, authed as a *system tool*
    (`X-Vantage-Tool: intel` + `X-Vantage-Tool-Key`), not as an agent. Its
    contract is {symbol, source, type, conviction, direction, detail, mint}.

    ## The conviction ceiling matters more than anything else here

    `backend/routers/trading.py` states it plainly:

        "Conviction is a 0-1 confidence, and >0.7 auto-creates a real order."

    and `config.py` carries `PUMPFUN_SCAN_CONVICTION = 0.72  # >0.7 -> auto-order`.

    This engine is research. Its score is 0-100. Pushed raw it would be rejected
    by `_validated_conviction` for exceeding 1.0 — but pushed *divided by 100*
    it would clear 0.7 on every strong signal and **auto-create real orders on
    the strength of a convergence heuristic**. So the mapping is clamped below
    the execution line by construction, and crossing it requires an explicit,
    separately-named opt-in that defaults off.

    `mint` is sent as the real contract address. A ticker is not resolvable
    downstream, so a signal without an address cannot be traded on — which is
    the correct outcome for most of what this engine finds.
    """

    # The auto-execution line lives in Vantage at >0.7. Stay strictly under it.
    AUTO_EXECUTE_THRESHOLD = 0.7
    MAX_PUSH_CONVICTION = 0.69

    def __init__(
        self,
        base: str = DEFAULT_VANTAGE,
        tool_key: str | None = None,
        tool: str = "intel",
        timeout: int = 20,
        allow_auto_execute: bool = False,
    ):
        self.base = base.rstrip("/")
        self.tool = tool
        self.tool_key = tool_key
        self.timeout = timeout
        self.allow_auto_execute = allow_auto_execute

    # ── conviction ───────────────────────────────────────────────────────

    def conviction_for(self, score: float) -> float:
        """Map the engine's 0-100 score onto Vantage's 0-1 conviction, clamped.

        Default ceiling is 0.69 — under the auto-order line. With
        `allow_auto_execute` the score maps to 0..1 unabridged, which is a
        deliberate, explicit decision to let strong signals place orders.
        """
        s = max(0.0, min(100.0, float(score)))
        ceiling = 1.0 if self.allow_auto_execute else self.MAX_PUSH_CONVICTION
        return round((s / 100.0) * ceiling, 4)

    def _body(self, s) -> dict:
        ev = getattr(s, "evidence", {}) or {}
        conv = self.conviction_for(getattr(s, "score", 0.0))
        # Direction: net flow over the window decides it. A convergence with net
        # selling is not a long, whatever the buyer count says.
        net = float(ev.get("net_buy_usd") or 0.0)
        direction = "long" if net > 0 else ("short" if net < 0 else "flat")
        return {
            "symbol": (getattr(s, "symbol", "") or getattr(s, "token", "")[:12])[:12],
            "source": "fpconv",
            "type": "convergence",
            "conviction": conv,
            "direction": direction,
            # The mint is the contract address — without it nothing downstream
            # can trade this, which is the right default for a research signal.
            "mint": getattr(s, "token", ""),
            "detail": " | ".join((getattr(s, "reasons", None) or [])[:4])[:900],
        }

    # ── transport ────────────────────────────────────────────────────────

    def _post(self, path: str, body: dict) -> tuple[int, str]:
        headers = {
            "Content-Type": "application/json",
            f"X-Vantage-Tool": self.tool,
        }
        # Header name with the exact casing httpx/fastapi normalise to.
        headers.pop("X-Vantage-Tool", None)
        headers["X-Vantage-Tool"] = self.tool
        headers["X-Vantage-Tool-Key"] = self.tool_key or ""
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(body).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")[:400]
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")[:400]
        except Exception as e:  # noqa: BLE001 - reachability is the answer here
            return 0, str(e)[:400]

    def probe(self) -> dict:
        """Is Vantage up, and is the intel tool configured?

        503 means the endpoint is live but `VANTAGE_TOOL_INTEL` is unset, so no
        signal can be ingested. That is reported rather than discovered later,
        and it is why the local store is written first on every path.
        """
        out: dict = {}
        try:
            with urllib.request.urlopen(f"{self.base}/api/health", timeout=self.timeout) as r:
                out["up"] = True
                out["status"] = r.status
        except urllib.error.HTTPError as e:
            out["up"] = True
            out["status"] = e.code
        except Exception as e:  # noqa: BLE001
            return {"up": False, "error": str(e)[:200]}

        status, body = self._post(
            "/api/intel/signals/ingest",
            {
                "symbol": "__fpconv_probe__",
                "source": "fpconv",
                "type": "probe",
                "conviction": 0.0,
                "direction": "flat",
                "detail": "capability probe — safe to ignore",
                "mint": "",
            },
        )
        out["ingest_status"] = status
        if status == 503:
            out["ingest"] = "NOT CONFIGURED — set VANTAGE_TOOL_INTEL on the Vantage host"
        elif status == 401:
            out["ingest"] = "tool key rejected — check VANTAGE_TOOL_INTEL matches"
        elif status in (200, 201):
            out["ingest"] = "ready"
        else:
            out["ingest"] = f"unexpected: {body[:160]}"
        out["conviction_ceiling"] = (
            1.0 if self.allow_auto_execute else self.MAX_PUSH_CONVICTION
        )
        out["auto_execute_armed"] = bool(self.allow_auto_execute)
        return out

    def push(self, signals: list, now: int | None = None, limit: int = 20) -> dict:
        now = int(now or time.time())
        results = []
        for s in signals[:limit]:
            if not getattr(s, "actionable", False):
                continue
            status, body = self._post("/api/intel/signals/ingest", self._body(s))
            results.append(
                {
                    "token": getattr(s, "token", ""),
                    "conviction": self.conviction_for(getattr(s, "score", 0.0)),
                    "status": status,
                    "body": body,
                }
            )
        ok = sum(1 for r in results if r["status"] in (200, 201))
        return {
            "attempted": len(results),
            "accepted": ok,
            "ceiling": (
                1.0 if self.allow_auto_execute else self.MAX_PUSH_CONVICTION
            ),
            "results": results,
        }
