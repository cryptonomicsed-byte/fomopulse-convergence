"""Who counts as a top trader, and what one of their entries is worth.

Ranking is deliberately composite and deliberately sceptical:
`followers` measures attention, not skill, so it is capped and always
outweighed by a demonstrated record. A wallet with no closed round trips has
not shown anything yet — it is open, not winning — and is treated as unknown
rather than good.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .client import Trader

# Component weights. These are judgements; the point of keeping them named is
# that a disagreement is a number to change, not a rewrite.
W_RECORD = 0.40        # closed round trips actually won
W_REALIZED = 0.25      # money taken out, not paper gains
W_ATTENTION = 0.20     # followers — real but easily gamed, so capped
W_CONVICTION = 0.15    # how much of their own book they are willing to hold

MAX_ATTENTION = 1_000_000.0
MIN_TRIPS_FOR_RECORD = 3


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def _log01(x: float, ceiling: float) -> float:
    """Compress a heavy-tailed quantity into 0..1. Attention and realized PnL
    are both power-law; comparing them raw would let one whale set the scale."""
    if x <= 0 or ceiling <= 0:
        return 0.0
    return _clamp01(math.log10(1.0 + x) / math.log10(1.0 + ceiling))


@dataclass
class WalletScore:
    handle: str
    address: str
    rank: int
    score: float                 # 0..1, how much this wallet's entry should count
    record: str                  # proven | unknown | weak
    followers: int
    win_rate: float
    trips: int
    realized: float
    detail: dict

    def to_dict(self) -> dict:
        return {
            "handle": self.handle,
            "address": self.address,
            "rank": self.rank,
            "score": round(self.score, 4),
            "record": self.record,
            "followers": self.followers,
            "win_rate": round(self.win_rate, 4),
            "trips": self.trips,
            "realized": round(self.realized, 2),
            **self.detail,
        }


class Roster:
    """The tracked-wallet set, scored. `unknown` wallets are kept — a new
    wallet entering alongside proven ones is information — but they carry the
    lowest weight, so they can never carry a signal on their own."""

    def __init__(self, traders: list[Trader]):
        self.traders = traders
        self._scores: dict[str, WalletScore] = {}
        self._by_handle: dict[str, WalletScore] = {}
        self._rank_ceiling = max((t.rank for t in traders if t.rank > 0), default=0)
        for t in traders:
            ws = self._score(t)
            if t.address:
                self._scores[t.address.lower()] = ws
            if t.handle:
                self._by_handle[t.handle.lower()] = ws

    def _score(self, t: Trader) -> WalletScore:
        proven = t.trips >= MIN_TRIPS_FOR_RECORD
        if proven and t.win_rate >= 0.5:
            record = "proven"
        elif proven:
            record = "weak"
        else:
            record = "unknown"

        # Rank is the tape's own ordering — lower is better, and it already
        # blends what the tape thinks matters.
        rank_term = 0.0
        if t.rank > 0 and self._rank_ceiling > 0:
            rank_term = _clamp01(1.0 - (t.rank - 1) / self._rank_ceiling)

        rec_term = _clamp01(t.win_rate) if proven else 0.35
        realized_term = _log01(max(t.realized, 0.0), 1e7)
        attention_term = _log01(t.followers, MAX_ATTENTION)
        conviction_term = _log01(t.open_value, 5e7)

        score = (
            W_RECORD * rec_term
            + W_REALIZED * realized_term
            + W_ATTENTION * attention_term
            + W_CONVICTION * conviction_term
        )
        # Blend toward the tape's own ranking so a wallet the tape has seen
        # enough of to place cannot be scored against what it measured.
        score = 0.7 * score + 0.3 * rank_term
        if record == "weak":
            score *= 0.6
        return WalletScore(
            handle=t.handle,
            address=t.address,
            rank=t.rank,
            score=_clamp01(score),
            record=record,
            followers=t.followers,
            win_rate=t.win_rate,
            trips=t.trips,
            realized=t.realized,
            detail={
                "rank_term": round(rank_term, 4),
                "realized_term": round(realized_term, 4),
                "attention_term": round(attention_term, 4),
                "conviction_term": round(conviction_term, 4),
            },
        )

    def score_for(
        self, address: str | None = None, handle: str | None = None
    ) -> WalletScore | None:
        if address and address.lower() in self._scores:
            return self._scores[address.lower()]
        if handle and handle.lower() in self._by_handle:
            return self._by_handle[handle.lower()]
        return None

    def weight(self, address: str | None = None, handle: str | None = None) -> float:
        """A wallet not on the roster still counts, at a floor — it is a real
        buyer, we just cannot place it. Zero weight would let an unknown wallet
        pile into a token invisibly."""
        ws = self.score_for(address, handle)
        return ws.score if ws else 0.15

    def top(self, n: int = 50, require_record: bool = True) -> list[WalletScore]:
        pool = [s for s in self._scores.values()]
        if require_record:
            pool = [s for s in pool if s.record == "proven"]
        pool.sort(key=lambda s: (-s.score, s.rank or 10**9))
        return pool[:n]

    def __len__(self) -> int:
        return len(self._scores)

    def summary(self) -> dict:
        rec: dict[str, int] = {}
        for s in self._scores.values():
            rec[s.record] = rec.get(s.record, 0) + 1
        return {
            "tracked": len(self._scores),
            "by_record": rec,
            "rank_ceiling": self._rank_ceiling,
        }
