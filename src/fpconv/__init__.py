"""fpconv — top-trader convergence & anomaly engine over the fomopulse tape.

Read-only signal generation. Nothing here signs, holds keys, or moves funds.
Every signal is emitted as evidence (which wallets, what the filters said) so a
consumer can disagree with the score without re-deriving it from the chain.

Layers:
    client      live tape API (status/tape/discover/traders/bags)
    heuristics  manipulation filters — churn, honeypot, spray, wash, stale quote
    roster      who counts as a top trader, and how much their entry is worth
    convergence distinct-buyer convergence + robust attention anomaly
    signals     composite score, verdict, evidence
    store       SQLite persistence in Vantage's alpha_clusters/alpha_signals_log shape
    bridges     emit to the paper trader DB and to Vantage
"""

__version__ = "0.1.0"
