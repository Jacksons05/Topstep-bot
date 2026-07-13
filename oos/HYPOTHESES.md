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

## Round 13 — results (2026-07-09)

**Verdict: FAIL** on pooled cell (the judged hypothesis). n=309 (vs n≥400, pre-disclosed shortfall), PF=0.844 (vs 1.10), p_one_sided=0.761 t-test / 0.7614 bootstrap (vs <0.05), pct_years_positive=52.9% (vs 60%). Fails all four PASS bar dimensions.

**Detailed breakdown:**
- **Pooled ES+NQ+MES+MNQ (judged):** n=309, total_pts=-1173.96, PF=0.844, t=-0.71, p=0.761, 52.9% years positive.
  - ES solo: n=127, $15,117 total, PF=1.268, t=0.78, p=0.218, 58.8% years+ (positive PF but not sig).
  - NQ solo (exploratory, 2010-2019): n=72, $15,657 total, **PF=2.577**, **t=2.514, p=0.006**, **90% years+** — strongest individual cell, passes every PASS bar dimension independently. However, it is an exploratory (non-pre-judged) cell in a family-wise family (per HYPOTHESES.md disclosure), so a single PASS requires unseen-data confirmation on a genuinely new period/instrument before actionable.
  - MES solo: n=55, $539.25, PF=1.128, t=0.295, p=0.384, 50% years+ (weak).
  - MNQ solo: n=55, -$4,734, **PF=0.531**, t=-1.567, p=0.941, 25% years+ (strong fail).

**Yearly trend in pooled P&L:** 2010-2020 was net-positive (eight of ten years, cumulative ~$859). 2021-2026 inverted dramatically: -$898k (2021), -$237k (2020), -$500k (2023), -$1,025k (2024), -$306k (2026). The 2022 recovery (+$780k) was dwarfed by the pre/post-2022 losses. This pattern (pre-2021 edge, post-2021 inversion) suggests either regime shift or that the original Baglioni & Ribeiro (2022) sample window (through 2020) captured a transient effect that has reversed post-2021 monetary-policy cycle regime change.

**Interpretation:** The pooled FOMC reversal hypothesis FAILS. Shortfall in n is pre-disclosed and should be reported honestly, not as a reason to lower the bar: the p-value, PF, and yearly breakdown all independently falsify at any sample size. The strong NQ individual result (PF 2.577, p=0.006) is genuine but exploratory only — it would require a confirmed repeat on unseen NQ data 2019-2026 (years MNQ traded but NQ did not, if such data were obtainable) to be actionable. No parameter sweep to rescue; hypothesis is dead.

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

## Round 14 — diagnostic result (2026-07-09)

**Step 1 diagnostic ran 2026-07-09.** Probed market-tide endpoint going back from today (2026-07-09), recording the first date where responses become consistently EMPTY/ERROR.

**Furthest confirmed usable date: 2026-03-10.** Approx usable span: 121 calendar days (roughly 4 months, Feb 2026 → Jul 2026).

**Data structure verified:** Each day contains ~80-82 intraday ticks (per-minute or finer granularity). Sample fields (net_call_premium, net_put_premium, net_volume) are populated as per-tick records, so per-HYPOTHESES.md's Step 2 frozen rule, day_score(D) will be computed as the **last available intraday tick's (net_call_premium − net_put_premium)** for trading day D (end-of-day snapshot), not a sum-of-day. This rule was decided from data structure only, before any P&L computed.

**PASS-bar tier determination:** 121 days usable → falls in "3-12 months" tier. Per HYPOTHESES.md Round 14:

**"3-12 months → exploratory only; report descriptive stats but do NOT treat any PASS as actionable. Only legitimate next step is forward/live logging."**

**Conclusion:** Do NOT run full backtest. Historical depth is insufficient for a PASS bar to be actionable (n≥200 with 4 months of daily data is borderline, and p<0.05 would still be exploratory-tier). Instead, rely on forward logging via `com.jarvis.uwcapture`'s accumulated daily market-tide read (already active since 2026-07-04, persisting to ~/Claude/Trading-Bot/data/uw_intraday/). Revisit this hypothesis once that capture reaches ~3 months of accumulation (target: early October 2026), at which point a ~1-year-equivalent sample (4mo historical + 3mo forward = 7mo, or blended with a paid backfill to 1yr if budget allows) will merit full registration and test.

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

---

