"""Live read-only client for the fomopulse tape (Robinhood Chain id 4663).

Public, no auth, no keys. The tape's own numbers are measured on their side and
served here; the only optional thing behind an external session is the fomo
leaderboard card (handles/avatars), which this client deliberately does not use.

Freshness is a first-class return value: every read carries the tape's reported
lag so a consumer can refuse a stale signal instead of acting on it.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

DEFAULT_BASE = "https://fomopulse.app"
USER_AGENT = "fpconv/0.1 (+sovereign convergence engine)"

# The tape serves these window values; anything else is rejected server-side.
WINDOWS = ("1h", "6h", "24h", "7d", "30d", "all")


class TapeError(RuntimeError):
    """The tape refused, timed out, or answered with something unparseable."""


@dataclass
class TapeHealth:
    """What the tape says about itself. `lag_seconds` is the number that matters:
    a signal built from a tape that is an hour behind is an hour-old signal."""

    chain_id: int = 0
    wallets: int = 0
    trades: int = 0
    last_block: int = 0
    source: str = ""
    latency_median_s: float = 0.0
    latency_p90_s: float = 0.0
    lag_seconds: int = 0
    uptime_s: int = 0
    fetched_at: int = 0
    raw: dict = field(default_factory=dict)

    @property
    def fresh(self) -> bool:
        """Under five minutes of lag is the working bar; beyond that the tape is
        describing a market that has moved on."""
        return self.lag_seconds <= 300

    def age(self) -> int:
        return max(0, int(time.time()) - self.fetched_at) if self.fetched_at else -1


@dataclass
class Trader:
    """One tracked wallet. `rank` is the tape's own leaderboard position, lower
    is better; `trips`/`wins` are closed round trips, so win_rate is only
    meaningful once trips > 0 — an untripped wallet is not a winner, it is open."""

    handle: str
    address: str
    display_name: str = ""
    followers: int = 0
    rank: int = 0
    fills: int = 0
    tape_volume: float = 0.0
    realized: float = 0.0
    unrealized: float = 0.0
    trips: int = 0
    wins: int = 0
    open_value: float = 0.0
    open_tokens: int = 0
    tokens: int = 0
    clan: str | None = None
    verified: int = 0
    last_ts: int = 0

    @property
    def win_rate(self) -> float:
        return (self.wins / self.trips) if self.trips > 0 else 0.0

    @property
    def has_record(self) -> bool:
        """A trader with no closed trips has no demonstrated skill yet."""
        return self.trips >= 3

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["win_rate"] = round(self.win_rate, 4)
        d["has_record"] = self.has_record
        return d


class TapeClient:
    """Thin, dependency-free reader. `requests` is available in this
    environment but stdlib keeps the engine runnable anywhere the tape is."""

    def __init__(self, base: str = DEFAULT_BASE, timeout: int = 45):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self._calls = 0

    # ── transport ────────────────────────────────────────────────────────────

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base}{path}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            url = f"{url}?{urllib.parse.urlencode(clean)}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        self._calls += 1
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            raise TapeError(f"{path} → HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise TapeError(f"{path} → unreachable ({e})") from e
        except json.JSONDecodeError as e:
            raise TapeError(f"{path} → unparseable body ({e})") from e

    @property
    def calls(self) -> int:
        return self._calls

    # ── reads ────────────────────────────────────────────────────────────────

    def status(self) -> TapeHealth:
        d = self._get("/api/status")
        lat = d.get("latency") or {}
        return TapeHealth(
            chain_id=d.get("chain_id", 0),
            wallets=d.get("wallets", 0),
            trades=d.get("trades", 0),
            last_block=d.get("last_block", 0),
            source=d.get("source", ""),
            latency_median_s=float(lat.get("median", 0.0) or 0.0),
            latency_p90_s=float(lat.get("p90", 0.0) or 0.0),
            lag_seconds=int(d.get("lag_seconds", 0) or 0),
            uptime_s=int(d.get("uptime", 0) or 0),
            fetched_at=int(time.time()),
            raw=d,
        )

    def tape(
        self,
        window: str = "1h",
        limit: int = 500,
        stocks: bool = False,
        dust: bool = False,
        before: int | None = None,
        before_id: int | None = None,
    ) -> list[dict]:
        """Fills, newest first. `before`/`before_id` page backwards from a row
        already held — the cursor the tape's own site uses."""
        rows = self._get(
            "/api/tape",
            {
                "window": window,
                "limit": limit,
                "stocks": str(bool(stocks)).lower(),
                "dust": str(bool(dust)).lower(),
                "before": before,
                "beforeId": before_id,
            },
        )
        return rows if isinstance(rows, list) else []

    def discover(self, window: str = "3d", limit: int = 100) -> list[dict]:
        """Young pools a tracked wallet bought. Carries the per-buyer list, the
        wash count, the dusted count and the pool's own depth — everything the
        heuristics need, already measured on the tape's side."""
        rows = self._get("/api/discover", {"window": window, "limit": limit})
        return rows if isinstance(rows, list) else []

    def traders(self, window: str = "24h", limit: int = 200) -> list[Trader]:
        rows = self._get("/api/traders", {"window": window, "limit": limit})
        out = []
        for r in rows if isinstance(rows, list) else []:
            out.append(
                Trader(
                    handle=r.get("handle") or "",
                    address=(r.get("address") or "").lower(),
                    display_name=r.get("display_name") or "",
                    followers=int(r.get("followers", 0) or 0),
                    rank=int(r.get("rank", 0) or 0),
                    fills=int(r.get("fills", 0) or 0),
                    tape_volume=float(r.get("tape_volume", 0.0) or 0.0),
                    realized=float(r.get("realized", 0.0) or 0.0),
                    unrealized=float(r.get("unrealized", 0.0) or 0.0),
                    trips=int(r.get("trips", 0) or 0),
                    wins=int(r.get("wins", 0) or 0),
                    open_value=float(r.get("open_value", 0.0) or 0.0),
                    open_tokens=int(r.get("open_tokens", 0) or 0),
                    tokens=int(r.get("tokens", 0) or 0),
                    clan=r.get("clan"),
                    verified=int(r.get("verified", 0) or 0),
                    last_ts=int(r.get("last_ts", 0) or 0),
                )
            )
        return out

    def bags(self, window: str = "24h", limit: int = 100) -> list[dict]:
        rows = self._get("/api/bags", {"window": window, "limit": limit})
        return rows if isinstance(rows, list) else []

    def overview(self, window: str = "24h") -> dict:
        return self._get("/api/overview", {"window": window}) or {}

    def limits(self) -> dict:
        """The tape publishes its own operational knobs; echoing them back is how
        a consumer can tell whether the tape's judgement changed under it."""
        return self._get("/api/limits") or {}
