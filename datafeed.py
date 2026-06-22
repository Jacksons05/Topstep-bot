"""Historical data layer — your Databento alternative.

Primary path (free, your data): LiveRecorder taps the order-flow feed you ALREADY
stream over ProjectX SignalR and writes bar+L2 snapshots to partitioned parquet.
Over a few weeks this builds a proprietary tick/micro dataset no vendor sells —
and, crucially, it's the only way to get HISTORICAL order-flow features into the
model (FirstRateData et al. ship OHLCV only).

Bootstrap path: load_firstrate_csv reads a FirstRateData (or any OHLCV) export so
you can train a bar-only model on day one, before the recorder has accumulated.

Both converge on the canonical bars dict the rest of the codebase speaks:
    {"close": [...], "high": [...], "low": [...], "open": [...], "volume": [...]}
oldest→newest (same shape MarketData.bars returns).

Optional deps (lazy): pyarrow for parquet, duckdb for ad-hoc queries. Absent →
recorder degrades to JSONL append so you never lose data while wiring deps.
"""
from __future__ import annotations

import csv
import json
import logging
import time
from collections import defaultdict
from pathlib import Path

from config import CONFIG

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent

# canonical recorded columns (order is the parquet/JSONL schema)
RECORD_COLUMNS: tuple[str, ...] = (
    "ts", "symbol", "open", "high", "low", "close", "volume",
    "obi", "cvd", "micro_price", "bid", "ask", "whale", "cvd_div",
)


# ── live recorder (record-own ProjectX feed) ──────────────────────────────
class LiveRecorder:
    """Buffer bar+order-flow rows and flush to parquet (or JSONL fallback).

    Call `record(symbol, bars, oflow)` once per scan from the engine; it snapshots
    the latest bar and the live order-flow metrics together so a future retrain
    gets aligned micro features. Cheap: O(1) append, periodic flush.
    """

    def __init__(self, out_dir: str | None = None, flush_every: int = 200):
        self.dir = ROOT / (out_dir or CONFIG.record_path)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.flush_every = flush_every
        self._buf: list[dict] = []

    def record(self, symbol: str, bars: dict, oflow=None) -> None:
        """Append one row for `symbol` from the latest bar + order-flow engine.
        `oflow` is an orderflow.OrderFlowEngine (or None for bar-only capture)."""
        closes = bars.get("close") or []
        if not closes:
            return
        row = {
            "ts": time.time(),
            "symbol": symbol,
            "open": float((bars.get("open") or closes)[-1]),
            "high": float((bars.get("high") or closes)[-1]),
            "low": float((bars.get("low") or closes)[-1]),
            "close": float(closes[-1]),
            "volume": float((bars.get("volume") or [0.0])[-1]),
            "obi": float("nan"), "cvd": float("nan"), "micro_price": float("nan"),
            "bid": float("nan"), "ask": float("nan"), "whale": 0.0, "cvd_div": 0.0,
        }
        if oflow is not None and getattr(oflow, "has_data", False):
            div = oflow.cvd_divergence()
            row.update({
                "obi": float(oflow.obi),
                "cvd": float(oflow.cvd),
                "micro_price": float(oflow.micro_price),
                "bid": float(oflow.bid),
                "ask": float(oflow.ask),
                "whale": float(oflow.whale()),
                "cvd_div": 1.0 if div == "bullish" else (-1.0 if div == "bearish" else 0.0),
            })
        self._buf.append(row)
        if len(self._buf) >= self.flush_every:
            self.flush()

    def flush(self) -> None:
        """Persist the buffer. Parquet when pyarrow is present, else JSONL."""
        if not self._buf:
            return
        batch, self._buf = self._buf, []
        day = time.strftime("%Y%m%d", time.gmtime())
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
            table = pa.Table.from_pylist([{k: r[k] for k in RECORD_COLUMNS} for r in batch])
            path = self.dir / f"flow_{day}_{int(time.time())}.parquet"
            pq.write_table(table, path)
            log.debug(f"[recorder] wrote {len(batch)} rows → {path.name}")
        except ImportError:
            path = self.dir / f"flow_{day}.jsonl"
            with path.open("a") as fh:
                for r in batch:
                    fh.write(json.dumps(r) + "\n")
            log.debug(f"[recorder] pyarrow absent — appended {len(batch)} rows → {path.name}")


# ── loaders → canonical bars dict ──────────────────────────────────────────
def _empty_bars() -> dict:
    return {"open": [], "high": [], "low": [], "close": [], "volume": []}


def load_firstrate_csv(path: str | Path, *, has_header: bool = True) -> dict:
    """Load a FirstRateData / generic OHLCV CSV → canonical bars dict.

    Accepts either `datetime,open,high,low,close[,volume]` (FirstRateData) or a
    header naming those columns. Rows are assumed chronological (oldest→newest);
    if your export is newest-first, reverse it first.
    """
    p = Path(path)
    bars = _empty_bars()
    with p.open(newline="") as fh:
        reader = csv.reader(fh)
        rows = list(reader)
    if not rows:
        return bars
    start = 0
    col = {"open": 1, "high": 2, "low": 3, "close": 4, "volume": 5}
    if has_header:
        header = [h.strip().lower() for h in rows[0]]
        start = 1
        for name in col:
            if name in header:
                col[name] = header.index(name)
    for r in rows[start:]:
        try:
            bars["open"].append(float(r[col["open"]]))
            bars["high"].append(float(r[col["high"]]))
            bars["low"].append(float(r[col["low"]]))
            bars["close"].append(float(r[col["close"]]))
            bars["volume"].append(float(r[col["volume"]]) if len(r) > col["volume"] else 0.0)
        except (ValueError, IndexError):
            continue  # skip malformed / blank lines
    return bars


def load_recorded(symbol: str, in_dir: str | None = None) -> dict:
    """Reassemble recorded parquet/JSONL for `symbol` → canonical bars dict
    (+ the micro columns, returned under their own keys for L2-aware training)."""
    d = ROOT / (in_dir or CONFIG.record_path)
    rows: list[dict] = []
    if d.exists():
        try:
            import pyarrow.parquet as pq
            for f in sorted(d.glob("flow_*.parquet")):
                rows.extend(pq.read_table(f).to_pylist())
        except ImportError:
            pass
        for f in sorted(d.glob("flow_*.jsonl")):
            with f.open() as fh:
                rows.extend(json.loads(line) for line in fh if line.strip())
    rows = [r for r in rows if r.get("symbol") == symbol]
    rows.sort(key=lambda r: r.get("ts", 0.0))
    out = _empty_bars()
    out.update({"obi": [], "cvd": [], "micro_price": [], "whale": [], "cvd_div": []})
    for r in rows:
        for k in ("open", "high", "low", "close", "volume",
                  "obi", "cvd", "micro_price", "whale", "cvd_div"):
            out[k].append(r.get(k, float("nan")))
    return out


def load_history(symbol: str, *, csv_path: str | None = None) -> dict:
    """Single entry point train.py uses. Prefers a CSV bootstrap when given,
    else falls back to the recorded ProjectX dataset."""
    if csv_path:
        return load_firstrate_csv(csv_path)
    return load_recorded(symbol)
