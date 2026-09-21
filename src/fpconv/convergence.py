"""The core: distinct-buyer convergence, entry earliness, and attention anomaly.

Two questions, deliberately kept separate because they can disagree and the
disagreement is itself information:

    convergence   how many *different* wallets are net-buying this token
                  ("a lot of people are buying the same thing")
    attention     how far the token's *recent* fill rate has moved off its own
                  immediately preceding baseline
                  ("a lot of users are interacting a lot")

A token can converge without an attention spike (slow broad accumulation) and
spike without converging (one whale, or a wash loop). The scorer weighs them
separately for that reason.

Attention is a two-window test, not a whole-window one. Bucketing a 24h window
into 5-minute bins gives 288 buckets holding about one fill each, so the last
bucket is usually empty and the z-score reads zero for everything — which looks
like "no anomalies" while actually meaning "no baseline". Instead the recent
tail is compared against the buckets immediately before it: a dense recent
window against a sparse preceding one is exactly the shape being hunted.

The z uses median/MAD rather than mean/stdev: one 50x wash candle would set a
mean-based baseline high enough to hide everything after it, which is the
manipulation this is meant to catch.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Iterable

# ── knobs ────────────────────────────────────────────────────────────────────

CONVERGENCE_MIN_WALLETS = 3
"""Distinct net buyers before it is a crowd rather than a coincidence."""

STRONG_CONVERGENCE_WALLETS = 6
"""Where a crowd becomes a stampede worth ranking above everything else."""

ATTENTION_Z_THRESHOLD = 3.0
"""Robust z at which a fill rate is off-pattern."""

MIN_BUCKETS_FOR_BASELINE = 6
"""Below this there is no baseline to be anomalous against, only history."""

ATTENTION_TAIL_BUCKETS = 3
"""Recent buckets averaged for the 'now' side. The median of the last few, not
the single last one: one unlucky gap would otherwise read as calm."""

ACCELERATION_RATIO = 2.5
"""Recent bucket rate over the preceding median. The second-derivative read:
not 'is this big' but 'did this just change'."""

EARLY_LAG_SECONDS = 600
"""A tracked wallet buying within this of pool creation is an early entry."""


@dataclass
class TokenConvergence:
    """Everything known about one token in one window."""

    token: str
    symbol: str = ""
    name: str = ""

    # convergence
    buyers: set[str] = field(default_factory=set)
    buyer_handles: list[str] = field(default_factory=list)
    sellers: set[str] = field(default_factory=set)
    net_buy_usd: float = 0.0
    buy_fills: int = 0
    sell_fills: int = 0
    dust_fills: int = 0
    real_fills: int = 0

    # weighted quality of the crowd
    weight_sum: float = 0.0
    best_rank: int = 0
    proven_buyers: int = 0

    # timing
    first_buy_ts: int = 0
    last_ts: int = 0
    window_start: int = 0
    pair_created_at: int = 0       # milliseconds, as the tape reports it
    new_positions: int = 0

    # pool metadata, harvested from the fills themselves so a token is never
    # judged unhealthy merely because no /api/discover row covered it
    liquidity: float = 0.0
    volume24: float = 0.0
    buys24: int = 0
    sells24: int = 0
    change24: float | None = None
    price: float = 0.0
    quoted_at: int = 0
    is_stock: bool = False
    dex: str = ""
    meta_seen: int = 0

    # attention
    buckets: list[int] = field(default_factory=list)
    bucket_seconds: int = 300
    attention_z: float = 0.0
    acceleration: float = 0.0
    attention_note: str = ""

    # per-wallet evidence
    wallet_events: list[dict] = field(default_factory=list)

    @property
    def distinct_buyers(self) -> int:
        return len(self.buyers)

    @property
    def buy_ratio(self) -> float:
        total = self.buy_fills + self.sell_fills
        return (self.buy_fills / total) if total else 0.0

    @property
    def pool_age_at_first_buy(self) -> int:
        """Seconds from pool creation to the first tracked buy. 0 when the pool
        creation is unknown; `early_lag` gates on that."""
        if not self.pair_created_at or not self.first_buy_ts:
            return 0
        return int(self.first_buy_ts - self.pair_created_at / 1000)

    @property
    def early_lag(self) -> int:
        """0 means 'unknown or predates view', which the scorer treats as
        neutral rather than early. A pool older than the window cannot be
        claimed as an early catch, and pretending otherwise would inflate it."""
        lag = self.pool_age_at_first_buy
        if lag <= 0:
            return 0
        if self.window_start and self.pair_created_at / 1000 < self.window_start:
            return 0  # pool predates the window entirely
        return lag

    def to_dict(self) -> dict:
        return {
            "token": self.token,
            "symbol": self.symbol,
            "name": self.name,
            "distinct_buyers": self.distinct_buyers,
            "distinct_sellers": len(self.sellers),
            "buyers": sorted(self.buyers),
            "buyer_handles": self.buyer_handles,
            "proven_buyers": self.proven_buyers,
            "weight_sum": round(self.weight_sum, 4),
            "best_rank": self.best_rank,
            "net_buy_usd": round(self.net_buy_usd, 2),
            "buy_fills": self.buy_fills,
            "sell_fills": self.sell_fills,
            "real_fills": self.real_fills,
            "dust_fills": self.dust_fills,
            "buy_ratio": round(self.buy_ratio, 4),
            "first_buy_ts": self.first_buy_ts,
            "last_ts": self.last_ts,
            "early_lag": self.early_lag,
            "pool_age_at_first_buy": self.pool_age_at_first_buy,
            "new_positions": self.new_positions,
            "attention_z": round(self.attention_z, 3),
            "acceleration": round(self.acceleration, 3),
            "attention_note": self.attention_note,
            "bucket_seconds": self.bucket_seconds,
            "bucket_fills": self.buckets,
            "liquidity": self.liquidity,
            "volume24": self.volume24,
            "buys24": self.buys24,
            "sells24": self.sells24,
            "change24": self.change24,
            "price": self.price,
            "quoted_at": self.quoted_at,
            "is_stock": self.is_stock,
            "dex": self.dex,
        }


# ── anomaly maths ────────────────────────────────────────────────────────────


def robust_z(values: list[float]) -> tuple[float, float]:
    """MAD z-score of the last element against the rest. Returns (z, mad)."""
    if len(values) < 2:
        return 0.0, 0.0
    head, last = values[:-1], values[-1]
    med = statistics.median(head)
    mad = statistics.median([abs(v - med) for v in head])
    if mad == 0:
        return 0.0, 0.0
    return (last - med) / (1.4826 * mad), mad


def robz_at(values: list[float], idx: int) -> tuple[float, float]:
    """z of `values[idx:]` (median) against `values[:idx]` (median/MAD)."""
    if idx <= 1 or idx >= len(values):
        return 0.0, 0.0
    head = values[:idx]
    tail = values[idx:]
    if len(tail) == 0 or len(head) < 2:
        return 0.0, 0.0
    med = statistics.median(head)
    mad = statistics.median([abs(v - med) for v in head])
    now = statistics.median(tail)
    if mad == 0:
        # A flat baseline with a different recent value is maximally
        # anomalous in direction, but the magnitude is unmeasurable. Report the
        # direction at the threshold rather than an invented number.
        if now == med:
            return 0.0, 0.0
        return (ATTENTION_Z_THRESHOLD if now > med else -ATTENTION_Z_THRESHOLD), 0.0
    return (now - med) / (1.4826 * mad), mad


def bucket_fills(timestamps: Iterable[int], bucket_seconds: int, now: int) -> list[int]:
    """Fill counts per bucket, oldest first, ending at `now`. Empty buckets are
    zeros, not gaps — an empty five minutes is a data point."""
    ts = [t for t in timestamps if t]
    if not ts:
        return []
    span = bucket_seconds
    oldest = min(ts)
    total = max(span, now - oldest)
    n = max(1, (total + span - 1) // span)
    n = min(n, 2000)  # bound the series; a month of 5s buckets is not a signal
    out = [0] * n
    for t in ts:
        idx = (now - t) // span
        if idx < 0:
            continue
        pos = n - 1 - int(idx)
        if 0 <= pos < n:
            out[pos] += 1
    return out


def attention_scores(buckets: list[int]) -> tuple[float, float, str]:
    """(z, acceleration, note) for a fill-count series.

    Baselines against the buckets *before* the tail, not the whole series, so a
    sparse 24h window still yields a real comparison instead of a zero.
    """
    if len(buckets) < MIN_BUCKETS_FOR_BASELINE:
        return 0.0, 0.0, f"only {len(buckets)} buckets — no baseline"

    idx = max(1, len(buckets) - ATTENTION_TAIL_BUCKETS)
    z, mad = robz_at([float(b) for b in buckets], idx)

    head = buckets[:idx]
    med = statistics.median(head) if head else 0.0
    tail = buckets[idx:]
    now_rate = statistics.median(tail) if tail else 0.0
    accel = (now_rate / med) if med > 0 else (float(now_rate) if now_rate else 0.0)

    note = ""
    if mad == 0:
        note = "flat baseline"
    if med == 0 and now_rate > 0:
        note = "first activity against an empty baseline"
    return z, accel, note


# ── the pass ─────────────────────────────────────────────────────────────────


def _harvest_meta(tc: TokenConvergence, f: dict) -> None:
    """Take pool metadata off a fill row.

    Every fill carries the pool's own figures (liquidity, volume24, buys24,
    sells24, change24, mark, quoted_at) — the tape measures them per row. Taking
    the freshest non-zero of each means a token is never judged on a missing
    /api/discover row, which is what falsely vetoed healthy convergences before
    this existed.
    """
    tc.meta_seen += 1
    liq = float(f.get("liquidity") or 0.0)
    if liq > 0:
        tc.liquidity = liq
    vol = float(f.get("volume24") or 0.0)
    if vol > 0:
        tc.volume24 = vol
    b = int(f.get("buys24") or 0)
    if b > 0:
        tc.buys24 = b
    s = int(f.get("sells24") or 0)
    if s > 0:
        tc.sells24 = s
    ch = f.get("change24")
    if ch is not None:
        try:
            tc.change24 = float(ch)
        except (TypeError, ValueError):
            pass
    price = float(f.get("mark") or f.get("price") or 0.0)
    ts = int(f.get("ts") or 0)
    if price > 0 and ts >= tc.quoted_at:
        tc.price = price
        tc.quoted_at = ts  # the fill's own timestamp is when we observed it
    if f.get("dex"):
        tc.dex = f.get("dex") or tc.dex
    if f.get("is_stock"):
        tc.is_stock = True
    pca = int(f.get("pair_created_at") or 0)
    if pca and not tc.pair_created_at:
        tc.pair_created_at = pca


def scan(
    fills: list[dict],
    roster,
    now: int,
    bucket_seconds: int = 300,
    include_stocks: bool = False,
    include_dust: bool = False,
    window_seconds: int = 86400,
) -> dict[str, TokenConvergence]:
    """Fold a tape window into per-token convergence, weighted by the roster."""
    out: dict[str, TokenConvergence] = {}
    per_token_ts: dict[str, list[int]] = {}
    window_start = now - window_seconds

    for f in fills:
        token = (f.get("token") or "").lower()
        if not token:
            continue
        if f.get("is_stock") and not include_stocks:
            continue

        tc = out.setdefault(
            token, TokenConvergence(token=token, window_start=window_start)
        )
        if not tc.symbol:
            tc.symbol = f.get("symbol") or ""
            tc.name = f.get("name") or ""

        _harvest_meta(tc, f)
        ts = int(f.get("ts") or 0)

        if f.get("is_dust"):
            # Dust counts toward the spray ratio — excluded from the crowd,
            # not from the evidence.
            tc.dust_fills += 1
            if not include_dust:
                continue

        per_token_ts.setdefault(token, []).append(ts)

        wallet = (f.get("wallet") or "").lower()
        handle = f.get("handle") or ""
        usd = float(f.get("usd") or 0.0)
        side = (f.get("side") or "").lower()

        tc.real_fills += 1
        tc.last_ts = max(tc.last_ts, ts)

        if side == "buy":
            tc.buy_fills += 1
            tc.net_buy_usd += usd
            if wallet:
                tc.buyers.add(wallet)
            if handle and handle not in tc.buyer_handles:
                tc.buyer_handles.append(handle)
            w = roster.weight(address=wallet, handle=handle)
            tc.weight_sum += w
            ws = roster.score_for(address=wallet, handle=handle)
            if ws:
                if ws.record == "proven":
                    tc.proven_buyers += 1
                if ws.rank and (tc.best_rank == 0 or ws.rank < tc.best_rank):
                    tc.best_rank = ws.rank
            if f.get("new_position"):
                tc.new_positions += 1
            if not tc.first_buy_ts or (ts and ts < tc.first_buy_ts):
                tc.first_buy_ts = ts
            tc.wallet_events.append(
                {
                    "wallet": wallet,
                    "handle": handle,
                    "ts": ts,
                    "usd": round(usd, 2),
                    "weight": round(w, 4),
                    "new_position": int(f.get("new_position") or 0),
                    "followers": int(f.get("followers") or 0),
                    "rank": int(f.get("rank") or 0),
                    "pnl_24h": round(float(f.get("pnl_24h") or 0.0), 2),
                }
            )
        elif side == "sell":
            tc.sell_fills += 1
            tc.net_buy_usd -= usd
            if wallet:
                tc.sellers.add(wallet)

    for token, tc in out.items():
        tc.bucket_seconds = bucket_seconds
        tc.buckets = bucket_fills(per_token_ts.get(token, []), bucket_seconds, now)
        z, accel, note = attention_scores(tc.buckets)
        tc.attention_z = z
        tc.acceleration = accel
        tc.attention_note = note

    return out


def converged(
    tokens: dict[str, TokenConvergence],
    min_wallets: int = CONVERGENCE_MIN_WALLETS,
) -> list[TokenConvergence]:
    """Only the tokens where a crowd formed, best-conviction first."""
    hits = [t for t in tokens.values() if t.distinct_buyers >= min_wallets]
    hits.sort(key=lambda t: (-t.weight_sum, -t.distinct_buyers, t.best_rank or 10**9))
    return hits
