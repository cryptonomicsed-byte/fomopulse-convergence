"""Manipulation filters, ported from fomopulse's own thresholds.

Five shapes a pump wears, each with the reasoning kept beside the number — the
reasoning is the part worth having, because the thresholds are the tape's
judgement about its own market and may not transfer. Every filter returns a
verdict with its evidence so a caller can override one without losing the rest.

    churn      day's volume over pool depth — wash trading's fingerprint
    honeypot   many buys, no exit — the buy works for everyone, the sell for nobody
    spray      handouts per real fill — the ranking being bought
    wash       one wallet's buy and sell cancelling — volume that moved nothing
    stale      a quote old enough that a rugged pool still reads as a discovery

Two deliberate asymmetries, both inherited:
  * wash is *counted*, never a veto — hiding it would be the manipulation
  * spray is counted off this engine's own dust verdict, not the venue's
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ── thresholds (fomopulse's, named so they can be argued with) ───────────────

MIN_POOL_USD = 10_000.0
"""A pool under this is not a market. Under it, a quote is not a price but the
last dust that crossed an empty pool."""

MAX_CHURN = 20.0
"""Day's volume over pool depth. A pool turning over twenty times its own depth
in a day is the shape wash trading leaves behind."""

HONEYPOT_BUYS = 10
"""Buys a pool has to have taken before no sell at all is a fact about the token
rather than about its age. Under this many it is only early."""

MAX_SPRAY = 5.0
"""Handouts per real fill. A thousand dustings next to twenty buys is what a
convergence ranking gets played with."""

WASH_SECONDS = 300
WASH_TOLERANCE = 0.05
"""A buy and a sell by one wallet this close together and this near the same
size cancel: nothing moved and the tape carries the volume anyway."""

MAX_QUOTE_AGE = 3_600
"""A pool that rugs stops being answered for rather than answered badly: the card
keeps the depth, the volume and the change it had the hour it emptied, and every
one of those reads as a discovery. An hour of silence is the feed's answer."""

MAX_POOL_CHANGE_PCT = 100_000.0
"""Guard against a quote that is arithmetically impossible (division by a
near-zero prior price). Above this the pool's own history is unusable."""


@dataclass
class Verdict:
    """One filter's answer. `veto` means exclude outright; a filter that fires
    without vetoing is a penalty the scorer is free to weigh."""

    name: str
    fired: bool
    veto: bool
    detail: str
    value: float | None = None
    limit: float | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "fired": self.fired,
            "veto": self.veto,
            "detail": self.detail,
            "value": self.value,
            "limit": self.limit,
        }


@dataclass
class FilterReport:
    verdicts: list[Verdict] = field(default_factory=list)

    @property
    def vetoes(self) -> list[Verdict]:
        return [v for v in self.verdicts if v.fired and v.veto]

    @property
    def penalties(self) -> list[Verdict]:
        return [v for v in self.verdicts if v.fired and not v.veto]

    @property
    def blocked(self) -> bool:
        return bool(self.vetoes)

    def add(self, v: Verdict) -> None:
        self.verdicts.append(v)

    def to_dict(self) -> dict:
        return {
            "blocked": self.blocked,
            "vetoes": [v.to_dict() for v in self.vetoes],
            "penalties": [v.to_dict() for v in self.penalties],
            "all": [v.to_dict() for v in self.verdicts],
        }


# ── individual filters ───────────────────────────────────────────────────────


def check_liquidity(liquidity: float | None) -> Verdict:
    liq = float(liquidity or 0.0)
    return Verdict(
        name="liquidity",
        fired=liq < MIN_POOL_USD,
        veto=liq < MIN_POOL_USD,
        detail=f"pool depth ${liq:,.0f} vs floor ${MIN_POOL_USD:,.0f}",
        value=liq,
        limit=MIN_POOL_USD,
    )


def check_churn(volume24: float | None, liquidity: float | None) -> Verdict:
    vol = float(volume24 or 0.0)
    liq = float(liquidity or 0.0)
    if liq <= 0:
        # No depth to compare against: churn is undefined, not zero. Say so
        # rather than pass it, because a pool with volume and no depth is the
        # exact shape being hunted.
        return Verdict(
            name="churn",
            fired=vol > 0,
            veto=vol > 0,
            detail="volume with no measurable depth — churn undefined",
            value=None,
            limit=MAX_CHURN,
        )
    churn = vol / liq
    return Verdict(
        name="churn",
        fired=churn > MAX_CHURN,
        veto=churn > MAX_CHURN,
        detail=f"{churn:.1f}x pool depth in 24h vs ceiling {MAX_CHURN:.0f}x",
        value=round(churn, 3),
        limit=MAX_CHURN,
    )