## External literature cross-validation (2026-07-06): ORB, VWAP mean-reversion,
intraday momentum/trend — independent confirmation of this file's own
Round 2/10 dead verdicts, plus the sizing-theory basis for the Topstep
risk layer

Requested: a survey of credible (non-marketing) published evidence for
ORB, VWAP/mean-reversion, and trend/momentum on MES/MNQ, post-2015 decay,
realistic micro costs, and how Topstep's trailing MLL + DLL should change
sizing/strategy choice. No new backtest — this cross-checks already-dead
internal verdicts against outside evidence and adds citations, discounting
prop-firm/course-seller marketing content per instruction.

**ORB.** The one heavily-cited "academic" ORB result (Zarattini & Aziz,
SSRN 2023, "Can Day Trading Really Be Profitable?" — a working paper, not a
peer-reviewed journal article) reports a 33% annualized alpha on QQQ
2016-2023, but the headline number is driven almost entirely by leverage
(TQQQ) rather than the raw signal, and an independent stress-test
replication extending the same logic to 2004-2024 found: (a) the edge was
already thin within the paper's own window ($0.04/share before costs), (b)
predictive power "completely flattened" post-2021, and (c) adding realistic
slippage (2¢ entry / 4¢ exit) and institutional commissions made the
strategy net-negative. This independently corroborates our own Round 2
C2_orb result (ES: PF 0.981, p=0.66; MES: PF 1.031, p=0.32, 37.5% years
positive; MNQ: PF 1.074, p=0.13, 62.5% years positive) — directionally
flat-to-mildly-positive, never statistically distinguishable from noise,
consistent with an overfit, decaying, leverage-dependent effect rather than
a real one.

**VWAP mean-reversion.** No credible peer-reviewed literature treats VWAP
deviation as a standalone alpha source — the academic VWAP literature
(execution-tracking papers, e.g. the UTS QFR working paper on optimal VWAP
strategies) is about *minimizing execution cost relative to VWAP*, not
about VWAP bands predicting price. The widely-repeated "63%/61% win rate at
the 2-SD band" claim traces back to unaudited vendor/course-seller blog
content (no visible methodology, sample, or cost accounting) — exactly the
kind of source this survey was asked to discount. This absence of credible
support matches our own Round 2 C3_vwap_reversion result, which is not
merely insignificant but catastrophic: PF 0.72-0.85 and **0% of years
positive on all three of ES, MES, and MNQ** — the worst result in this
entire file.

