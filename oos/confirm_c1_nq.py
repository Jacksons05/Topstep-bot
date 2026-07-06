"""Round-3 confirmatory test: C1 overnight drift on unseen NQ 2010-2019.

Spec + PASS bar frozen in HYPOTHESES.md Round 3 before the data was pulled.

Usage:  .venv/bin/python oos/confirm_c1_nq.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from candidates import SPECS, c1_overnight_drift, evaluate, load  # noqa: E402

SPECS["NQ"] = {"pt": 20.0, "tick": 0.25, "comm_rt": 4.00}


def main() -> int:
    data = load("NQ")
    ts = data[0]
    trades = c1_overnight_drift(*data)
    cell = evaluate(trades, ts, "NQ")
    ok = bool(cell.get("n", 0) >= 1500 and (cell.get("pf") or 0) >= 1.10
              and cell.get("p_one_sided") is not None and cell["p_one_sided"] < 0.05
              and cell.get("p_bootstrap") is not None and cell["p_bootstrap"] < 0.05
              and cell.get("pct_years_positive", 0) >= 60)
    out = {"verdict": "PASS" if ok else "FAIL", "cell": cell}
    Path(__file__).resolve().with_name("confirm_c1_nq_results.json").write_text(
        json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
