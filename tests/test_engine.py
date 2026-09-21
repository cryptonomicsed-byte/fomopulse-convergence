"""Unit tests — the parts that must be right regardless of what the tape says.

The live API is a moving target, so nothing here calls it. These assert the
arithmetic and the honesty properties: that a flat series does not report a
fake anomaly, that a veto is a veto, and that a missing field cannot silently
become a healthy zero.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fpconv import convergence as C  # noqa: E402
from fpconv import heuristics as H  # noqa: E402
from fpconv.client import Trader  # noqa: E402
from fpconv.roster import Roster  # noqa: E402
from fpconv.signals import score_token  # noqa: E402


# ── heuristics ───────────────────────────────────────────────────────────────


def test_liquidity_floor_vetoes():
    assert H.check_liquidity(5_000).veto
    assert not H.check_liquidity(50_000).veto


def test_churn_fires_over_twenty_times_depth():
    assert not H.check_churn(volume24=100_000, liquidity=100_000).fired  # 1x
    assert H.check_churn(volume24=2_100_000, liquidity=100_000).veto  # 21x


def test_churn_with_no_depth_is_a_veto_not_a_zero():
    """A pool with volume and no measurable depth is the shape being hunted —
    it must not read as churn 0.0 and pass."""
    v = H.check_churn(volume24=50_000, liquidity=0)
    assert v.fired and v.veto, "volume with no depth must veto"


def test_honeypot_needs_both_conditions():
    assert not H.check_honeypot(buys=4, sells=0).fired, "too few buys to conclude"
    assert not H.check_honeypot(buys=50, sells=3).fired, "there are exits"
    assert H.check_honeypot(buys=50, sells=0).veto


def test_spray_ratio():
    assert not H.check_spray(dusted=10, fills=100).fired  # 0.1 per fill
    assert H.check_spray(dusted=600, fills=100).veto  # 6 per fill


def test_wash_is_counted_never_vetoed():
    v = H.check_wash(42)
    assert v.fired and not v.veto, "wash must be visible, not hidden"


def test_stale_quote_vetoes():
    now = 1_000_000
    assert not H.check_quote_freshness(now - 60, now).fired
    assert H.check_quote_freshness(now - 7200, now).veto
    assert H.check_quote_freshness(0, now).fired, "no quote is worth reporting"


def test_evaluate_pool_runs_every_filter():
    rep = H.evaluate_pool(
        {
            "liquidity": 50_000,
            "volume24": 100_000,
            "buys24": 40,
            "sells24": 10,
            "dusted": 0,
            "fills": 50,
            "wash": 2,
            "quoted_at": 0,
        },
        now=100,
    )
    names = {v.name for v in rep.verdicts}
    assert {"liquidity", "churn", "honeypot", "spray", "wash", "stale_quote"} <= names
    assert not rep.blocked, "a healthy pool with one stale quote must not be blocked"
    assert rep.penalties, "wash and stale_quote should still be recorded"


# ── attention maths ──────────────────────────────────────────────────────────


def test_flat_series_reports_no_anomaly_not_a_fake_one():
    z, accel, note = C.attention_scores([5] * 12)
    assert z == 0.0, "a flat series has no anomaly to report"
    assert note == "flat baseline"


def test_dense_recent_tail_is_an_anomaly():
    # eleven quiet buckets then three busy ones
    series = [1, 1, 2, 1, 1, 1, 1, 2, 1, 1, 9, 10, 11]
    z, accel, _ = C.attention_scores(series)
    assert z >= C.ATTENTION_Z_THRESHOLD, f"expected an anomaly, got z={z}"
    assert accel > C.ACCELERATION_RATIO


def test_short_series_refuses_to_claim_a_baseline():
    z, accel, note = C.attention_scores([1, 2, 3])
    assert z == 0.0 and accel == 0.0
    assert "no baseline" in note


def test_bucketing_places_fills_in_the_right_bucket():
    now = 1_000
    # one fill in the newest bucket, one three buckets back
    buckets = C.bucket_fills([now - 10, now - 610], bucket_seconds=300, now=now)
    assert buckets[-1] == 1
    assert buckets[-3] == 1


def test_robust_z_ignores_a_single_washed_candle():
    """The reason for MAD over stdev: one 50x print must not raise the baseline
    enough to hide everything after it."""
    series = [1.0] * 20 + [50.0] + [1.0, 1.0, 2.0]
    z, mad = C.robz_at(series, len(series) - 3)
    assert mad <= 1.0, f"a single spike must not inflate MAD, got {mad}"


# ── roster ───────────────────────────────────────────────────────────────────


def _trader(**kw):
    base = dict(
        handle="h", address="0xabc", display_name="", followers=0, rank=1, fills=0,
        tape_volume=0.0, realized=0.0, unrealized=0.0, trips=0, wins=0,
        open_value=0.0, open_tokens=0, tokens=0, clan=None, verified=0, last_ts=0,
    )
    base.update(kw)
    return Trader(**base)


def test_wallet_without_a_record_is_not_proven():
    r = Roster([_trader(handle="norecord", trips=0, wins=0, followers=500_000)])
    ws = r.score_for(handle="norecord")
    assert ws.record == "unknown", "followers are not a record"
    assert not ws.has_record if hasattr(ws, "has_record") else True


def test_proven_winner_outranks_proven_loser():
    r = Roster([
        _trader(handle="winner", address="0x1", trips=10, wins=9, rank=5, realized=50_000),
        _trader(handle="loser", address="0x2", trips=10, wins=1, rank=5, realized=-5_000),
    ])
    assert r.score_for(handle="winner").score > r.score_for(handle="loser").score


def test_unknown_wallet_has_a_floor_not_zero():
    """An unlisted wallet is a real buyer — zero weight would let it pile in
    invisibly."""
    r = Roster([_trader(handle="known", address="0x1", trips=5, wins=5)])
    w = r.weight(address="0xdeadbeef")
    assert 0 < w < 0.5, f"unknown weight should be a small floor, got {w}"


# ── the conviction ceiling (safety-critical) ─────────────────────────────────


def test_pushed_conviction_never_reaches_the_auto_execute_line():
    """Vantage auto-creates a REAL order above 0.7 conviction. A 0-100 research
    score must never map across that line by accident."""
    from fpconv.bridges import VantageBridge

    vb = VantageBridge(tool_key="x")
    assert vb.MAX_PUSH_CONVICTION < vb.AUTO_EXECUTE_THRESHOLD
    for score in (0, 19, 35, 55, 64.5, 69.5, 100):
        c = vb.conviction_for(score)
        assert c <= VantageBridge.MAX_PUSH_CONVICTION, f"score {score} → {c} crossed the line"
        assert c < vb.AUTO_EXECUTE_THRESHOLD, f"score {score} → {c} is at/over 0.7"


def test_perfect_score_maps_to_the_ceiling_not_beyond_it():
    from fpconv.bridges import VantageBridge

    vb = VantageBridge(tool_key="x")
    assert vb.conviction_for(100) == vb.MAX_PUSH_CONVICTION
    assert vb.conviction_for(1000) == vb.MAX_PUSH_CONVICTION, "clamped, not extrapolated"
    assert vb.conviction_for(-50) == 0.0


def test_auto_execute_requires_an_explicit_opt_in():
    from fpconv.bridges import VantageBridge

    assert VantageBridge(tool_key="x").conviction_for(100) < 0.7
    armed = VantageBridge(tool_key="x", allow_auto_execute=True)
    assert armed.conviction_for(100) == 1.0
    assert armed.conviction_for(90) > 0.7, "armed means armed, and it is named as such"


def test_conviction_is_always_within_vantage_contract():
    """Vantage hard-rejects anything outside 0..1."""
    from fpconv.bridges import VantageBridge

    for armed in (False, True):
        vb = VantageBridge(tool_key="x", allow_auto_execute=armed)
        for score in (-1, 0, 50, 100, 999):
            assert 0.0 <= vb.conviction_for(score) <= 1.0


def test_push_body_carries_the_mint_so_downstream_can_resolve_it():
    from fpconv.bridges import VantageBridge

    class _S:
        token = "0xabc123def456"
        symbol = "TEST"
        score = 60.0
        actionable = True
        reasons = ["r1", "r2"]
        evidence = {"net_buy_usd": 1234.0}

    body = VantageBridge(tool_key="x")._body(_S())
    assert body["mint"] == "0xabc123def456", "a ticker alone is not tradeable downstream"
    assert body["direction"] == "long"
    assert body["source"] == "fpconv"
    assert set(["symbol", "source", "type", "conviction"]) <= set(body)


def test_net_selling_convergence_is_not_a_long():
    from fpconv.bridges import VantageBridge

    class _S:
        token = "0x1"
        symbol = "SELL"
        score = 50.0
        actionable = True
        reasons = []
        evidence = {"net_buy_usd": -5000.0}

    assert VantageBridge(tool_key="x")._body(_S())["direction"] == "short"


# ── TLS interception detection ───────────────────────────────────────────────
# Offline assertions on the classification logic. The live check is `fpconv tls`.


def test_interception_markers_cover_the_appliance_actually_seen():
    """A FortiGate re-signed both pipeline hosts on 2026-09-21. It must be
    recognised by name, not just by 'verification failed'."""
    from fpconv import tlscheck

    seen = (
        "1.2.840.113549.1.9.1=support@fortinet.com,CN=FGT70FTK23012743,"
        "OU=Certificate Authority,O=Fortinet,L=Sunnyvale,ST=California,C=US"
    ).lower()
    assert any(m in seen for m in tlscheck.INTERCEPTION_MARKERS)


def test_real_public_ca_is_not_flagged_as_an_interceptor():
    from fpconv import tlscheck

    for issuer in (
        "CN=Sectigo Public Server Authentication CA DV E36,O=Sectigo Limited,C=GB",
        "CN=WE1,O=Google Trust Services,C=US",
        "CN=GlobalSign Atlas R3 DV TLS CA 2025 Q4,O=GlobalSign nv-sa,C=BE",
        "CN=R11,O=Let's Encrypt,C=US",
    ):
        low = issuer.lower()
        assert not any(m in low for m in tlscheck.INTERCEPTION_MARKERS), issuer


def test_a_verified_connection_is_never_reported_unsafe():
    """Even a trusted re-signer would be unsafe: a re-signed link is not
    end-to-end, whatever the device trusts."""
    from fpconv.tlscheck import TlsVerdict

    assert TlsVerdict("h", verified=True).safe
    assert not TlsVerdict("h", verified=True, intercepted=True).safe, (
        "interception makes a connection unusable regardless of trust"
    )
    assert not TlsVerdict("h", verified=False).safe


def test_there_is_no_flag_to_disable_verification():
    """The engine must not offer a bypass. Checked against the CLI surface so a
    well-meaning later change cannot quietly add one."""
    from fpconv.cli import build_parser

    p = build_parser()
    flags: list[str] = []
    for a in p._actions:
        flags += list(a.option_strings)
        choices = getattr(a, "choices", None)
        for sub in (choices.values() if isinstance(choices, dict) else []):
            for sa in getattr(sub, "_actions", []) or []:
                flags += list(sa.option_strings)
    bad = [
        f for f in flags
        if any(k in f.lower() for k in ("insecure", "no-verify", "noverify", "trust-ca", "verify-off"))
    ]
    assert not bad, f"a verification bypass appeared: {bad}"
    assert "--insecure" not in flags


# ── end to end on a synthetic fill set ───────────────────────────────────────


def _fill(token, wallet, side, usd, ts, **extra):
    f = {
        "token": token, "wallet": wallet, "side": side, "usd": usd, "ts": ts,
        "symbol": token[:6].upper(), "liquidity": 100_000.0, "volume24": 50_000.0,
        "buys24": 40, "sells24": 10, "fills": 50, "new_position": 1,
        "is_dust": 0, "is_stock": 0, "pair_created_at": 0,
    }
    f.update(extra)
    return f


def test_convergence_counts_distinct_wallets_not_fills():
    now = 10_000
    fills = [
        _fill("0xtok", "0xa", "buy", 100, now - 100),
        _fill("0xtok", "0xa", "buy", 100, now - 90),   # same wallet again
        _fill("0xtok", "0xb", "buy", 100, now - 80),
        _fill("0xtok", "0xc", "buy", 100, now - 70),
    ]
    tokens = C.scan(fills, Roster([]), now, window_seconds=3600)
    tc = tokens["0xtok"]
    assert tc.distinct_buyers == 3, "three wallets, four fills"
    assert tc.buy_fills == 4


def test_dust_is_excluded_from_the_crowd_but_kept_as_evidence():
    now = 10_000
    fills = [
        _fill("0xtok", "0xa", "buy", 100, now - 100),
        _fill("0xtok", "0xb", "buy", 100, now - 90),
        _fill("0xtok", "0xdust", "buy", 1, now - 80, is_dust=1),
    ]
    tc = C.scan(fills, Roster([]), now, window_seconds=3600)["0xtok"]
    assert tc.distinct_buyers == 2, "a dusting is not a buyer"
    assert tc.dust_fills == 1, "but it is evidence"


def test_missing_liquidity_in_fills_does_not_falsely_veto():
    """The bug the first live run exposed: a healthy convergence was vetoed
    because no /api/discover row covered the token."""
    now = 10_000
    fills = [
        _fill("0xtok", f"0x{i}", "buy", 500, now - 100 + i, liquidity=250_000.0)
        for i in range(5)
    ]
    tokens = C.scan(fills, Roster([]), now, window_seconds=3600)
    sig = score_token(tokens["0xtok"], Roster([]), pool=None, now=now)
    assert not sig.filters.blocked, f"should not be vetoed: {[v.to_dict() for v in sig.filters.vetoes]}"


def test_pool_that_predates_the_window_does_not_claim_earliness():
    now = 2_000_000
    old_pool_ms = (now - 500_000) * 1000  # pool opened long before the window
    fills = [
        _fill("0xtok", f"0x{i}", "buy", 500, now - 100 + i, pair_created_at=old_pool_ms)
        for i in range(4)
    ]
    tc = C.scan(fills, Roster([]), now, window_seconds=3600)["0xtok"]
    assert tc.early_lag == 0, "an old pool must not be claimed as an early catch"
    assert tc.pool_age_at_first_buy > 0, "but the real age is still available"


def test_price_reaches_the_evidence_so_the_paper_bridge_can_use_it():
    now = 10_000
    fills = [_fill("0xtok", f"0x{i}", "buy", 500, now - 10, mark=0.0042) for i in range(4)]
    tc = C.scan(fills, Roster([]), now, window_seconds=3600)["0xtok"]
    assert tc.to_dict()["price"] == 0.0042


def run_all():
    tests = [
        (n, f) for n, f in sorted(globals().items())
        if n.startswith("test_") and callable(f)
    ]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed.append(name)
            print(f"  FAIL  {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed.append(name)
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run_all())
