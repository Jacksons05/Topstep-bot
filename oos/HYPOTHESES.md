# Pre-registered out-of-sample test — Topstep-bot quant strategy

Registered 2026-07-03, BEFORE any external historical data was downloaded.
This file exists so no result can be accused of post-hoc pocket-picking.

## Background

In-sample trial (26 sessions of ProjectX 5-min bars, 2026-06-07 → 2026-07-03)
found one candidate pocket: **ES overnight** (n=71, PF 1.45, t=1.29, net of
1-tick slippage). Multiple-comparisons-corrected p = 0.337 → indistinguishable
from noise at that trial count. This test decides it on independent data.

## Data

Databento GLBX.MDP3, `ohlcv-1m`, continuous volume-rolled front contracts
(`ES.v.0`, `MNQ.v.0`, `MES.v.0`), aggregated to 5-min bars. ES history from
2010; MES/MNQ from 2019-05 (launch). The 2026-06-07 → 2026-07-03 window is
EXCLUDED from evaluation (it produced the hypothesis).

## Strategy under test (frozen — the bot's own code, live config)

`backtest_fast._quant_arrays` + `_simulate` verbatim: SMA(20/50)+RSI(14/30/70),
confidence threshold 0.75 (full alignment only), ATR(14) brackets 2×stop/3×target,
max_hold 24 bars (2h), one position at a time. Costs: round-trip commission
(ES $4.00, MES/MNQ $1.40) + 1-tick slippage both sides (sensitivity 0/2 ticks
reported but H1/H2 are judged at 1 tick).

## Hypotheses (one-sided, judged at 1-tick slippage, net)

- **H1 (primary):** ES overnight-session trades have positive mean net P&L.
  PASS: p < 0.05 (t-test AND 20k-bootstrap agree) over the full out-of-sample
  history, AND ≥60% of calendar years positive. Else FAIL.
- **H2 (secondary):** ES all-hours net total positive with PF ≥ 1.1.
- **H3 (secondary):** MNQ overnight mean net P&L positive, p < 0.05.

No other cells count. Anything else found in the grid is exploratory only and
inherits the same multiple-comparisons discount that killed the in-sample run.

## Verdict rule

- H1 passes → "ES overnight edge is real" survives; size it with the live
  regime playbook and re-trial.
- H1 fails → hunch confirmed for Topstep-bot too: no demonstrated edge
  anywhere; strategy layer needs replacement, not more data.

---

# Round 2 — replacement-signal candidates (registered 2026-07-04, before running)

