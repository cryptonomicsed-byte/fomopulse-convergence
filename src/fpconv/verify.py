"""Oracle coverage check — the discipline ported from fomopulse's verify-tape.

The original site is the oracle: every fill it published in the window has to
be in the tape we read too. This is the check that cannot be satisfied by prose
— it fails with a non-zero exit code and names what is missing.

Three rules this got wrong on its first pass, all fixed here because each one
would have produced a green light over an unverified tape:

  1. An unreachable oracle returned success. It now returns 2 and says so.
     A check that passes when it cannot check is worse than no check.
  2. There was one oracle. There is now a list, and which one answered is part
     of the output, so a passing run cannot be mistaken for a different run.
  3. When no external oracle answers, the self-consistency check is labelled
     NOT INDEPENDENT. It is still worth running — an internal contradiction is
     a real bug — but it must never be reported as verification.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

# Independent third-party tapes. Any one answering is enough; which one did is
# printed, because "coverage 100%" against a different oracle is a different
# claim.
ORACLES = [
    "https://robinhoodtrenches.com/api/tape",
]

OURS = "https://fomopulse.app"

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_UNVERIFIED = 2
EXIT_UNREACHABLE = 3


def _get(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers={"User-Agent": "fpconv-verify/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _window_seconds(window: str) -> int:
    return {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800}.get(window, 3600)


def _tx_key(f: dict) -> str:
    return (f.get("tx") or f.get("transaction") or "").lower()


def _ours(window: str, limit: int, stocks: bool = True) -> list[dict] | None:
    """Read our tape. `stocks=True` by default to match the oracle's own
    default: the reference tape publishes tokenised stocks unless asked not to,
    so reading ours with stocks excluded compares two different universes and
    reports every stock fill as a gap. That was a real false failure."""
    flag = "true" if stocks else "false"
    try:
        rows = _get(f"{OURS}/api/tape?window={window}&limit={limit}&stocks={flag}&dust=true")
        return rows if isinstance(rows, list) else []
    except Exception:  # noqa: BLE001
        return None


def _tracked_wallets() -> set[str]:
    """The wallets our tape claims to follow.

    The coverage assertion has to be scoped to these. The reference tape covers
    a broader wallet set than fomopulse's 294 fomo.family traders, so demanding
    every oracle fill would report a permanent false gap. The claim being
    tested is narrower and correct: *everything the oracle published that
    involves a wallet we track, we must also have.*
    """
    try:
        rows = _get(f"{OURS}/api/traders?window=24h&limit=500")
        if isinstance(rows, list):
            return {(r.get("address") or "").lower() for r in rows if r.get("address")}
    except Exception:  # noqa: BLE001
        pass
    return set()


def _coverage(
    oracle_name: str, oracle_fills: list[dict], ours: list[dict], tracked: set[str]
) -> tuple[float, list[dict], int]:
    """Coverage over the fills we are actually claiming to have."""
    have = {_tx_key(f) for f in ours if _tx_key(f)}

    in_scope = [
        f
        for f in oracle_fills
        if _tx_key(f) and (not tracked or (f.get("wallet") or "").lower() in tracked)
    ]
    out_of_scope = len([f for f in oracle_fills if _tx_key(f)]) - len(in_scope)

    missing = [f for f in in_scope if _tx_key(f) not in have]
    covered = len(in_scope) - len(missing)
    rate = (covered / len(in_scope)) if in_scope else 1.0
    return rate, missing, out_of_scope


def _self_consistency(window: str, ours: list[dict]) -> tuple[bool, list[str]]:
    """NOT independent. Checks the tape against itself: do the fills we read in
    a window agree with the totals the tape reports for that window, and does
    every fill carry the pool metadata this engine's heuristics depend on?

    A contradiction here is a real bug even though it is not verification, so
    it is reported — clearly labelled — rather than skipped.
    """
    problems: list[str] = []
    try:
        ov = _get(f"{OURS}/api/overview?window={window}")
    except Exception as e:  # noqa: BLE001
        return False, [f"overview unreachable ({e}) — cannot self-check"]

    reported = int(ov.get("fills") or 0)
    seen = len(ours)
    if reported and seen < reported:
        # A limit-bounded read is expected to be short; only flag a real
        # shortfall where we asked for at least as many as were reported.
        problems.append(
            f"read {seen} fills, tape reports {reported} in the same window "
            f"(expected if limit < reported)"
        )

    missing_meta = sum(1 for f in ours if not f.get("liquidity"))
    if ours and missing_meta / len(ours) > 0.5:
        problems.append(
            f"{missing_meta}/{len(ours)} fills carry no liquidity — "
            f"the heuristic layer would be running on incomplete metadata"
        )

    bad_side = sum(1 for f in ours if (f.get("side") or "") not in ("buy", "sell"))
    if bad_side:
        problems.append(f"{bad_side} fills have an unrecognised side")

    no_ts = sum(1 for f in ours if not f.get("ts"))
    if no_ts:
        problems.append(f"{no_ts} fills have no timestamp")

    return (len(problems) == 0), problems


def verify_tape(
    window: str = "1h",
    limit: int = 400,
    oracles: list[str] | None = None,
    tolerance: float = 0.98,
    allow_self_check: bool = True,
) -> int:
    oracles = oracles or ORACLES
    since = int(time.time()) - _window_seconds(window)

    print("─" * 62)
    print(f"window      last {window}   (since {since})")
    print(f"ours        {OURS}/api/tape")
    print(f"oracles     {len(oracles)}")
    print("─" * 62)

    ours = _ours(window, limit)
    if ours is None:
        print(f"FAIL — our tape unreachable at {OURS}")
        return EXIT_UNREACHABLE
    tracked = _tracked_wallets()
    print(f"our fills   {len(ours)}")
    print(f"tracked     {len(tracked)} wallets on our roster (the assertion's scope)")

    attempts: list[str] = []
    for url in oracles:
        print(f"\noracle      {url}")
        try:
            raw = _get(url)
        except urllib.error.HTTPError as e:
            print(f"  refused: HTTP {e.code}")
            attempts.append(f"{url} → HTTP {e.code}")
            continue
        except Exception as e:  # noqa: BLE001
            print(f"  unreachable: {type(e).__name__}: {e}")
            attempts.append(f"{url} → {type(e).__name__}")
            continue

        oracle = [
            f for f in (raw if isinstance(raw, list) else []) if int(f.get("ts") or 0) >= since
        ]
        if not oracle:
            print("  answered but published nothing in the window — unusable here")
            attempts.append(f"{url} → empty window")
            continue

        rate, missing, out_of_scope = _coverage(url, oracle, ours, tracked)
        print(f"  oracle fills  {len(oracle)}  ({out_of_scope} by wallets we do not track — out of scope)")
        print(f"  in scope      {len(oracle) - out_of_scope}")
        print(f"  covered       {len(oracle) - out_of_scope - len(missing)}")
        print(f"  coverage      {rate:.2%}  (bar {tolerance:.0%})")
        if missing:
            print("  missing (up to 20):")
            for f in missing[:20]:
                print(
                    f"    {_tx_key(f)[:18]} {f.get('side', '?'):<5} "
                    f"${float(f.get('usd') or 0):>10,.2f} {f.get('symbol') or '?'}"
                )
        ok = rate >= tolerance
        print("─" * 62)
        print(f"{'PASS' if ok else 'FAIL'} — against {url}")
        return EXIT_PASS if ok else EXIT_FAIL

    # Nothing independent answered.
    print("\n" + "─" * 62)
    print("NO INDEPENDENT ORACLE ANSWERED")
    for a in attempts:
        print(f"  {a}")
    print("─" * 62)

    if not allow_self_check:
        print("UNVERIFIED — nothing independent replied")
        return EXIT_UNVERIFIED

    print("falling back to SELF-CONSISTENCY — this is NOT independent verification,")
    print("it only proves the tape does not contradict itself.\n")
    ok, problems = _self_consistency(window, ours)
    if ok:
        print("self-consistency: OK (not verification)")
        print("─" * 62)
        print("UNVERIFIED — no independent oracle replied")
        return EXIT_UNVERIFIED
    print("self-consistency: PROBLEMS")
    for p in problems:
        print(f"  - {p}")
    print("─" * 62)
    print("FAIL — the tape contradicts itself")
    return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(verify_tape(*(sys.argv[1:2] or ["1h"])))
