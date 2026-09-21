# fpconv — top-trader convergence & anomaly engine

Follows the top traders on the fomopulse tape, finds where many different
wallets are buying the same token, flags attention anomalies, filters the
manipulation shapes, and emits signals to the paper trader and to Vantage's
trading intel.

Read-only. Holds no keys, signs nothing, moves no funds.

Built against the live tape — every number in "Live results" below came from a
real run, not a fixture.

```
chain   Robinhood Chain id 4663   (native ETH, quote tokens USDG + WETH)
tape    https://fomopulse.app     294 tracked wallets · 241k+ trades · ~0.9s median latency
```

## What it answers

| Question | Where |
|---|---|
| who are the top traders, and how much should their entry count? | `follow` |
| which tokens have many *different* wallets buying right now? | `scan` |
| which tokens just became unusually busy vs their own baseline? | `scan` (attention z) |
| are any of these actually a pump/wash/honeypot/dusting? | `scan` (filter report) |
| what would the paper trader do with this? | `paper` |
| is our tape actually complete? | `verify` |

## Install & run

```bash
cd ~/fomopulse-convergence
export PYTHONPATH=$PWD/src

python3 -m fpconv health              # tape liveness + freshness (lag)
python3 -m fpconv follow --top 20     # build + review the roster
python3 -m fpconv scan --window 24h   # convergence + anomaly, ranked
python3 -m fpconv paper --from-store  # dry-run what the paper trader gets
python3 -m fpconv verify              # oracle coverage check
python3 tests/test_engine.py          # 21 unit tests, no network
```

`scan --json` emits machine-readable output. `paper --execute` opens PAPER
positions (opt-in, see *Paper trader* below).

## The four ideas

**1. Convergence — "a lot of people are buying the same thing."**
Counts *distinct wallets* net-buying a token in the window, not fills. Four
buys from three wallets is three buyers. Each buyer contributes a weight from
the roster, so six unknown wallets rank below three proven ones.

**2. Attention anomaly — "a lot of users are interacting a lot."**
Fill rate is bucketed (default 5 min) and the recent tail is compared against
the buckets *before* it, using a median/MAD z-score.

Two things this gets right that a naive version does not:
- A **flat** series reports `flat baseline`, not a fake anomaly. A silent `0.0`
  and a genuine `0.0` are different facts and are reported differently.
- MAD, not stdev. One 50x wash candle would inflate a stdev baseline enough to
  hide everything after it — which is the manipulation being hunted.

**3. Manipulation filters.** Five shapes, ported from fomopulse's own
thresholds with their reasoning attached:

| filter | fires when | verdict |
|---|---|---|
| liquidity | pool depth < $10,000 | veto |
| churn | 24h volume / depth > 20x | veto |
| honeypot | ≥10 buys and **zero** sells | veto |
| spray | dustings / real fills > 5 | veto |
| wash | same-wallet buy+sell ≤300s apart at ≤5% size | **counted, never vetoed** |
| stale quote | quote older than 1h | veto |

Wash is deliberately not a veto: hiding it *is* the manipulation. A pool with
volume and no measurable depth vetoes rather than reading as churn `0.0`.

**4. Earliness.** Seconds from pool creation to the first tracked buy. A pool
that predates the window returns `0` = "unknown", never a large number dressed
up as a late entry, and the reason line says so explicitly.

## Scoring

`score = 100 × Σ(weight × component)`, all six components 0..1 and each one
kept in `Signal.components` so a disagreement traces to the term that caused it:

```
crowd        0.32   distinct buyers, saturating at 6
quality      0.26   roster-weighted, normalised per buyer
attention    0.16   MAD z above its own baseline
acceleration 0.10   recent rate ÷ preceding median
earliness    0.10   time from pool open to first tracked buy
flow         0.06   buy ratio
```

Verdicts: `strong` (≥6 buyers, ≥55) · `watch` (≥3 buyers, ≥35) · `note` (≥20) ·
`noise`. A filter veto caps the score at 19 so a vetoed token can never rank
actionable, but its score and reasons are still recorded — nothing is silently
dropped.

## Roster

294 wallets, scored on closed round trips actually won, realized PnL, followers
(capped — attention is not skill), open conviction, blended 70/30 toward the
tape's own ranking. `record` is `proven` (≥3 closed trips, win rate ≥50%),
`weak`, or `unknown`. A wallet with no closed trips is **unknown, not good**.
Wallets not on the roster still count, at weight 0.15 — zero would let an
unlisted wallet pile into a token invisibly.

## Live results (real runs)

```
tape      lag 156–229s · 1000 fills · 186 tokens touched
roster    294 tracked · 38 proven · 35 weak · 221 unknown

score verdict sym        buyers prov    z accel      liq     netUSD  why
 69.5 watch   DUEL            4   14  3.0   1.0   53,172      5,276  * attention 3.0 MADs above its own baseline
 68.2 strong  Nautilo        14   23  0.0   0.0  612,281    153,182  * 14 wallets (23 with a record), best rank #2
 67.9 strong  musebook       10    3  0.0   0.0  1,599,049   151,528  * 10 wallets, best rank #5
 64.5 strong  KEEL           18    2  0.0   0.0   53,565      3,839  * 18 wallets, first buy 166s after pool open
 57.8 watch   HOOD            4    4  0.0   0.0  182,555        497  * best rank #3
 55.9 watch   PONS            4    0  0.0   0.0  5,496,592  -136,482  * 4 wallets, net selling
```

