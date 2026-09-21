#!/data/data/com.termux/files/usr/bin/bash
# fpconv watchdog — silent unless something actually matters.
#
# Runs a convergence scan and, separately, the oracle check. Prints ONLY when
# there is something a human should see:
#   * a strong convergence signal (>= 6 wallets, score >= 55)
#   * the verification failing (the tape went incomplete)
#   * the scan itself erroring
#
# Silence is the success signal. A watchdog that reports "all fine" every five
# minutes trains you to ignore it, which is how the failures in this ecosystem
# went unnoticed for days.
#
# Costs no tokens: cron runs it with no_agent.

set -uo pipefail

REPO="$HOME/fomopulse-convergence"
export PYTHONPATH="$REPO/src"
LOG_DIR="$HOME/.fpconv"
mkdir -p "$LOG_DIR"

STATE="$LOG_DIR/watchdog.state"
NOW=$(date +%s)

# ── 1. convergence scan ──────────────────────────────────────────────────────
SCAN=$(cd "$REPO" && timeout 180 python3 -m fpconv scan --window 1h --limit 800 \
        --top 10 --show 0 2>&1)
SCAN_RC=$?

ALERTS=""

if [ $SCAN_RC -ne 0 ]; then
  ALERTS+="fpconv scan FAILED (rc=$SCAN_RC)"$'\n'"$(echo "$SCAN" | tail -3)"$'\n\n'
fi

# Strong signals only. Read them back from the store so the format is stable.
STRONG=$(cd "$REPO" && timeout 60 python3 - <<'PY' 2>/dev/null
import sqlite3, os, json, time
db = os.path.expanduser("~/.fpconv/fpconv.db")
try:
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    since = int(time.time()) - 600
    rows = c.execute(
        "SELECT token,symbol,score,verdict,distinct_buyers,proven_buyers,best_rank,"
        "attention_z,net_buy_usd FROM fpconv_signals "
        "WHERE emitted_at > ? AND verdict='strong' AND actionable=1 "
        "ORDER BY score DESC LIMIT 5", (since,)).fetchall()
    for r in rows:
        print(f"  {r['symbol'][:14]:<14} score {r['score']:.0f}  "
              f"{r['distinct_buyers']} wallets ({r['proven_buyers']} proven)  "
              f"best rank #{r['best_rank'] or '-'}  z {r['attention_z']:.1f}  "
              f"net ${r['net_buy_usd']:,.0f}")
except Exception:
    pass
PY
)

if [ -n "$STRONG" ]; then
  ALERTS+="CONVERGENCE — multiple proven wallets piling into the same tokens:"$'\n'"$STRONG"$'\n\n'
fi

# ── 2. oracle verification, at most hourly ───────────────────────────────────
LASTV=$(cat "$LOG_DIR/lastverify" 2>/dev/null || echo 0)
if [ $((NOW - LASTV)) -gt 3600 ]; then
  VOUT=$(cd "$REPO" && timeout 180 python3 -m fpconv verify --window 1h --limit 400 2>&1)
  VRC=$?
  echo "$NOW" > "$LOG_DIR/lastverify"

  if [ $VRC -eq 1 ]; then
    # The tape went incomplete — that is exactly what this check exists for.
    DETAIL=$(echo "$VOUT" | grep -E "coverage|in scope|oracle fills" | head -4)
    ALERTS+="TAPE VERIFICATION FAILED — our tape no longer covers what the oracle published:"$'\n'"$DETAIL"$'\n'
    ALERTS+="  (signals derived from an incomplete tape are not trustworthy)"$'\n\n'
  elif [ $VRC -eq 3 ]; then
    ALERTS+="fpconv: our own tape is unreachable — signals are stale."$'\n\n'
  fi
  # rc 2 (no independent oracle) is expected when the reference site is down.
  # It is NOT an alert: reporting "couldn't verify" every hour is noise, and the
  # run is still recorded in the log for anyone who goes looking.
  echo "----- $(date -Is) verify rc=$VRC -----" >> "$LOG_DIR/verify.log"
  echo "$VOUT" >> "$LOG_DIR/verify.log"
fi

# ── 3. emit, so the paper trader stays current ───────────────────────────────
(cd "$REPO" && timeout 120 python3 -m fpconv paper --from-store \
   >> "$LOG_DIR/paper.log" 2>&1) || true

# Record the scan every time, whether or not it alerted.
echo "----- $(date -Is) scan rc=$SCAN_RC -----" >> "$LOG_DIR/scan.log"
echo "$SCAN" >> "$LOG_DIR/scan.log"

# ── output: only if there is an alert ────────────────────────────────────────
if [ -n "$ALERTS" ]; then
  echo "fpconv — $(date '+%Y-%m-%d %H:%M')"
  echo
  printf '%s' "$ALERTS"
  echo "detail: ~/.fpconv/{scan,verify,paper}.log"
fi
exit 0
