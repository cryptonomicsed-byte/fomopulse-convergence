"""Composite scoring: turn convergence + attention + pool health into one
ranked signal with its evidence attached.

The score is a sum of named components, each 0..1 and each weighted. Nothing
is hidden behind a single opaque number: `Signal.components` carries every term
so a disagreement can be traced to the term that caused it, and the filters'
verdicts ride along so a veto is visible rather than a silent zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .convergence import (
    ACCELERATION_RATIO,
    ATTENTION_Z_THRESHOLD,
    EARLY_LAG_SECONDS,
    STRONG_CONVERGENCE_WALLETS,
    CONVERGENCE_MIN_WALLETS,
    TokenConvergence,
)
from .heuristics import FilterReport, evaluate_pool

# Component weights. Sum to 1.0 so a score reads as a percentage of "why this
# token", not an arbitrary scale.
W_CROWD = 0.32        # distinct buyers — the thing actually being asked for
W_QUALITY = 0.26      # who those buyers are (roster-weighted)
W_ATTENTION = 0.16    # fill-rate anomaly off its own baseline
W_ACCELERATION = 0.10 # did it just change
W_EARLINESS = 0.10    # did we get here before the move
W_FLOW = 0.06         # net buy pressure over the window

PENALTY_PER_FILTER = 0.12
DUST_PENALTY = 0.20


def _c01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


@dataclass
class Signal:
    token: str
    symbol: str
    name: str
    score: float
    verdict: str
    components: dict
    evidence: dict
    filters: FilterReport
    reasons: list[str] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        return self.verdict in ("strong", "watch") and not self.filters.blocked

    def to_dict(self) -> dict:
        return {
            "token": self.token,
            "symbol": self.symbol,
            "name": self.name,
            "score": round(self.score, 2),
            "verdict": self.verdict,
            "actionable": self.actionable,
            "components": {k: round(v, 4) for k, v in self.components.items()},
            "reasons": self.reasons,
            "filters": self.filters.to_dict(),
            "evidence": self.evidence,
        }

    # -- shapes the consumers already speak --------------------------------

    def as_cluster(self, now: int) -> dict:
        """Vantage alpha_clusters row shape (wallets_json + detected_at)."""
        import json

        return {
            "detected_at": now,
            "token": self.token,
            "symbol": self.symbol,
            "kind": "convergence",
            "score": round(self.score, 2),
            "wallets_json": json.dumps(self.evidence.get("wallets", [])),
            "meta_json": json.dumps(
                {
                    "distinct_buyers": self.evidence.get("distinct_buyers"),
                    "proven_buyers": self.evidence.get("proven_buyers"),
                    "attention_z": self.evidence.get("attention_z"),
                    "acceleration": self.evidence.get("acceleration"),
                    "verdict": self.verdict,
                }
            ),
        }

    def as_event(self) -> dict:
        """Vantage alpha_signals_log row shape (ts, kind, payload)."""
        import json

        return {
            "ts": self.evidence.get("last_ts") or 0,
            "kind": "convergence_signal",
            "payload": json.dumps(self.to_dict()),
        }


def _verdict(score: float, tc: TokenConvergence) -> str:
    if tc.distinct_buyers >= STRONG_CONVERGENCE_WALLETS and score >= 55:
        return "strong"
    if tc.distinct_buyers >= CONVERGENCE_MIN_WALLETS and score >= 35:
        return "watch"
    if score >= 20:
        return "note"
    return "noise"


def score_token(
    tc: TokenConvergence,
    roster,
    pool: dict | None = None,
    now: int | None = None,
) -> Signal:
    """Score one converged token. `pool` is a discover row (or nothing) — when
    absent the filters still run on whatever the fills carried."""
    import time as _t

    now = int(now or _t.time())
    pool = pool or {}

    # ── crowd: distinct buyers, saturating ────────────────────────────────
    crowd = _c01(tc.distinct_buyers / float(STRONG_CONVERGENCE_WALLETS))

    # ── quality: roster weight, normalised by the same wallet count ───────
    # Divided by distinct buyers so a big crowd of unknowns cannot outrank a
    # small crowd of proven wallets by sheer weight accumulation.
    quality = _c01(tc.weight_sum / max(1.0, tc.distinct_buyers * 0.85))

    # ── attention: z off its own baseline ─────────────────────────────────
    attention = _c01(tc.attention_z / (ATTENTION_Z_THRESHOLD * 2.0)) if tc.attention_z > 0 else 0.0

    # ── acceleration: did it just change ──────────────────────────────────
    accel = _c01((tc.acceleration - 1.0) / (ACCELERATION_RATIO - 1.0)) if tc.acceleration > 1.0 else 0.0

    # ── earliness: how soon after the pool opened we saw the crowd ────────
    lag = tc.early_lag
    if lag <= 0:
        earliness = 0.5  # pool predates our view; cannot claim early
    else:
        earliness = _c01(1.0 - lag / float(EARLY_LAG_SECONDS * 6))

    # ── flow: net buy pressure ────────────────────────────────────────────
    flow = _c01(tc.buy_ratio)

    comp = {
        "crowd": W_CROWD * crowd,
        "quality": W_QUALITY * quality,
        "attention": W_ATTENTION * attention,
        "acceleration": W_ACCELERATION * accel,
        "earliness": W_EARLINESS * earliness,
        "flow": W_FLOW * flow,
    }
    score = 100.0 * sum(comp.values())

    # ── pool metadata: the discover row wins where it has a value, the token's
    # own harvested fills fill every gap. A token is never vetoed for metadata
    # a different endpoint did not happen to cover.
    def _pick(key: str, harvested, default=None):
        v = pool.get(key)
        if v in (None, 0, 0.0, "", []):
            return harvested if harvested not in (None, 0, 0.0) else default
        return v

    merged_pool = {
        "liquidity": _pick("liquidity", tc.liquidity, 0.0),
        "volume24": _pick("volume24", tc.volume24, 0.0),
        "buys24": _pick("buys24", tc.buys24, tc.buy_fills),
        "sells24": _pick("sells24", tc.sells24, tc.sell_fills),
        "change24": pool.get("change24", tc.change24),
        "quoted_at": _pick("quoted_at", tc.quoted_at, 0),
        "fills": _pick("fills", tc.real_fills, tc.real_fills),
        "dusted": _pick("dusted", tc.dust_fills, tc.dust_fills),
        "wash": pool.get("wash", 0),
    }
    rep = evaluate_pool(merged_pool, now=now)

    for v in rep.penalties:
        score -= PENALTY_PER_FILTER * 100.0
    if tc.dust_fills and tc.real_fills:
        share = tc.dust_fills / (tc.dust_fills + tc.real_fills)
        if share > 0.25:
            score -= DUST_PENALTY * 100.0 * _c01(share)
    score = max(0.0, _c01(score / 100.0) * 100.0)
    if rep.blocked:
        score = min(score, 19.0)  # a vetoed token cannot rank actionable

    reasons = []
    reasons.append(f"{tc.distinct_buyers} distinct wallets bought ({tc.proven_buyers} with a record)")
    if tc.best_rank:
        reasons.append(f"best trader rank in the crowd: #{tc.best_rank}")
    if tc.attention_z >= ATTENTION_Z_THRESHOLD:
        reasons.append(
            f"attention {tc.attention_z:.1f} MADs above its own baseline"
            + (f" ({tc.attention_note})" if tc.attention_note else "")
        )
    elif tc.attention_note:
        reasons.append(f"attention: {tc.attention_note}")
    if tc.acceleration >= ACCELERATION_RATIO:
        reasons.append(f"recent fill rate {tc.acceleration:.1f}x the preceding median")
    lag = tc.early_lag
    if lag > 0:
        reasons.append(f"first tracked buy {lag}s after the pool opened")
    elif tc.pool_age_at_first_buy > 0:
        reasons.append("pool predates the window — earliness unknown, not claimed")
    elif not tc.pair_created_at:
        reasons.append("pool creation unknown to the tape — earliness unknown")
    if tc.new_positions:
        reasons.append(f"{tc.new_positions} new positions opened")
    if tc.dust_fills:
        reasons.append(f"{tc.dust_fills} dusting fills in the crowd's window")
    for v in rep.vetoes:
        reasons.append(f"VETO {v.name}: {v.detail}")
    for v in rep.penalties:
        reasons.append(f"penalty {v.name}: {v.detail}")

    return Signal(
        token=tc.token,
        symbol=tc.symbol,
        name=tc.name,
        score=score,
        verdict=_verdict(score, tc),
        components=comp,
        evidence=tc.to_dict(),
        filters=rep,
        reasons=reasons,
    )


def rank(
    tokens: dict[str, TokenConvergence],
    roster,
    pools: dict[str, dict] | None = None,
    now: int | None = None,
    limit: int = 50,
) -> list[Signal]:
    """Score everything that converged and return it best-first."""
    pools = pools or {}
    out = []
    for tc in tokens.values():
        if tc.distinct_buyers < CONVERGENCE_MIN_WALLETS:
            continue
        out.append(score_token(tc, roster, pools.get(tc.token), now=now))
    out.sort(key=lambda s: -s.score)
    return out[:limit]