def check_honeypot(buys: int | None, sells: int | None) -> Verdict:
    b = int(buys or 0)
    s = int(sells or 0)
    fired = b >= HONEYPOT_BUYS and s == 0
    return Verdict(
        name="honeypot",
        fired=fired,
        veto=fired,
        detail=(
            f"{b} buys and no exit at all — the buy works and the sell does not"
            if fired
            else f"{b} buys / {s} sells (needs >={HONEYPOT_BUYS} buys and zero exits to fire)"
        ),
        value=float(s),
        limit=float(HONEYPOT_BUYS),
    )


def check_spray(dusted: int | None, fills: int | None) -> Verdict:
    """Handouts per real fill. Counted off whichever dust verdict the caller
    trusts — pass the engine's own, not the venue's, where they differ."""
    d = int(dusted or 0)
    f = int(fills or 0)
    if f <= 0:
        fired = d > 0
        ratio = float(d) if d else 0.0
    else:
        ratio = d / f
        fired = ratio > MAX_SPRAY
    return Verdict(
        name="spray",
        fired=fired,
        veto=fired,
        detail=f"{d} dustings / {f} fills = {ratio:.1f} per fill vs ceiling {MAX_SPRAY:.0f}",
        value=round(ratio, 3),
        limit=MAX_SPRAY,
    )


def check_wash(wash: int | None) -> Verdict:
    """Never a veto. The count is the point: a token whose volume is mostly
    cancelling round trips should be shown that way, not hidden."""
    w = int(wash or 0)
    return Verdict(
        name="wash",
        fired=w > 0,
        veto=False,
        detail=f"{w} cancelling round trip(s) within {WASH_SECONDS}s at {WASH_TOLERANCE:.0%} size",
        value=float(w),
        limit=None,
    )


def check_quote_freshness(quoted_at: int | None, now: int | None = None) -> Verdict:
    import time as _t

    now = int(now or _t.time())
    q = int(quoted_at or 0)
    if q <= 0:
        return Verdict(
            name="stale_quote",
            fired=True,
            veto=False,
            detail="pool has no quote at all — depth and volume are unverifiable",
            value=None,
            limit=float(MAX_QUOTE_AGE),
        )
    age = now - q
    return Verdict(
        name="stale_quote",
        fired=age > MAX_QUOTE_AGE,
        veto=age > MAX_QUOTE_AGE,
        detail=f"quote {age}s old vs {MAX_QUOTE_AGE}s ceiling",
        value=float(age),
        limit=float(MAX_QUOTE_AGE),
    )


def check_dust_ratio(real_fills: int, dust_fills: int) -> Verdict:
    """The engine's own dust read: what share of the fills we are counting are
    handouts rather than purchases. Feeds the spray ratio when the venue does
    not report one."""
    total = real_fills + dust_fills
    if total <= 0:
        return Verdict("dust_ratio", False, False, "no fills to weigh", 0.0, None)
    share = dust_fills / total
    fired = share > 0.5
    return Verdict(
        name="dust_ratio",
        fired=fired,
        veto=fired,
        detail=f"{share:.0%} of fills are handouts ({dust_fills}/{total})",
        value=round(share, 4),
        limit=0.5,
    )


# ── the whole pass ───────────────────────────────────────────────────────────


def evaluate_pool(pool: dict, now: int | None = None) -> FilterReport:
    """Run every filter over a token's pool metadata.

    Accepts either a `/api/discover` row or a `/api/tape` fill, because both
    carry the same pool fields — that is deliberate on the tape's side and it
    means the filters run on either without a shim.
    """
    rep = FilterReport()
    rep.add(check_liquidity(pool.get("liquidity")))
    rep.add(check_churn(pool.get("volume24"), pool.get("liquidity")))
    rep.add(check_honeypot(pool.get("buys24"), pool.get("sells24")))
    rep.add(check_spray(pool.get("dusted"), pool.get("fills")))
    rep.add(check_wash(pool.get("wash")))
    rep.add(check_quote_freshness(pool.get("quoted_at"), now))

    # Implausible quote change is a data fault, not a market fact.
    ch = pool.get("change24")
    if ch is not None:
        try:
            chf = float(ch)
            if abs(chf) > MAX_POOL_CHANGE_PCT:
                rep.add(
                    Verdict(
                        "quote_sanity",
                        True,
                        True,
                        f"24h change of {chf:,.0f}% is not a market — quote unusable",
                        chf,
                        MAX_POOL_CHANGE_PCT,
                    )
                )
        except (TypeError, ValueError):
            pass
    return rep
