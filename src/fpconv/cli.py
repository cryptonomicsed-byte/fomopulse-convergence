"""fpconv CLI.

    python -m fpconv health                  is the tape alive, and how stale
    python -m fpconv follow                  rebuild + review the top-trader roster
    python -m fpconv scan                    convergence + anomaly, ranked
    python -m fpconv scan --json              same, for a machine
    python -m fpconv paper                   dry-run what the paper trader would get
    python -m fpconv paper --execute          actually open PAPER positions
    python -m fpconv verify                   oracle coverage check (exit != 0 on a gap)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from .client import TapeClient, TapeError
from .convergence import scan as scan_fills
from .roster import Roster
from .signals import rank
from .store import Store
from .bridges import PaperTraderBridge, VantageBridge


def _pools_by_token(discover_rows: list[dict]) -> dict[str, dict]:
    return {(r.get("token") or "").lower(): r for r in discover_rows if r.get("token")}


def _window_seconds(window: str) -> int:
    return {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "30d": 2592000}.get(
        window, 86400
    )


def cmd_health(args) -> int:
    c = TapeClient(base=args.base)
    try:
        h = c.status()
    except TapeError as e:
        print(f"tape unreachable: {e}")
        return 2
    print("─" * 62)
    print(f"chain          {h.chain_id}")
    print(f"tracked         {h.wallets} wallets · {h.trades:,} trades")
    print(f"source         {h.source} · last block {h.last_block}")
    print(f"latency        median {h.latency_median_s:.2f}s · p90 {h.latency_p90_s:.2f}s")
    print(f"lag            {h.lag_seconds}s  {'FRESH' if h.fresh else 'STALE — signals would be late'}")
    print(f"uptime         {h.uptime_s}s")
    if args.json:
        print(json.dumps(h.raw, indent=1)[:2000])
    return 0 if h.fresh else 1


def cmd_follow(args) -> int:
    c = TapeClient(base=args.base)
    try:
        traders = c.traders(window=args.window, limit=args.roster_limit)
    except TapeError as e:
        print(f"tape unreachable: {e}")
        return 2
    r = Roster(traders)
    top = r.top(n=args.top, require_record=not args.include_unknown)
    print("─" * 62)
    print(f"roster: {r.summary()}")
    print("─" * 62)
    print(f"{'#':>4} {'handle':<22} {'rec':<8} {'score':>6} {'followers':>10} {'WR':>6} {'trips':>6} {'realized':>14}")
    for s in top:
        print(
            f"{s.rank:>4} {s.handle[:22]:<22} {s.record:<8} {s.score:>6.3f} "
            f"{s.followers:>10,} {s.win_rate:>6.1%} {s.trips:>6} {s.realized:>14,.0f}"
        )
    if args.store:
        st = Store(args.db)
        n = st.save_roster(int(time.time()), r.top(n=1000, require_record=False))
        print(f"\nstored {n} roster rows → {st.path}")
    if args.json:
        print(json.dumps([s.to_dict() for s in top], indent=1))
    return 0


def cmd_scan(args) -> int:
    c = TapeClient(base=args.base)
    st = Store(args.db)
    try:
        h = c.status()
    except TapeError as e:
        print(f"tape unreachable: {e}")
        return 2

    if args.refuse_stale and not h.fresh:
        print(f"refusing to scan: tape lag {h.lag_seconds}s exceeds the freshness bar")
        return 3

    run_id = st.start_run(args.window, h.lag_seconds)
    try:
        traders = c.traders(window=args.roster_window, limit=args.roster_limit)
        roster = Roster(traders)
        fills = c.tape(window=args.window, limit=args.limit, stocks=args.stocks, dust=False)
        pools = _pools_by_token(c.discover(window=args.discover_window, limit=200))
        now = int(time.time())
        tokens = scan_fills(
            fills, roster, now,
            bucket_seconds=args.bucket,
            include_stocks=args.stocks,
            window_seconds=_window_seconds(args.window),
        )
        sigs = rank(tokens, roster, pools=pools, now=now, limit=args.top)
    except TapeError as e:
        st.finish_run(run_id, 0, 0, 0, 0, h.raw, {}, error=str(e))
        print(f"scan failed: {e}")
        return 2

    written = st.save_signals(run_id, now, sigs)
    if args.store_roster:
        st.save_roster(now, roster.top(n=1000, require_record=False))
    converged = sum(1 for t in tokens.values() if t.distinct_buyers >= 3)
    st.finish_run(run_id, len(fills), len(tokens), converged, written, h.raw, roster.summary())

    if args.json:
        print(json.dumps(
            {"health": h.raw, "roster": roster.summary(),
             "signals": [s.to_dict() for s in sigs]},
            indent=1,
        ))
        return 0

    print("─" * 96)
    print(f"tape      lag {h.lag_seconds}s · {len(fills)} fills · {len(tokens)} tokens touched")
    print(f"roster    {roster.summary()}")
    print(f"converged {converged} tokens with >=3 distinct buyers · {written} scored")
    print("─" * 96)
    if not sigs:
        print("no convergence in this window.")
        return 0

    print(f"{'score':>6} {'verdict':<7} {'sym':<12} {'buyers':>6} {'prov':>5} {'z':>6} {'accel':>6} {'liq':>11} {'netUSD':>12}  why")
    for s in sigs[: args.show]:
        ev = s.evidence
        flag = "!" if s.filters.blocked else ("*" if s.actionable else " ")
        print(
            f"{s.score:>6.1f} {s.verdict:<7} {s.symbol[:12]:<12} "
            f"{ev.get('distinct_buyers', 0):>6} {ev.get('proven_buyers', 0):>5} "
            f"{ev.get('attention_z', 0):>6.1f} {ev.get('acceleration', 0):>6.1f} "
            f"{ev.get('liquidity', 0):>11,.0f} {ev.get('net_buy_usd', 0):>12,.0f}  {flag} {s.reasons[0] if s.reasons else ''}"
        )
        for extra in s.reasons[1:5]:
            print(f"{'':>6} {'':<7} {'':<12} {'':>6} {'':>5} {'':>6} {'':>6} {'':>11} {'':>12}    {extra}")
    print("─" * 96)
    print("! = a filter vetoed it   * = actionable   z = attention MADs off baseline")
    return 0


def cmd_paper(args) -> int:
    st = Store(args.db)
    br = PaperTraderBridge(args.paper_db, paper_usd=args.paper_usd)
    rep = br.schema_report()
    print("─" * 62)
    print(f"paper trader   {'FOUND' if rep.get('available') else 'MISSING'}  {rep.get('path')}")
    if rep.get("available"):
        print(f"  tables       {', '.join(rep.get('tables', []))}")
        print(f"  sidecar      {'present' if rep.get('has_sidecar') else 'will be created'}")
    vb = VantageBridge(
        args.vantage,
        tool_key=args.tool_key,
        allow_auto_execute=args.allow_auto_execute,
    )
    probe = vb.probe()
    print(
        f"vantage        {'UP' if probe.get('up') else 'DOWN'}  "
        f"ingest={probe.get('ingest_status')} {probe.get('ingest', '')}"
    )
    print(
        f"conviction     ceiling {probe.get('conviction_ceiling')} "
        f"(Vantage auto-orders above {VantageBridge.AUTO_EXECUTE_THRESHOLD})"
        f"{'  ⚠ AUTO-EXECUTE ARMED' if probe.get('auto_execute_armed') else ''}"
    )

    if not args.from_store:
        print("\n(nothing emitted — pass --from-store to emit the latest stored signals)")
        return 0

    import sqlite3
    conn = sqlite3.connect(str(st.path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM fpconv_signals ORDER BY emitted_at DESC, score DESC LIMIT ?",
        (args.top,),
    ).fetchall()
    conn.close()
    if not rows:
        print("\nno stored signals to emit — run `scan` first.")
        return 0

    payloads = []
    for r in rows:
        payloads.append(
            _StoredSignal(r)
        )
    out = br.emit(payloads, min_score=args.min_score)
    print(f"\nemit → {out}")
    if args.execute:
        ex = br.execute(payloads, min_score=args.execute_min)
        print(f"execute → {ex}")
    if args.push_vantage:
        pv = vb.push(payloads, limit=args.top)
        print(f"vantage push → {pv['pushed']} attempted")
        for r in pv["results"][:5]:
            print(f"  {r['token'][:12]} → {r['status']} {r['body'][:80]}")
    return 0


class _StoredSignal:
    """Rehydrate a stored row just enough for the bridges, which only need
    score / verdict / actionable / evidence / token / symbol / name."""

    def __init__(self, row):
        self.token = row["token"]
        self.symbol = row["symbol"] or ""
        self.name = row["name"] or ""
        self.score = row["score"] or 0.0
        self.verdict = row["verdict"] or "noise"
        self.actionable = bool(row["actionable"])
        self.evidence = {
            "distinct_buyers": row["distinct_buyers"],
            "proven_buyers": row["proven_buyers"],
            "best_rank": row["best_rank"],
            "attention_z": row["attention_z"],
            "acceleration": row["acceleration"],
            "early_lag": row["early_lag"],
            "net_buy_usd": row["net_buy_usd"],
            "price": row["price"],
            "buyer_handles": json.loads(row["wallets_json"] or "[]"),
        }
        self.reasons = json.loads(row["reasons_json"] or "[]")

        class _F:
            def __init__(self, d):
                self._d = d
            def to_dict(self):
                return self._d
            @property
            def blocked(self):
                return self._d.get("blocked", False)
        try:
            self.filters = _F(json.loads(row["filters_json"] or "{}"))
        except Exception:  # noqa: BLE001
            self.filters = _F({})

    def to_dict(self):
        return {
            "token": self.token, "symbol": self.symbol, "name": self.name,
            "score": self.score, "verdict": self.verdict,
            "actionable": self.actionable, "evidence": self.evidence,
        }


def cmd_verify(args) -> int:
    from .verify import verify_tape

    oracles = [o.strip() for o in args.oracle.split(",") if o.strip()] if args.oracle else None
    return verify_tape(
        window=args.window,
        limit=args.limit,
        oracles=oracles,
        tolerance=args.tolerance,
        allow_self_check=not args.no_self_check,
    )


def cmd_tls(args) -> int:
    """Inspect every endpoint the engine depends on, without trusting any of them.

    Deliberately not a 'fix' command: there is no flag here to disable
    verification or to trust an interceptor. The only honest outputs are
    "end-to-end" and "not end-to-end", and a pipeline carrying a tool key and
    trading signals should refuse to run on the latter.
    """
    from .tlscheck import inspect

    targets = [
        ("tape", args.base),
        ("vantage", args.vantage),
    ]
    if args.also:
        targets += [("extra", u.strip()) for u in args.also.split(",") if u.strip()]

    print("─" * 76)
    unsafe = 0
    for label, url in targets:
        host = url.split("://", 1)[-1].split("/")[0].split(":")[0]
        v = inspect(host)
        state = "END-TO-END" if v.safe else "NOT END-TO-END"
        if not v.safe:
            unsafe += 1
        print(f"{label:<9} {host:<28} {state}")
        print(f"          verified={v.verified}  intercepted={v.intercepted}")
        if v.cert:
            print(f"          issuer  : {v.cert.issuer[:100]}")
            print(f"          validity: {v.cert.not_after[:19]}")
        if v.reason:
            print(f"          {v.reason}")
        if v.interceptor:
            print(f"          INTERCEPTOR: {v.interceptor}")
        print()

    print("─" * 76)
    if unsafe:
        print(f"{unsafe} endpoint(s) NOT end-to-end.")
        print()
        print("This is reported, not bypassed. Verification is left ON and no")
        print("interceptor CA is installed — accepting a re-signed link on a")
        print("pipeline that carries an API key and trading signals is worse than")
        print("failing. Signals still scan and store locally; only transmission")
        print("is refused.")
        return 1
    print("all endpoints end-to-end.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fpconv", description="fomopulse convergence & anomaly engine")
    p.add_argument("--base", default="https://fomopulse.app", help="tape base URL")
    p.add_argument("--db", default=str(Store().path), help="local sqlite path")
    p.add_argument("--json", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("health", help="tape liveness and freshness")
    h.set_defaults(func=cmd_health)

    f = sub.add_parser("follow", help="build and review the top-trader roster")
    f.add_argument("--window", default="24h")
    f.add_argument("--top", type=int, default=40)
    f.add_argument("--roster-limit", type=int, default=294)
    f.add_argument("--store", action="store_true")
    f.add_argument("--include-unknown", action="store_true")
    f.set_defaults(func=cmd_follow)

    s = sub.add_parser("scan", help="convergence + anomaly scan")
    s.add_argument("--window", default="24h")
    s.add_argument("--discover-window", default="3d")
    s.add_argument("--roster-window", default="24h")
    s.add_argument("--limit", type=int, default=1000)
    s.add_argument("--roster-limit", type=int, default=294)
    s.add_argument("--bucket", type=int, default=300, help="attention bucket seconds")
    s.add_argument("--top", type=int, default=40)
    s.add_argument("--show", type=int, default=25)
    s.add_argument("--stocks", action="store_true", help="include tokenised stocks")
    s.add_argument("--store-roster", action="store_true")
    s.add_argument("--refuse-stale", action="store_true", default=True)
    s.set_defaults(func=cmd_scan)

    pp = sub.add_parser("paper", help="emit to the paper trader")
    pp.add_argument("--paper-db", default=str(PaperTraderBridge().path))
    pp.add_argument("--paper-usd", type=float, default=100.0)
    pp.add_argument("--vantage", default="https://omokoda.duckdns.org")
    pp.add_argument(
        "--tool-key",
        default=os.environ.get("VANTAGE_TOOL_INTEL", ""),
        help="X-Vantage-Tool-Key for the intel ingest (or env VANTAGE_TOOL_INTEL)",
    )
    pp.add_argument(
        "--allow-auto-execute",
        action="store_true",
        help=(
            "DANGEROUS: let conviction reach 1.0. Vantage auto-creates a REAL "
            "order above 0.7. Off by default; research signals stay under it."
        ),
    )
    pp.add_argument("--from-store", action="store_true")
    pp.add_argument("--execute", action="store_true")
    pp.add_argument("--push-vantage", action="store_true")
    pp.add_argument("--min-score", type=float, default=35.0)
    pp.add_argument("--execute-min", type=float, default=55.0)
    pp.add_argument("--top", type=int, default=20)
    pp.set_defaults(func=cmd_paper)

    v = sub.add_parser("verify", help="oracle coverage check")
    v.add_argument("--window", default="1h")
    v.add_argument("--limit", type=int, default=400)
    v.add_argument(
        "--oracle",
        default="",
        help="comma-separated independent tape URLs; empty uses the built-in list",
    )
    v.add_argument("--tolerance", type=float, default=0.98)
    v.add_argument(
        "--no-self-check",
        action="store_true",
        help="refuse to fall back to the tape checking itself",
    )
    v.set_defaults(func=cmd_verify)

    t = sub.add_parser("tls", help="inspect endpoints for TLS interception")
    t.add_argument("--vantage", default="https://omokoda.duckdns.org")
    t.add_argument("--also", default="", help="extra host URLs, comma-separated")
    t.set_defaults(func=cmd_tls)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
