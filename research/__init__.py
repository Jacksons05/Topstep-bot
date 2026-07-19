"""Reusable research infrastructure for pre-registered OOS rounds.

Modules
-------
datasets  : loaders for every data source the account already owns ($0):
            Databento GLBX bars/MBO artifacts, SqueezeMetrics GEX, UW.
features  : session/bar/microstructure/regime feature engineering.
backtest  : realistic-cost bracket simulator + the standard stats kernel and
            PASS bar every round in HYPOTHESES.md has been judged with.

Design rules (learned the hard way — see HYPOTHESES.md Round 24):
  * enter at the NEXT bar's OPEN, never the signal bar's close
  * never exit on the entry bar itself
  * valid-geometry only: entry strictly between stop and target
  * both stop and target inside one bar  -> STOP (conservative)
  * costs are commission + N-tick slippage PER SIDE, always applied
  * every threshold must be causal (trailing windows, no full-sample stats)
"""
from __future__ import annotations

__all__ = ["datasets", "features", "backtest"]