Same data (oos/data/*.csv, ends 2026-06-06), same costs (comm + slippage,
judged at 1 tick), same kernel conventions (signals act on bar close, entry at
next bar close, no look-ahead). Parameters below are frozen as written; no
post-hoc tuning. All times ET.

## C1 — Overnight drift
Buy 1 ES at the 16:00 bar close; exit at the next session's first bar closing
at/after 09:30. One trade per day. No stop (overnight gap risk is the premium).
Judged on ES; MES/MNQ exploratory.

## C2 — Opening-range breakout (ORB 30-min)
Range = high/low of 09:30–09:55 bars. From 10:00 to 12:00: first 5-min close
above range-high → long next bar close; below range-low → short. Stop = far
side of the range. No target; flatten at 15:55. Max one trade/day.

## C3 — VWAP reversion (RTH)
Session VWAP = cum(TP×V)/cum(V) from 09:30 (TP=(H+L+C)/3). Entry window
10:00–15:30: close deviates from VWAP by >2× the 20-bar rolling σ of
(close−VWAP) → fade toward VWAP. Exit: price crosses VWAP, or 2×ATR(14) stop,
or 15:55 flatten. One position at a time.

## PASS bar (each candidate, judged on ES at 1-tick slippage, net)
n ≥ 200, PF ≥ 1.15, one-sided p < 0.05 (t AND 20k bootstrap),
≥ 60% of calendar years positive. Fail any → candidate dead, no re-tuning.

---

# Round 3 — C1 confirmation on unseen data (registered 2026-07-04, before pull)

Round 2 exploratory finding: C1 (overnight drift) on MNQ 2019-2026 printed
PF 1.159, t=2.12, p=0.017, 87.5% years positive — but in an UNREGISTERED cell.
Confirmatory test on data no analysis has touched: full-size NQ, 2010-05-01 →
2019-05-05 (the period the micro didn't exist). Same C1 spec verbatim
(buy 16:00 bar close → exit next session first bar ≥09:30, no stop). NQ specs:
$20/pt, 0.25 tick, $4.00 comm RT, judged at 1-tick slippage.

PASS bar: n ≥ 1500, PF ≥ 1.10, one-sided p < 0.05 (t AND bootstrap),
≥ 60% of years positive. PASS → overnight-drift edge confirmed on two
independent samples; wire into bot (sim) sized for Topstep DLL. FAIL → dead,
report no-edge-found, do not go fishing further.

---

# Round 4 — C1b: Topstep-compliant overnight drift (registered 2026-07-04)

Constraint discovered post-Round-3: Topstep requires flat by ~16:10 ET, so the
C1 16:00 entry violates account rules. Exogenous modification (rule-driven,
not performance-driven): entry moved to the 18:00 ET Globex-reopen bar close;
exit unchanged (next first bar ≥09:30). Tested on BOTH prior samples with full
disclosure that they are re-used; the change is not data-mined.

PASS bar: on EACH of NQ 2010-2019 and MNQ 2019-2026 separately —
PF ≥ 1.10, one-sided p < 0.05 (t AND bootstrap), ≥ 60% years positive.
PASS → ship C1b (service entry window 18:00-18:15 ET). FAIL → do not ship;
overnight service stays off.

---

# Round 5 — order flow as ENTRY DRIVER (registered 2026-07-04, before pull)

Data: ES mbp-10 (10-level book + trades), GLBX.MDP3, 2026-05-06 → 2026-06-06
(~$41 credits; one month — disclosed limitation: single regime, so PASS here
is promising-not-proven and requires a second independent month before any
shipping; FAIL at tick-level n is a strong kill).

Features (1-second samples): OBI10 = (Σbid_sz − Σask_sz)/(Σbid_sz + Σask_sz)
over 10 levels; z = trailing 30-min z-score of OBI10; CVD5 = signed trade
volume, trailing 5 min (aggressor side from trade records).

Signal (mirrors the bot's live gate params, OBI_Z=1.5 + direction agreement):
z ≥ +1.5 AND CVD5 > 0 → LONG. z ≤ −1.5 AND CVD5 < 0 → SHORT.

Execution: enter at next snapshot CROSSING THE SPREAD (buy ask / sell bid);
exit exactly 5 minutes later crossing the spread again. One position at a
time. Costs: $4.00 RT commission; the crossed spread is the slippage (0 extra
ticks judged; 1-tick sensitivity reported). Cells: all-hours judged; RTH and
overnight reported.

PASS bar: n ≥ 1000, PF ≥ 1.10, one-sided p < 0.05 (t AND 20k bootstrap).

---

# Round 6 — GEX-regime conditioning (registered 2026-07-04, before merge)

Mechanism (stated before testing): positive dealer net gamma → dealers fade
moves → mean reversion favored; negative net gamma → dealers chase moves →
trend/breakout favored. Conditioning variable: prior day's SPX (fallback SPY)
net-GEX sign from UW greek-exposure, mapped to ES trade dates (NDX/QQQ → MNQ
exploratory). Strategy specs FROZEN from Round 2 — C3 VWAP-reversion and
C2 ORB verbatim, judged at 1-tick slippage, over whatever window UW history
covers.

- H-R6a: C3 (VWAP reversion) on ES restricted to POSITIVE-GEX days:
  n ≥ 100, PF ≥ 1.10, one-sided p < 0.05 (t AND bootstrap).
- H-R6b: C2 (ORB) on ES restricted to NEGATIVE-GEX days: same bar.
- Consistency check (reported, not judged): each strategy on the opposite
  regime should be no better than its unconditional result.

PASS on either → extend UW window / second sample before shipping (this is
one short window, ~1yr). FAIL both → GEX-sign daily conditioning is dead at
this granularity; remaining UW angle would be intraday levels (walls/flip),
which needs intraday GEX history we do not have.

---

# Round 7 — pooled index overnight drift (registered 2026-07-04, before running)

Mechanism: the overnight premium is an index-wide phenomenon; pooling the
frozen C1 spec (16:00 close → next 09:30) across ES 2010-2026 + NQ 2010-2019 +
MNQ 2019-2026 (all already generated, dollar P&L per 1 contract, 1-tick slip)
is the test the mechanism actually implies. Pooling normalization: each
trade's net USD divided by its contract point value ($50/$20/$2) → net POINTS
per unit, so no instrument dominates. PASS: pooled n ≥ 5000, PF ≥ 1.10,
one-sided p < 0.05 (t AND 20k bootstrap), ≥ 60% of calendar years positive.

# Round 8 — C1b with honest passive entry (registered 2026-07-04)

Round 4's Topstep-legal 18:00 taker entry failed the recent sample (MNQ
PF 1.09). Hypothesis: taker costs, not the premium, caused the failure.
Frozen spec: place a BUY LIMIT at the 18:00 ET bar close price; count a fill
ONLY if a bar in 18:05–20:00 ET trades strictly through it (bar low < limit);
unfilled → no trade that night. Entry at limit price, $4.00 comm RT (ES-scale;
$1.40 micros), exit unchanged (next first bar ≥09:30, taker, 1-tick slip on
exit only). Adverse selection is captured automatically (only dip-nights
fill). Judged like Round 4 on BOTH NQ 2010-19 and MNQ 2019-26: each PF ≥ 1.10,
one-sided p < 0.05 (t AND bootstrap), ≥ 60% years positive. Fill fraction
reported; if < 40% of nights fill, flag sample-selection concern regardless
of PASS/FAIL.

# Round 9 — UW options-flow → next-day ES (registered 2026-07-04, before pull)

Mechanism: informed SPX/SPY options flow predicts next-session index drift.
Signal: SPY day-D flow score from UW net-prem-ticks (JARVIS backtest_flow.py
`day_flow_score` formula, frozen as-is), computed from D's full session.
Trade: score > +0.15 → BUY ES at D 18:00 bar close; score < −0.15 → SELL;
exit next day's 16:00 bar close (taker both sides, 1-tick slip, $4 comm).
Topstep-legal window. History: as far back as UW serves net-prem-ticks.
PASS: n ≥ 100, PF ≥ 1.10, one-sided p < 0.05 (t AND 20k bootstrap).

---

# Round 10 — intraday time-series momentum (registered 2026-07-04, before running)

Mechanism: Gao-Han-Li-Zhou (2018) "Market intraday momentum" — the first
half-hour return predicts the last half-hour return (documented on SPY,
1993-2013, driven by late-informed trading + hedging flows). Frozen spec on
our ES 5-min data 2010-2026: r_open = (10:00 bar close − 09:30 bar open);
if r_open > 0 → BUY at 15:30 bar close; r_open < 0 → SELL; exit at 15:55 bar
close (Topstep-legal). Costs $4.00 RT + 1-tick slip both sides. NQ/MNQ
exploratory. PASS on ES: n ≥ 1000, PF ≥ 1.10, one-sided p < 0.05 (t AND 20k
bootstrap), ≥ 60% years positive.

# Round 11 — overnight gap fade (registered 2026-07-04, before running)

Mechanism: large overnight gaps in index futures partially revert intraday
(liquidity-provision premium at the open). Frozen spec on ES 5-min 2010-2026:
gap = 09:30 bar open − prior day 16:00 bar close; if |gap| > 0.3% of price →
enter AGAINST gap sign at 09:30 bar close, exit at 12:00 bar close. Costs
$4.00 RT + 1-tick slip both sides. PASS on ES: n ≥ 500, PF ≥ 1.10, one-sided
p < 0.05 (t AND bootstrap), ≥ 60% years positive.

# Family-wise disclosure (rounds 7-12)
~10 registered hypotheses at p<0.05 → ~0.5 expected false positives across
the family. Any single PASS therefore requires an unseen-data confirmation
(new period, instrument, or forward paper) before shipping, as with Round 3.

---

# Round 12 — cross-asset overnight drift (registered 2026-07-04, before pull)

Mechanism transfer test of the confirmed index overnight premium: unseen
instruments, frozen C1 spec verbatim (16:00 close → next 09:30, no stop) AND
frozen C1b passive variant (18:00 limit, strict trade-through fill 18:05-20:00).
Instruments within remaining free credits: RTY (2017-2026, $10.73) and
GC (2010-2026, $20.24). CL and YM ($39.83 combined) DEFERRED pending user
funding approval. Specs: RTY $50/pt 0.10 tick $4.00 comm; GC $100/pt 0.10
tick $4.00 comm. PASS per instrument: n ≥ 1000, PF ≥ 1.10, one-sided p < 0.05
(t AND 20k bootstrap), ≥ 60% years positive. Note: GC "16:00/09:30" windows
are equity-session anchors applied to a metals contract — mechanism may not
transfer; that is the test.

## Round 12 — results (2026-07-06)

C1 (taker, 16:00→09:30 hold — NOT Topstep-legal): RTY **PASS** (n=2218,
PF 1.115, p=0.0402 t / 0.0407 bootstrap, 80% years positive) — but 2025
($152) and 2026 (−$1,629) are flat-to-negative, i.e. the edge is weakest in
the two most recent years, the pattern you'd expect from a marginal
family-wise false positive rather than a strengthening real effect. GC
**FAIL** (p=0.060, wildly regime-dependent yearly P&L — 2012/2013/2021/2022
deeply negative, 2020/2025 hugely positive — not a stable edge).

C1b (passive limit, 18:00 entry — the ONLY Topstep-legal variant): RTY
**FAIL** (n=1753, PF 1.059, p=0.199 t / 0.198 bootstrap, 2025 alone lost
$10,299). GC **FAIL** (PF 1.003, p=0.481, 47% years positive — no edge at
all).

**Portfolio-level finding (not itself a new hypothesis — a synthesis of
Rounds 4, 8, and 12):** every Topstep-legal (18:00-entry, C1b) variant of
the overnight-drift family tested to date has FAILED — NQ (Round 4, PF 1.091
p=0.12), MNQ (Round 8), and now RTY (this round). GC's C1b also failed. That
is 4/4. Only the non-compliant taker/C1 variant (16:00 entry, holds through
Topstep's mandatory 16:10–18:00 flatten window) has ever passed, and only on
Nasdaq-linked instruments (NQ, MNQ) plus one marginal, decaying RTY result.

**Implication for CL/YM (deferred pending funding):** given a 0/4 base rate
for the Topstep-legal variant across four instruments spanning three asset
classes (Nasdaq, small-cap, metals), the prior that CL or YM's C1b clears
the bar is now low. Recommend NOT spending the $39.83 in Databento credits
on Round 12's CL/YM leg — expected information value is poor. The overnight-
drift family should be considered EXHAUSTED for Topstep-compliant trading;
further capital/time should go to a genuinely different mechanism (see
CURRENT PRIORITIES in CLAUDE.md) rather than more instruments on this one.

---

# Round 13 — FOMC announcement reversal (registered 2026-07-06, before pull)

Mechanism: Baglioni & Ribeiro, "The FOMC Announcement Reversal" (2022) —
using intraday ES prices across 180 scheduled FOMC announcements
(Oct 1997 – Jan 2020), pre-FOMC drift (Lucca & Moench 2015 — already dead in
our own In-sample/CLAUDE.md history) has been replaced post-2011 by its
OPPOSITE: a reversal of the trailing 24h pre-announcement return, entered at
13:50 ET (10 min before the 14:00 statement) and closed at the RTH close same
day. Reported Sharpe > 2.5x the old drift strategy, and INCREASING (not
decaying) in the 2011-2020 subperiod — the opposite decay direction from
every other strategy in this file, consistent with a genuine
uncertainty-resolution risk premium rather than a slow-money inefficiency.
Distinguishing feature: mechanical (fades the pre-existing trend), does not
require predicting the FOMC outcome, so it does not inherit the
regime-fragility that killed GEX-sign conditioning (Round 6) or that makes
raw NFP-direction bets regime-fragile (2022 hiking cycle inverted the
"good news" sign). Naturally Topstep-legal by construction — entry 13:50 ET,
exit ~16:00 ET, entirely inside the RTH day session, nowhere near the
16:10–18:00 flatten window; no compliant-variant redesign needed (unlike
every entry in the overnight-drift family).

**Frozen spec:** at 13:50 ET on each scheduled FOMC decision day (dates from
the Federal Reserve's official historical meeting calendars,
federalreserve.gov/monetarypolicy/fomchistorical{YEAR}.htm — verified
2010, 2015, 2021-2027 directly during registration; full 2010-2026 table to
be completed via the same source or FRED's release-calendar API before any
data pull, not from memory), compute r24 = close(13:50 ET today) −
close(13:50 ET prior session). If r24 > 0 → SELL ES at 13:50 close. If
r24 < 0 → BUY. Flat if r24 == 0. Exit at the RTH 16:00 ET close same day (no
overnight hold). Costs: $4.00 RT (ES-scale) + 1-tick slippage both sides,
same convention as every other round.

**Pooling (frozen, not chosen post-hoc):** given only ~8 events/year
(~128-140 over 2010-2026), ES alone will not reach this repo's usual
n≥200-500 bar. Pool ES + NQ + MES + MNQ on the same event-days, each trade's
net USD divided by its point value (Round 7's normalization method), to
raise n toward pooled adequacy. This pooling rule is written down now,
before any data is touched, specifically so it cannot be adopted or dropped
after seeing which choice looks better.

**PASS bar:** pooled n ≥ 400, PF ≥ 1.10, one-sided p < 0.05 (t AND 20k
bootstrap), ≥ 60% of calendar years with net-positive pooled P&L.

**Before running:** (1) verify Baglioni & Ribeiro's citation record (SSRN/
journal version, any published failure-to-replicate) — a single working
paper is not the same evidentiary weight as a well-cited, replicated result;
(2) complete and sanity-check the full FOMC date table against the
official Fed calendar year-by-year (no fabricated/remembered dates); (3) NFP
and CPI reversal claims found alongside this (vendor-blog sourced, not
peer-reviewed) are explicitly EXCLUDED from this round — weaker evidence
should not ride along with a stronger hypothesis in the same test.

**Implementation status (2026-07-06):** FOMC date table completed and
verified directly against federalreserve.gov's year-by-year historical
archive (131 scheduled announcements, 2010 through the last completed 2026
meeting; exclusions documented in `oos/round13_fomc_reversal.py`'s module
docstring). Runner written: `oos/round13_fomc_reversal.py`. Not yet
executed — needs `oos/data/{ES,NQ,MES,MNQ}_5min.csv` locally.

**Known data-coverage caveat (disclosed before running, not after):** per
`CLAUDE.md`'s own infrastructure map, NQ history stops at 2019 and MES/MNQ
start at 2019 — so the pooled sample will realistically land near ES's full
131 + NQ's ~80 (2010-2019) + MES's ~51 + MNQ's ~51 (2019-2026) ≈ 310-330,
short of the n≥400 bar registered above, unless additional NQ 2019-2026
history is fetched. This is flagged now, before any P&L is computed, as an
implementation note (not a bar change) — if the pooled n comes in under 400
when run, treat that shortfall honestly (report it, do not lower the bar
post-hoc to make an under-powered result look sufficient).

---

# Round 14 — UW market-tide daily options positioning → next-session ES/NQ
(registered 2026-07-06, diagnostic-first, before any P&L computed)

Mechanism: Unusual Whales' `/api/market/market-tide` endpoint reports
market-wide (not single-name) net call/put options premium flow at intraday
granularity and accepts a historical `date` parameter. This is a DIFFERENT
data source and mechanism from Round 9 (which used the single-name SPY
flow-alerts endpoint and its `day_flow_score` formula, and FAILED to even
reach n=100 before the sample ran out) — so a fresh test here is a new
hypothesis, not a rescue of Round 9. Hypothesis: a market-wide, one-sided
options-premium day is followed by continued directional pressure in
SPX/NDX-linked index futures (ES/NQ) the next RTH session, via the dealer
hedging-flow channel (dealers who sold the skewed side must hedge, pushing
price further in that direction until the position unwinds).

**Known constraint, stated up front:** unlike Databento futures data (16
years, cheap), UW's usable historical depth for `market-tide` under our
current API plan is UNKNOWN and must be measured empirically before any
PASS bar is chosen — Round 9's SPY flow-alerts pull ran out after only ~55
usable days, which may reflect that endpoint specifically, the account's
subscription tier, or both. Full historical option-trade data is available
for separate purchase from UW at $250/month (10% off >1yr) if a deeper
backfill is wanted — a real cost decision, not something to assume.

**Step 1 — diagnostic (data availability, not a result; run first, always):**
probe `market-tide?date=D` at regular intervals going back from today and
record the first date at which the response is empty/404/quota-blocked.
This determines which PASS-bar tier below applies. This step must run
before any P&L is computed and its output must be recorded regardless of
what it shows (a short history is itself a usable, honestly-reported
finding, not a reason to keep pulling until it looks better).

**Step 2 — frozen signal (decided from data STRUCTURE, not outcomes):**
day_score(D) = last available intraday tick's (net_call_premium −
net_put_premium) for trading day D, i.e. the end-of-day reading. [If the
diagnostic pull shows this field is a per-tick/per-interval value rather
than a running cumulative — check via the raw shape of a few sample days
before freezing — switch to day_score(D) = sum of (net_call_premium −
net_put_premium) across all of day D's ticks instead. This choice must be
made by inspecting the data's shape only, written down, and never revisited
after computing a single dollar of P&L.]

**Step 3 — frozen trade rule (Topstep-legal by construction, no overnight
hold — the overnight-drift family's failure mode does not apply here):**
threshold ε = the trailing-60-trading-day rolling tercile split of
day_score (never a full-sample/look-ahead split). If day_score(D) is in the
top tercile → BUY ES at the D+1 09:30 ET open, exit at the D+1 16:00 ET
close. Bottom tercile → SELL. Middle tercile → no trade. Costs: $4.00 RT +
1-tick slippage both sides.

**PASS bar (tiered on Step 1's outcome — written now, before Step 1 runs):**
- ≥ 1 year of usable daily history → n ≥ 200 (pooled ES+NQ+MES+MNQ,
  point-normalized per Round 7's method), PF ≥ 1.10, one-sided p < 0.05
  (t AND bootstrap), ≥ 60% of available years/half-years net-positive.
- 3-12 months → exploratory only; report descriptive stats but do NOT treat
  any PASS as actionable. Only legitimate next step is forward/live logging
  (extend `uw_logger.py`'s existing CSV+analyzer to a next-session horizon,
  e.g. `python uw_logger.py path.csv 86400`) until a real OOS sample exists.
- < 3 months → stop; rely on forward logging only, same conclusion Round 9
  already reached for the single-name endpoint.

**Explicitly not in scope for this round:** GEX/dealer-gamma wall levels
(that stack, `options.py`, lives in the sister Trading-Bot repo and is not
part of this fork); the separate UW intraday GEX/gamma-flip capture
(`com.jarvis.uwcapture`, started 2026-07-04 in Trading-Bot, not reachable
from this repo) — CLAUDE.md already flags that as testable only after
~3 months of accumulation, independent of this round.

---

## Considered and REJECTED without a backtest (2026-07-06): FOMC reversal
does NOT generalize to more frequent scheduled releases

Motivation: Round 13's FOMC reversal only fires ~8x/year; wanted a
higher-frequency variant of the same mechanism (CPI, weekly initial jobless
claims, other 8:30 ET releases) to trade more consistently.

**Verdict: rejected on existing peer-reviewed evidence, no new data pull
needed.** Lucca & Moench (2015, the same paper that discovered the
pre-FOMC drift this reversal fades) explicitly tested nine other major
releases — weekly initial jobless claims, GDP, ISM, industrial production,
housing starts, personal income, CPI/PPI-adjacent — for the same
pre-announcement pattern and found NONE of them show a statistically
significant effect ("we conclude that no other major macroeconomic
announcement is associated with large and statistically significant
pre-announcement returns"). They further note Treasury/money-market futures
show no pre-FOMC effect either — it is specific to equities and specific to
monetary-policy decisions, not a general "scheduled release" phenomenon.
Mechanism read: FOMC uniquely resolves broad, economy-wide policy
uncertainty that every asset in the index re-prices simultaneously; a
weekly claims number is routine, narrower, and does not carry the same
uncertainty-unwind dynamic. Running the Round 13 reversal rule on claims/
CPI dates would be testing a mechanism the original researchers already
falsified — that is exactly the kind of hypothesis the harness is meant to
screen out before spending a data pull on it.

**Where "more frequent, real edge" would have to come from instead** (for
the next research pass, not registered yet): 0DTE options have gone from
niche to ~40-60%+ of daily SPX options volume (Cboe research; multiple 2024
SSRN papers), and dealer gamma sign now measurably shapes DAILY (not
event-day) index behavior — positive dealer gamma inventory strengthens
intraday reversal, negative strengthens momentum (Baltas et al. 2024, SSRN
4692190). This is a genuinely current, high-frequency, literature-grounded
mechanism. It is NOT registered as a round yet because it has the same
shape as Round 6 (GEX-sign regime conditioning), which already failed using
a static prior-day GEX snapshot — testing it properly needs the INTRADAY,
0DTE-aware gamma read the UW capture (`com.jarvis.uwcapture`) is built to
produce, which per CLAUDE.md is not usable until ~3 months of accumulation
(~October 2026). Naively re-testing Round 6's mechanism now with a
different label would likely just fail the same way. Revisit this
specifically once that capture matures — do not force it sooner.