Two bugs the first live run exposed and the tests now pin:
- **`attention_z` was 0.0 for everything.** Bucketing a 24h window into 5-min
  bins gives 288 buckets holding ~1 fill each, so the tail was always empty and
  every token read as calm. Fixed by tail-vs-preceding baselining — `DUEL` now
  correctly reports z=3.0.
- **Healthy convergences were vetoed on `liquidity $0`.** `HOOD`, `PONS`,
  `CASHCAT` and `RAIN` were rejected because no `/api/discover` row covered
  them. Every fill carries the pool's own metadata, so it is now harvested per
  token. `HOOD` recovered at $182k depth, `PONS` at $5.5M.

## Consumers

**Paper trader** (`~/ares_papertrade/trades.db`) — the bridge is **additive**.
Its `portfolio`/`trades` tables assume a Solana venue (they carry `size_sol`,
its positions are pump.fun mints); a Robinhood Chain ERC-20 is not that, and
silently minting paper entries for a venue the trader cannot price would
corrupt the books that give a paper trader its only value. So signals land in a
sidecar `fpconv_signals` table, and reach the live tables only via
`--execute`, above an explicit score floor, skipping already-open tokens, every
row tagged `signal_type='fpconv'` so they can be told apart and removed cleanly.

**Vantage** (`https://omokoda.duckdns.org`) — emits Vantage's own row shapes:
`alpha_clusters` (`detected_at`, `wallets_json`) and `alpha_signals_log`
(`ts`, `kind`, `payload`), which `backend/routers/alpha_hunter.py` already
reads. The conviction ordering matches `degen.py`'s `high-conviction`
(`SUM(copy_trade_score)` over distinct wallets) so a signal slots into that
list without re-ranking. The local store is always written first, so a signal
survives Vantage being down.

## Verification — and its honest limit

`verify` compares our tape against an independent one and **exits non-zero when
coverage falls short**. Three rules it originally got wrong, all now pinned:

1. **An unreachable oracle returned success.** It now returns 2. A check that
   passes when it cannot check is worse than no check.
2. **It compared two different universes.** The oracle publishes tokenised
   stocks by default and reads a broader wallet set; comparing against our
   stocks-excluded, 294-wallet tape reported every stock fill as a gap. The
   assertion is now scoped to wallets we actually track.
3. **A fallback could masquerade as verification.** When no independent oracle
   answers, the self-consistency check runs but is labelled
   `NOT INDEPENDENT — UNVERIFIED`.

Current state, reported as-is:

```
oracle fills  120  (6 by wallets we do not track — out of scope)
in scope      114
covered        87
coverage      76.32%  (bar 98%)   →  FAIL
```

**This does not pass, and the tolerance was not lowered to make it pass.** The
gaps are real: tokenised-stock dust (AAPL/AMD/IBM/GME/COST/SPY/MU at $1–7), plus
`PARE` ($6,474) and `RSTOCK` (×4 at $1,162). Either the two tapes disagree about
dust, or ours is genuinely missing fills. That is a finding, not a fixture.

## Layout

```
src/fpconv/
  client.py       tape reads (status/tape/discover/traders/bags/limits)
  heuristics.py   the five manipulation filters + their reasoning
  roster.py       wallet scoring, composite and sceptical
  convergence.py  distinct-buyer convergence + MAD attention anomaly
  signals.py      composite score, verdict, evidence, reasons
  store.py        SQLite in Vantage's shapes + the engine's own history
  bridges.py      paper trader (additive) + Vantage emitters
  verify.py       oracle coverage, honest about what it could not verify
  cli.py          health / follow / scan / paper / verify
tests/test_engine.py   21 tests, no network
```

## Ritual

```
*/5 * * * *  cd ~/fomopulse-convergence && PYTHONPATH=$PWD/src python3 -m fpconv scan --window 1h --top 10 >> ~/.fpconv/scan.log 2>&1
0 * * * *    cd ~/fomopulse-convergence && PYTHONPATH=$PWD/src python3 -m fpconv paper --from-store >> ~/.fpconv/paper.log 2>&1
0 6 * * *    cd ~/fomopulse-convergence && PYTHONPATH=$PWD/src python3 -m fpconv verify >> ~/.fpconv/verify.log 2>&1
```

`verify` in cron is the point: it will fail loudly on the day the tape goes
incomplete, instead of the signals quietly going stale.

Àṣẹ.

## TLS interception — reported, never bypassed

`fpconv tls` inspects every endpoint the engine depends on. On 2026-09-21 this
network was found re-signing two of them:

```
tape      fomopulse.app          NOT END-TO-END   issuer CN=FGT70FTK23012743, O=Fortinet
vantage   omokoda.duckdns.org    NOT END-TO-END   same appliance
extra     github.com             END-TO-END       Sectigo
extra     pypi.org               END-TO-END       GlobalSign
extra     cloudflare.com         END-TO-END       Google Trust Services
```

The obvious "fix" is to disable verification or install the appliance's CA.
**Both are refused here.** The first accepts a MITM on a link carrying an API
key and trading signals; the second legitimises interception for every host on
the device. There is no `--insecure` flag and a test asserts none can be added
quietly.

Signals still scan and store locally — only transmission is refused. That is
the reason the local store is written before any push.