**Intraday momentum/trend.** This is the most nuanced case: real,
top-journal evidence exists (Baltussen, Da, Lammers & Martens, *Journal of
Financial Economics* 2021 — last-30-min return predicted by the rest of
the day's return, gamma-hedging mechanism, 60+ futures 1974-2020) and is
broader/more rigorous than Gao et al. (2018), which Round 10 already tested
and killed. But three things reconcile broad academic significance with
our own dead verdict: (1) a 2024 Aalto University thesis extending this
exact literature to a modern futures panel through Oct 2024 concludes
"most intraday momentum and reversal trading strategies may not be
feasible after transaction costs, especially in bond and currency
futures" — net-of-cost infeasibility is the literature's own current
conclusion, not just our result; (2) a 2026 arXiv paper ("Is Trend Still
Your Friend? A Microstructural Account of the Demise of Short-Term
Trend-Following") documents a *structural, mechanistic* post-2015 decay:
market makers now withdraw liquidity in the face of directional flow on
thin small-tick books, forcing trend-followers to "walk the book" at
prohibitive cost — explicitly "no post-2018 recovery," ruling out a
temporary crowding cycle; (3) a factor-decay model ("Not All Factors Crowd
Equally," 2025) quantifies momentum-family crowding as *accelerating*
specifically post-2015, correlated with factor-product AUM growth. All of
this matches our Round 10 result exactly (ES PF 0.733 p≈1.0; MNQ PF 0.888,
12.5% years positive) — the aggregate 46-year, 60-instrument academic
effect is real, but a single retail-cost account trading MNQ/MES today is
trading into the specific segment (short-horizon, thin-book, small-tick)
this literature says has already been arbitraged away.

**General decay framework.** McLean & Pontiff (*Journal of Finance*, 2016)
remains the best-cited general reference for *why* to expect this: across
97 published return predictors, returns are 26% lower out-of-sample
(statistical-bias upper bound) and 58% lower post-publication (≈32%
attributable to publication-informed trading) — anomalies decay, they
rarely disappear to exactly zero, but the decay is large and real. This is
the base rate this file's own track record (2 passes out of ~15 registered
hypotheses) is consistent with.

**Realistic MES/MNQ costs.** Corroborates CLAUDE.md's existing cost
assumptions rather than changing them: RTH bid-ask on both MES and MNQ is
consistently reported at 1 tick (equal to their full-size counterparts);
overnight/Globex widens to 2-3 ticks on the micros specifically (while
E-mini spreads stay closer to 1 tick), and the pre-RTH/afterhours window
around mega-cap earnings can widen further still (reported up to 4-6 ticks
on MNQ). Commission is consistently reported at ~$1.40-1.50 round-trip on
both micros, matching `oos/candidates.py`'s existing `comm_rt=1.40`
assumption. One directly relevant, methodologically rigorous outside check:
a 2026 arXiv systematic-falsification study built specifically on MNQ
(walk-forward validated, testing OHLCV-based intraday signals including a
gap-fill/continuation test structurally similar to our own Round 11) uses a
conservative fixed 2-point (~$4, i.e. 8-tick) round-trip friction assumption
and concludes tested signals hit a "friction ceiling" — directional content
exists but is too small to survive realistic costs. This is independent,
recent, MNQ-specific confirmation of this file's own cost-realism
conclusions, not a new finding for us to act on.

**How Topstep's trailing MLL + DLL should change sizing and strategy
choice.** This has a rigorous theoretical basis, not just intuition.
Grossman & Zhou (*Mathematical Finance*, 1993) solve the growth-optimal
strategy for an investor under a hard drawdown/trailing-stop constraint via
HJB/martingale methods: far above the floor, the optimal strategy converges
to full (unconstrained) Kelly; but *as wealth approaches the stop level, the
optimal exposure must converge to zero* — this is a rigorous proof of
"size down as you approach the floor," not a risk-management heuristic.
Busseti, Ryu & Boyd (*Journal of Investing*, 2016) give a practical convex
relaxation (risk-constrained Kelly) that provably beats plain
fractional-Kelly at the same drawdown-risk level. Two concrete implications
for this bot specifically:
1. **The trailing MLL (permanent, account-level) behaves like Grossman-Zhou's
   stop level** — `topstep_risk.giveback_ok()`'s equity-peak give-back
   halt is already structurally the right shape (shrink toward zero as
   equity nears the floor), not an ad hoc add-on; it is filling exactly the
   gap Grossman-Zhou identify (Topstep's own MLL locks at the starting
   balance and offers zero protection for profit accumulated above it).
2. **The Daily Loss Limit is a SEPARATE, tighter, resetting ruin barrier**,
   not a smaller version of the same constraint — it is effectively a fresh
   one-day gambler's-ruin problem every session, independent of how much
   MLL headroom exists. This structurally favors strategies with frequent,
   small, well-bounded-variance outcomes over few-large-trade strategies
   (classic trend-following, wide-stop breakouts): a single adverse
   trending day can consume an entire day's DLL budget in one trade,
   while a strategy generating many small, independent bets is far less
   likely to blow the daily budget in any single session even at the same
   aggregate expectancy. This is consistent with (and gives a theoretical
   reason for) `topstep_per_trade_risk_dll_frac` already capping per-trade
   risk at ≤50% of the DLL in `config.py`, and is a reason to weight
   research effort toward higher-frequency, smaller-edge-per-trade
   mechanisms (event-driven, mean-reversion-of-a-bounded-quantity) over
   large-swing trend/breakout ideas specifically **because of the account
   structure**, independent of whether trend-following would otherwise be
   profitable pre-cost.

**Bottom line:** none of ORB, VWAP mean-reversion, or intraday trend/momentum
survive independent literature scrutiny for a realistic-cost MES/MNQ
account any better than they survived this file's own Round 2/10 tests —
external evidence cross-validates, rather than reopens, those dead
verdicts. No new hypothesis is registered from this survey; it exists so
none of these three families gets re-proposed later without this context.

---

# Round 15 — regime-transition confluence, multi-bar hold (registered
2026-07-06, before running)

Mechanism: the same external survey (above) surfaced one MNQ-specific,
walk-forward-validated falsification study (arXiv, 2605.04004, 947 RTH days
of MNQ 5-min bars, 2021-2025) that tested 14 OHLCV single-bar signal
families and found none clear a 2-point round-trip friction floor — this
independently matches Round 2 (C2/C3) and Round 10's own dead single-bar
verdicts, and is not re-litigated here. That paper reported exactly TWO
positive-control signals that DID clear costs: an "RTH Confluence" signal
(regime classification + regime-transition probability + volume Z-score
confirmation, ATR-scaled pullback entry) and a session-based regime-
transition signal, both sharing two structural features every dead signal
in this file lacks: (a) they trade a detected REGIME TRANSITION rather than
a fixed OHLCV pattern, and (b) they use a multi-bar hold, not next-bar-only
exits. This is a mechanism difference worth one clean test, not a re-run of
Round 10 (which was a fixed time-of-day return-predicts-return pattern with
a single 25-minute hold) or Round 2 (fixed breakout/band patterns).

**Disclosed up front:** we do not have the arXiv paper's source code, exact
GMM specification, transition-probability estimation, or volume Z-score
construction. What follows is an honest PROXY built only from primitives
already present and unchanged in this repo (`regime.py`'s quantile-based
vol/trend classifier, `oos/candidates.py` C3's 20-bar rolling-sigma
convention, `backtest_fast.py`'s live 2×/3× ATR bracket, Round 8's
passive-limit-fill convention) — not a replication of the paper's own
result. A PASS here is evidence for THIS proxy; a FAIL does not refute the
paper's own (unreplicated by us) result, and a PASS should not be trusted
enough to size real capital before a cleaner replication attempt.

**Frozen spec (Hypothesis A, judged, RTH only):**
1. Causal regime label at bar i: reuse `regime.py`'s existing vol/trend
   buckets (`vol = ATR14/price`, `ts = |SMA20-SMA50|/SMA50`) but compute the
   90th/33rd/66th/33rd percentile thresholds from ONLY a trailing 500-bar
   rolling window ending at bar i-1 (never the global/full-sample
   quantiles `regime.py` uses for the live classifier — that would be
   look-ahead in a backtest). Label buckets: Crisis (vol ≥ p90),
   Trending (ts ≥ p66, not Crisis), Consolidation (vol ≤ p33 AND ts ≤ p33),
   else Mean-Reversion. Bars before 500 causal history exist are
   unlabeled/no-trade.
2. Regime-transition event: bar i where causal_label(i) != causal_label(i-1)
   AND causal_label(i) is "Trending" or "Crisis" (transition INTO a
   directional/high-vol regime — matches the paper's own framing of
   detecting the start of a regime, not steady-state).
3. Volume confirmation: z_vol(i) = (volume[i] - mean(volume[i-20:i])) /
   std(volume[i-20:i]) >= 1.0 (same 20-bar window convention as C3's
   VWAP-sigma).
4. Direction: sign of (SMA20(i) - SMA50(i)) at bar i — reuses the same
   trend-sign convention as `backtest_fast._quant_arrays`, not a new rule.
5. Entry: ATR-scaled pullback limit at close(i) - side*0.5*ATR14(i),
   counted filled only if one of the next 6 bars (30 min) trades through
   that price (Round 8's honest passive-fill convention — unfilled means
   no trade, not a market order).
6. Exit: stop = entry - side*2*ATR14, target = entry + side*3*ATR14 (the
   bot's own live bracket, `atr_stop_mult`/`atr_target_mult` defaults,
   2.0/3.0), max_hold = 24 bars (2h, same convention as `backtest_oos.py`).
   Session: entries only 09:30-15:00 ET; hard-flatten any open position at
   15:55 ET (Topstep-legal by construction, no overnight hold — this family
   does not inherit the overnight-drift flatten problem).
7. Costs: ES $4.00 RT + 1-tick slip both sides (judged instrument, full
   2010-2026 history for statistical power, same precedent as Round 10);
   MNQ $1.40 RT + 1-tick slip, 2019-2026 (exploratory / actual Topstep
   target instrument).

**Exploratory secondary cell (reported, not judged):** the same signal
computed on the overnight/Globex window (18:00-09:30 ET, excluding the
16:10-18:00 flatten window entirely) as a rough proxy for the paper's
separate "session-based" transition signal — legal on Topstep per the
existing Globex-overnight-is-fine rule, reported for completeness only,
not part of the PASS/FAIL judgment below.

**PASS bar (Hypothesis A, ES, standard convention):** n >= 200, PF >= 1.15,
one-sided p < 0.05 (t AND 20k bootstrap), >= 60% of calendar years positive.
FAIL -> proxy is dead, no parameter sweeps to rescue it, do not re-tune
0.5xATR/1.0-z/500-bar-window after seeing the result.

**Data:** already-local `oos/data/{ES,MNQ}_5min.csv` (Databento GLBX, same
files as every prior round) — no new data pull required.

**Result (2026-07-06): FAIL.** `oos/round15_regime_confluence.py`,
`oos/round15_results.json`.

| cell | n | total $ | PF | t | p (one-sided) | yrs+ |
|---|---|---|---|---|---|---|
| ES RTH_confluence (judged) | 2794 | -65,549 | 0.883 | -2.504 | 0.994 | 17.6% |
| ES overnight (exploratory) | 1797 | -37,802 | 0.861 | -2.272 | 0.988 | 29.4% |
| MNQ RTH_confluence (exploratory) | 1376 | +5,326 | 1.074 | 1.138 | 0.128 | 75.0% |
| MNQ overnight (exploratory) | 744 | +1,906 | 1.071 | 0.754 | 0.225 | 62.5% |

Judged cell (ES RTH) is decisively net-negative — not a marginal miss, a
wrong-signed result (p near 1.0, like Round 10, not like Round 6/9's
under-powered near-misses). PF < 1, most years losing. FAIL bar not met on
any dimension (n, PF, p, years+).

MNQ's own two cells are directionally positive (PF > 1, 62-75% years) but
neither clears p < 0.05 — noise-consistent, not evidence, at n=1376/744.
Since ES (2010-2026, the pre-registered judged instrument) fails outright,
MNQ's weaker positive reading is not grounds to re-test or re-tune per this
file's own no-rescue rule; it is reported for completeness, not treated as
a lead.

**Interpretation:** the regime-transition + volume-confluence + multi-bar-
hold proxy built here does not clear costs, on either instrument, in either
session. This does not refute arXiv 2605.04004's own reported result — our
causal-rolling-quantile regime classifier and volume Z-score are a
different, cruder construction than that paper's GMM + Markov transition
probabilities, which we have no source code for. What this DOES show:
simply adding "regime transition" + "multi-bar hold" on top of this
codebase's existing indicators (rolling ATR/trend-strength buckets, SMA
cross direction, the bot's 2x/3x ATR bracket) is not sufficient by itself
to manufacture the paper's edge — the specific unsupervised
classification/transition-probability machinery is likely load-bearing,
not incidental. Proxy is dead; do not re-tune threshold/window constants
(500-bar regime window, 1.0 z-vol, 0.5x ATR pullback) to rescue it.

---

# Round 16 — flow-risk OVERLAYS: vol-target sizing (A10) + toxicity veto (A8) (registered 2026-07-13, before running)

NOTE ON TYPE: unlike Rounds 1-15 these are RISK OVERLAYS, not entry edges. They
generate no trades of their own -- A10 scales an existing position's size, A8
stands aside on some of an existing strategy's entries. The standard entry PASS
bar (PF>=1.15, >=60% years positive on a trade stream) does not apply to a
non-directional sizing/filter rule; the adapted PASS bar below judges risk-
adjusted improvement of a base stream, net of the overlay's added turnover, by
the same machinery (one-sided t + 20k bootstrap seed 7, per calendar year).

DATA STATUS (2026-07-13): oos/data/ (ES 5-min) is not on disk and no Databento
key is set, so the ES kernel cannot run this round. A10's MECHANISM is tested
now on the one positive-drift series available offline -- daily SPX close
2011-05 -> 2026-07 (research/data/squeeze_dix_gex.csv, `price`, N~3800). A8 needs
intraday signed aggressor flow (oos/data/ES_of_1s.npz `tvol`) and is DATA-BLOCKED
-- spec frozen below, to run when that file exists.

## A10 -- Volatility-managed sizing (Moreira-Muir 2017 mechanism)
Base stream: daily long-index return r_t (equity risk premium -- the positive-
expectancy reference the overlay needs). Managed stream: scale exposure by a
CAUSAL weight w_t = c / sigma_hat_{t-1}; sigma_hat = trailing 22-day realized vol
of daily returns using data through t-1 only (no look-ahead); c fixed so the
managed stream's FULL-SAMPLE realized vol equals the base's (Moreira-Muir vol
normalization -- a presentation scalar, not a signal). Weights clipped to the
live overlay bounds, run BOTH ways: symmetric [0.34, 2.00] (mechanism form) and
the funded-account de-risk-only [0.34, 1.00] (the futures form actually shipped).
Cost: per-day turnover |w_t - w_{t-1}| charged the ES 1-tick round-trip rate
($29 = comm_rt 4.00 + 2*1*0.25*50) against index notional (price*50).

## A8 -- Flow-toxicity veto (BVC-VPIN stand-aside) [DATA-BLOCKED]
On ES intraday, compute bulk-volume-classified VPIN over `tvol` (ES_of_1s.npz),
equal-volume buckets. Frozen rule: mark bars with VPIN in the top decile of the
trailing 250-bucket distribution as blocked=1 (fed to _simulate's built-in veto
so freed slots re-pick the next signal). Judge a base entry stream WITH vs
WITHOUT the veto.

## PASS bar (adapted for a risk overlay; stated to avoid a moved goalpost)
A10 PASSES iff, net of turnover cost, ALL of:
  (1) alpha of managed on base (OLS r_managed ~ a + b*r_base) > 0, one-sided
      p < 0.05 by t AND 20k bootstrap;
  (2) Sharpe(managed) > Sharpe(base) AND max-drawdown(managed) < max-DD(base);
  (3) >= 60% of calendar years show managed >= base (annual net).
Fail (1) but hold (2) -> retained ONLY as drawdown control, not as alpha
(reported explicitly). A8 PASSES iff the vetoed stream's Sharpe > unvetoed and
the removed trades' mean is significantly < kept trades' (t + bootstrap).
Andersen-Bondarenko NULL registered: if VPIN is mechanical volatility, A8 adds
nothing beyond A10's vol scaling -> FAIL.

## Transfer disclosure
A10 on DAILY SPX validates the MECHANISM only. It does NOT license live use on
the Topstep INTRADAY mandate (flat-by-close, ~zero validated intraday drift --
Rounds 1-15 found no surviving intraday entry edge; an overlay has nothing to
improve without a positive-expectancy base). Live use stays blocked until a
validated intraday entry edge exists OR the overlay is confirmed on the ES 5-min
kernel with real data.

## Round 16 — results (2026-07-13)

Runner: oos/round16_flow_overlays.py (numpy-only; results in round16_results.json).
A10 on daily SPX 2011-05 -> 2026-07, N=3796, net of $29 ES 1-tick RT turnover cost.

**A10 verdict: does NOT pass as an alpha source. De-risk-only form validated as
DRAWDOWN CONTROL only (its intended role).**

- derisk_only [0.34,1.0] (the form actually shipped on the futures bot):
  Sharpe 0.698 -> 0.732, max-DD -$60,977 -> -$48,218 (-21%). BUT alpha
  $6.25/day, t=0.855, p=0.196 (t) / 0.201 (20k boot) -> NOT significant.
  pct_years managed>=base = 12.5% (it mostly holds LESS, so it trails base on
  raw return in calm up-years while winning on risk). => PARTIAL: retained as
  drawdown/Sharpe control, explicitly NOT an alpha source (fails PASS cond 1).
- symmetric [0.34,2.0] (leverage-up mechanism form): Sharpe 0.698 -> 0.657
  (WORSE), alpha t=0.257, p=0.398. => FAIL. Leveraging into low-vol regimes on
  post-2011 SPX does not survive costs (consistent with Cederburg et al. 2020's
  out-of-sample critique of volatility-managed portfolios).

**A8 (toxicity veto): NOT RUN — DATA-BLOCKED.** Needs intraday signed aggressor
flow (oos/data/ES_of_1s.npz `tvol`), not on disk; requires a Databento fetch +
oos/mbp10_features.py. Spec stays frozen for when the file exists.

**Interpretation.** Result matches the pre-registered expectation: these are risk
overlays, not edges. The shipped de-risk-only A10 is a legitimate DRAWDOWN-CONTROL
tool (Topstep's binding constraint is drawdown, not edge) but adds NO alpha and
must not be represented as one. Per the transfer disclosure, it has nothing to
improve on the flat-by-close intraday mandate until a validated intraday entry
edge exists (Rounds 1-15: none). A8 remains untested. Net: overlays may stay
wired for risk control; they do NOT change the bot's ENTRY-HALTED status, and no
entry logic is licensed by this round.
