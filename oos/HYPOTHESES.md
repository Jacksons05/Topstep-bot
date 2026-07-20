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

## Round 14 — BLOCKED, not failed (2026-07-20: UW subscription cancelled by
## the account holder)

The account holder cancelled the Unusual Whales subscription today. Both this
round's remaining paths are gone: the API's own historical market-tide access
(the 121-day probe above) and the `com.jarvis.uwcapture` forward accumulation
this round's plan depended on (ran only 2026-07-04 → 2026-07-20, ~16 days, far
short of the ~3-month target). No P&L exists to report and none was computed
against a maturing dataset that stopped mid-accumulation. **Status: BLOCKED
indefinitely, not dead** — this is a data-availability outcome, not a test
result; it must not be conflated with the DEAD list (CLAUDE.md) or treated as
a verdict. Reopen only if UW service is resubscribed (accumulation restarts
from zero) or an equivalent options-flow data source is sourced elsewhere;
until then, no further work on this round.

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

---

# Round 17 — A2: MOC / cash-close imbalance drift (registered 2026-07-13, before running)

RATIONALE: Highest-ranked candidate from the 2026-07 research pass (rank 2) that
is (a) NOT built on the dealer-gamma-sign DIRECTION mechanism already falsified in
Round 6 (GEX conditioning inverted in-window), and (b) Topstep-compatible (flat by
16:00, inside the 16:10 rule). Distinct mechanism: equity closing-auction (MOC)
order imbalance published ~15:50 ET -> index-arb + ETF hedging push ES into the
16:00 cash close in the imbalance direction; partial overnight reversion (Bogous-
slavsky & Muravyev 2023, SSRN 3485840).

## Frozen spec (ES, judged at 1-tick slippage, net; times ET)
Signal: net equity MOC imbalance (NYSE + Nasdaq, summed, $-notional) published at
the ~15:50 dissemination. Entry: at the 15:50 bar close, if |imbalance| is in the
top tercile of the trailing 60-session |imbalance| distribution, take ES in the
imbalance sign. Exit: 15:58 bar close (flat before 16:00 cash close, well inside
16:10). One trade per day. No stop (8-minute horizon; a hard $ cap sized to DLL
in live only). No overnight hold (avoids the documented reversion).
Costs: $29 ES 1-tick RT (comm 4.00 + 2*1*0.25*50).

## Data source + STATUS (2026-07-13): DATA-BLOCKED
Requires historical equity MOC imbalance (NYSE Order Imbalances / Nasdaq NOII).
These are PAID proprietary feeds; free multi-year history is not available. The
ES 5-min bars (oos/data/) needed for the drift leg are also not on disk. Options:
  1. FORWARD CAPTURE (recommended, matches the UW-capture precedent, CLAUDE.md
     priority #2): record net MOC imbalance + the 15:50->16:00 ES move daily; test
     after >= ~3 months / n>=200 sessions.
  2. Paid history (NYSE TAQ auction / Nasdaq TotalView-Imbalance, or a vendor) ->
     run oos/round17_moc_drift.py immediately.
NO PROXY substitute: the 15:45->15:50 ES move alone is just close momentum (a
distinct, already-dead hypothesis) and must NOT be reported as A2.

## PASS bar (standard entry-edge bar -- A2 IS a directional entry)
n >= 200, PF >= 1.15, one-sided p < 0.05 (t AND 20k bootstrap seed 7),
>= 60% of calendar years positive, judged NET at 1-tick slippage. Any fail ->
dead, no re-tuning. A single PASS still needs unseen-data / forward confirmation
before shipping (family-wise discipline).

## Runner
oos/round17_moc_drift.py -- data-format contract + evaluate() (harness kernel
conventions); reports DATA-BLOCKED until a MOC-imbalance dataset is present.

## Round 17 — data-availability update (2026-07-20, re-checked during a
## full-ledger re-evaluation; still DATA-BLOCKED, status only, no P&L)

Re-confirmed via Databento's own docs (not re-tested, not re-tuned): Databento
DOES carry both legs. NYSE closing-auction imbalance (`ref_price`,
`paired_qty`, `total_imbalance_qty`, `side`) is in the `imbalance` schema on
`XNYS.PILLAR`, disseminated every 1s from 15:50 ET — an exact match for this
round's frozen 15:50 signal timestamp. Nasdaq NOII is in the `imbalance`
schema on `XNAS.ITCH`. Pricing is NOT yet confirmed at the historical,
one-off-pull rate this program has always used (Databento's public page only
advertises a real-time "Plus/Unlimited" subscription starting at $1,000/mo
for the imbalance-only tier; the separate historical-usage-based rate for a
one-time backtest pull, which is what every prior round in this file has
paid, has not been quoted). Before any spend: run
`Historical.metadata.get_cost()` for the exact ES/NYSE+NASDAQ imbalance
window needed and report the number BEFORE downloading anything (same
discipline as Round 20's MBO cost check) -- this may be materially cheaper
than the $1,000/mo headline figure, or it may not; find out, do not assume
either way. A real-time capture (the $0 forward-logging alternative used for
UW GEX) does NOT look zero-cost here either -- Databento's real-time
imbalance access is gated behind the same Plus/Unlimited tier. **This is a
disclosed $ decision for the account holder, same as Round 12's deferred
CL/YM leg -- not something to spend on without explicit approval.** Status
remains DATA-BLOCKED pending that decision; the frozen spec above is
unchanged and ready to run the moment data exists.

## Round 17 — cost-check tooling added (2026-07-20, no P&L, no spend, still
## DATA-BLOCKED pending the account holder's decision)

`oos/fetch_round17_imbalance.py` written: calls `Historical.metadata.
get_cost()` (free, read-only) for the full-market `imbalance` schema on
XNYS.PILLAR (NYSE) and XNAS.ITCH (Nasdaq) across a few candidate window
sizes (1y/3y/5y -- the top-tercile gate needs ~600+ sessions, ~2.4y minimum,
for n>=200 post-gate trades). **Not yet run against a real key** -- no
DATABENTO_API_KEY was available in the environment this was written in; the
script fails loudly and safely with no key rather than guessing. Run it once
the key is available and report the numbers back before deciding on a
window. Still does not download or aggregate anything -- that pipeline (to
build this round's moc_imbalance.csv contract) is a separate follow-up,
written only once a window is approved.

---

# Round 18 — dealer net-gamma-level reversal, intraday (registered 2026-07-09, diagnostic-first, before P&L)

**Source:** deep-research pass (101-agent, 19 sources, 25 claims adversarially
verified 9-confirmed/16-refuted), converging on Dim/Eraker/Vilkov (SSRN
4692190 + extended 5641974), Buis/Pieterse-Bloem/Verschoor/Zwinkels (JEDC
2024), Baltussen/Da/Lammers/Martens (JFE, peer-reviewed).

**Mechanism:** dealer/market-maker net gamma sign+magnitude determines
hedging direction: positive net gamma → contrarian (buy-dip/sell-rally)
delta-hedging → dampened vol, stronger short-horizon reversal; negative net
gamma → momentum-following hedging → amplified vol/fragility. Sources'
futures-specific number: higher net gamma predicts lower SPX vol over the
next ~10min, effect fades within ~1hr, and predicts a stronger E-mini
reversal over the same window.

**Explicit differentiation from Round 6 (already dead — do not treat this as
a rescue):** Round 6 conditioned static prior-day GEX SIGN (from UW's EOD-only
`/greek-exposure` endpoint) as a coarse daily filter bolted onto pre-existing
C2 ORB / C3 VWAP-reversion entries; it FAILED and its own writeup concluded
"remaining UW angle would be intraday levels... which needs intraday GEX
history we do not have." Round 18 uses that intraday history (now confirmed
to exist and be reachable, see below) as the entry trigger itself — continuous
gamma MAGNITUDE, not daily sign; direct fade signal, not a filter on an
unrelated entry rule; calibrated to the literature's stated 10min-1hr decay
window, not an all-day regime label. This differentiation is real but
UNPROVEN — treat as a genuinely new hypothesis, tested skeptically, not as
exempt from Round 6's failure.

**Data-availability check (performed 2026-07-09, live probe against
`/api/stock/{ticker}/spot-exposures`, current UW subscription tier):**
- Endpoint confirmed live and reachable with current API key. Returns
  ~1-min-cadence intraday snapshots (fields: `gamma_per_one_percent_move_oi`,
  `_vol`, `_dir`, `price`, `time`), spanning ~06:30-16:00 ET per trading day
  (~480 records/day for SPX).
- Historical depth is tier-capped: probing back dates returned
  `403 historic_data_access_missing` for 2025-07-09 (1yr back), with the API
  reporting the earliest available date as **2026-02-26 (90 trading days)**.
  120 days back (2026-03-12) and 30 days back both returned 200 with data.
- **Tier determination (from calendar span, per Round 14's convention — decide
  from span, never from n):** 90 trading days ≈ 4.3 months → **3-12 months
  bucket → EXPLORATORY ONLY. No PASS from this round is actionable**, even
  though intraday sampling could produce n well above 200 — the yearly/
  half-yearly positive-year check cannot be computed meaningfully within a
  single partial calendar year, and the harness's tiering is bound to
  calendar span, not raw trade count. Only legitimate next step on a FAIL or
  a marginal PASS: keep `com.jarvis.uwcapture` running and continue accruing
  history in parallel, or evaluate the cost of UW's deeper-history add-on
  ($250/mo per Round 14's pricing note) as a real, disclosed spend decision —
  not something to assume.

**Signal field, frozen from structure inspection only (never from a P&L
run):** `gamma_per_one_percent_move_oi` — the open-interest-based gamma
figure, not `_vol` (volume-based) or `_dir` (directionalized-flow-based).
This choice is deliberate and mirrors the deep-research finding that the
volatility-dampening effect is driven by multi-day *inventory* (OI) rolling
into short-dated exposure, not same-day flow — the OI-based field is the
correct analog; the volume/direction fields are the same "same-day flow"
construct the research explicitly found does NOT propagate into forward
moves. Live sample (SPX, 2026-07-08) confirmed the field is signed (both
negative, e.g. -1.8e9, and positive, e.g. +1.3e10, values observed same day).

**Sign-convention assumption (frozen, stated before any P&L — this is the
single biggest implementation-validity risk carried into this round):**
assume UW's sign follows the standard industry GEX convention (positive =
dealers net long gamma = stabilizing/contrarian; negative = net short gamma =
destabilizing/momentum), matching the academic sources' convention. UW's
exact computation methodology is undocumented in the public API reference and
was NOT independently verified against the academic construct — this is
exactly the "vendor methodology mismatch" risk flagged by the research. If
this round's result looks inverted from the mechanism's prediction, the
correct response is to note the sign may be flipped and stop — NOT to flip
the sign and rerun (that is a rescue).

**Frozen trade rule (Topstep-legal, no overnight hold):**
1. Underlier: SPX (ES/MES judged instrument — this is the only instrument the
   source literature directly studied). NDX (proxy for NQ/MNQ) run in parallel
   but reported exploratory-only per the research's own open question #4 (no
   source studied NQ/QQQ/MNQ) — never pooled into the judged ES cell.
2. Trailing distribution: pool all `gamma_per_one_percent_move_oi` snapshots
   over the trailing 5 trading days (rolling, causal, no look-ahead), tercile
   split (33.3/66.7 pct), recomputed at each new snapshot.
3. Entry trigger: current snapshot reading >= top tercile (strongly positive
   net gamma) AND time-of-day in 09:30-15:00 ET (leaves room for the longest
   frozen hold below to exit before 15:59 ET flatten) AND no position
   currently open. Direction: fade the underlier's own trailing 10-minute
   price move (if price rose over the prior 10min, go short; if it fell, go
   long; flat trailing move -> skip). Bottom tercile (strongly negative net
   gamma) -> stand aside (research does not establish a clean momentum-side
   edge, only the dampening side) — no trade, not a mirrored momentum entry.
   Middle tercile -> no trade.
4. Exit — three frozen hold times, pre-registered together (not swept after
   seeing results):
   - **Primary, judged:** 10-minute hard time-stop (paper's stated peak-effect
     window).
   - **Secondary, exploratory/reported-not-judged:** 30-minute and 60-minute
     hard time-stops (paper's stated fade boundary), same convention as Round
     15's exploratory secondary cell.
   - All holds additionally hard-capped at 15:59 ET same-day flatten.
5. Execution proxy: ES 5-min bars (`oos/data/ES_5min.csv`, `NQ_5min.csv` for
   the exploratory NDX/NQ leg) — entry/exit price = nearest bar at-or-after
   the decision timestamp, tolerance 10min (same helper convention as Round
   14). Skip a signal if no bar found in tolerance.
6. Costs: ES $4.00 RT + 1-tick slippage both sides; NQ $4.00 RT + 1-tick
   slippage (exploratory leg), same convention as prior rounds.

**PASS bar (written now — but per the tier determination above, no PASS from
this run is actionable regardless of what these numbers say; this exists so
the exploratory read is still judged against a real bar and not just eyeballed):**
n >= 200 (primary 10-min cell, ES only), PF >= 1.15, one-sided p < 0.05 (t AND
20k bootstrap). Reported, not actionable, until calendar span clears 1 year.

**Explicitly not in scope:** any parameter sweep on the tercile window, hold
times, or trailing-lookback-for-direction (10min) if this round's numbers
look weak — those are frozen above and stay frozen. VIX/ATM-IV residualization
(open question #2 from the research) is a genuinely different, separate
hypothesis to register later, not a fix to apply here after seeing a weak
result.

## Round 18 — results (2026-07-09)

`oos/round18_gamma_reversal.py`, `oos/round18_gamma_scores.json` (190
ticker-days pulled, SPX+NDX, 2026-02-26 through 2026-07-08),
`oos/round18_results.json`.

**Primary judged cell — ES, 10-min hold:** n=1018, PF **0.767**, t **-2.943**,
p_one_sided **0.99837** (bootstrap 0.9985), win 43.5%, 2026 net -$647 (pts).
This is not a noisy null — p≈0.998 against the hypothesized direction means
the observed effect is **decisively wrong-signed**: the frozen fade rule lost
money at high confidence, the opposite of what the mechanism predicts.

| cell | n | PF | t | p (one-sided) | judged |
|---|---|---|---|---|---|
| ES hold10 (primary) | 1018 | 0.767 | -2.943 | 0.99837 | yes |
| ES hold30 (exploratory) | 394 | 0.816 | -1.527 | 0.93661 | no |
| ES hold60 (exploratory) | 227 | 0.993 | -0.040 | 0.51589 | no |
| NQ hold10/30/60 | 0 | — | — | — | no |

**NQ leg produced n=0 — a pre-existing data gap, not a signal finding.**
`oos/data/NQ_5min.csv` ends 2019-05-03 (stale, superseded by `MNQ_5min.csv`
in later work); it has zero overlap with the 2026-02-26+ gamma window. Not a
result on the NQ/NDX hypothesis either way — undetermined. (Separately,
`ES_5min.csv` ends 2026-06-05, ~5 weeks before the gamma data's end date —
the primary ES cell above is missing its most recent ~24 trading days for the
same reason. Noted for completeness; not re-pulled or rerun after seeing the
result, per no-rescue rule — the effect is already decisive at n=1018.)

**Interpretation:** the frozen spec explicitly pre-committed to this exact
scenario: *"If this round's result looks inverted from the mechanism's
prediction, the correct response is to note the sign may be flipped and
stop — NOT to flip the sign and rerun (that is a rescue)."* That is what
happened. Two live explanations, not distinguished by this data:
(1) UW's `gamma_per_one_percent_move_oi` sign convention does not match the
assumed industry convention (the single biggest implementation-validity risk
flagged at registration time), or (2) the fade-on-positive-gamma mechanism
genuinely does not survive this construction/instrument/cost structure.
**This construction is dead. Do not flip the sign and rerun Round 18.** A
sign-flip test, if wanted, is a NEW hypothesis (would need its own
pre-registration, e.g. Round 17) — not a patch to this one. Exploratory
30/60-min cells show the same negative-t direction, weakening toward zero as
hold lengthens, consistent with cost-drag on a null/negative signal rather
than a real effect that fades — not grounds to rescue the 10-min cell.

---

# Round 19 — quant-signal confidence stratification: does CONFIDENCE_THRESHOLD
have anything left to tune? (registered 2026-07-15, before running)

**Motivation.** Live question from the user: "how high should CONFIDENCE_THRESHOLD
be set?" That is a parameter-sweep question about an entry signal, and per
CLAUDE.md must go through this harness before being answered with a number.

**Why this is NOT a re-run of the original OOS trial.** The very first
pre-registered test in this file (top of this document, "Strategy under test")
ran `backtest_fast._quant_arrays` + `_simulate` at "confidence threshold 0.75
(full alignment only)" and found it dead (CLAUDE.md: "SMA(20/50)+RSI at 5-min
cadence: PF 0.74-0.95, negative 17/17 years, ES overnight cell -$402,767,
t=-9.9"). `_quant_arrays`'s `strength` output is DISCRETE, not continuous: trend
contributes +-0.5, RSI-extreme confirmation adds another +-0.5, clipped to
[-1,1] -- so `strength` can only be 0.0 (flat), 0.5 (trend fires, RSI does not
confirm), or 1.0 (both agree). A threshold of 0.75 therefore already isolated
the strength==1.0 cell exclusively, and it is the WORST cell on record (17/17
negative years) -- already dead, not eligible for re-test under the no-retest
rule. "Raise the threshold" has thus already been answered at its logical
maximum: no.

**What is actually untested.** The strength==0.5 cell (trend-direction fires,
RSI does not confirm) has never been isolated on its own -- the original trial's
0.75 gate excluded it entirely, and Round 15's regime-transition proxy used a
different construction (GMM-adjacent regime buckets, not this signal's own
strength value), and Round 18's dealer-gamma test is an unrelated mechanism.
This round tests the strength==0.5 cell. Answering it closes the loop on
CONFIDENCE_THRESHOLD: if BOTH discrete tiers (0.5 and 1.0) are dead, no
setting of this control rescues the SMA/RSI signal family, full stop.

**Disclosed scope limit.** Live `CONFIDENCE_THRESHOLD` gates a BLEND of this
quant strength value and the qual/LLM stream (50/50 RTH per `engine.py`), not
quant strength alone. The qual stream cannot be replayed deterministically over
history, so this round tests only the mechanically backtestable half. A PASS
here would answer "does the quant signal's own confidence carry information,"
not "here is the exact number to type into `.env`" -- that would still need the
qual blend's own validation, which is a separate, harder problem not in scope.

**Frozen spec.**
- Kernel: `backtest_fast._quant_arrays` + `_simulate` verbatim, live CONFIG
  brackets (`atr_stop_mult`=2.0, `atr_target_mult`=3.0, `stop_loss_pct` floor),
  `max_hold`=24 bars (2h) -- identical to every round reusing this kernel
  (Round 15 precedent). No new indicator, no new bracket logic.
- Two cells, mutually exclusive, by strength value at entry bar i:
  - **PARTIAL** (judged): strength(i) == 0.5 exactly.
  - **FULL** (NOT judged -- replication check only, see below): strength(i) ==
    1.0 exactly. Expected to reproduce the original trial's dead result; if it
    doesn't, that flags a runner bug before PARTIAL's result is trusted.
- Session: RTH only (09:30-16:00 ET, `is_rth()` from `oos/backtest_oos.py`) --
  live `CONFIDENCE_THRESHOLD` (as opposed to the separate
  `CONFIDENCE_THRESHOLD_OVERNIGHT`) governs RTH entries specifically. Overnight
  reported exploratory only, not judged (matches Round 15's session-split
  disclosure convention; the original trial's worst cell was already overnight).
- Instruments/costs: ES $4.00 RT + 1-tick slip both sides, full 2010-2026
  history (judged, statistical power); MNQ $1.40 RT + 1-tick slip, 2019-2026
  (exploratory, actual Topstep target instrument) -- same convention as every
  prior round.
- Stats: `tstat_p`/`boot_p`/`cell` reused verbatim from `oos/backtest_oos.py`
  (RNG_SEED=7, BOOT_N=20,000) -- no new statistical method introduced.

**PASS bar (PARTIAL cell, ES, RTH -- the judged hypothesis):** n >= 200,
PF >= 1.15, one-sided p < 0.05 (t AND 20k bootstrap), >= 60% of calendar years
positive. FULL cell is not re-judged (already dead per the original trial) --
reported only as a same-method replication check on this runner's correctness.
MNQ and overnight cells reported, not judged.

**Verdict rule.**
- PARTIAL FAILS -> both discrete strength tiers of this signal family are dead.
  The honest answer to "how high should CONFIDENCE_THRESHOLD be" is: no number
  rescues it; the quant stream needs replacement, not retuning, before this
  account trades on it. Consistent with CLAUDE.md's own priority list (0DTE
  gamma walls, tick-level order flow, higher-frequency regimes) -- pursue one of
  those instead of further threshold tuning.
- PARTIAL PASSES -> a genuinely new lead, but per this file's family-wise
  disclosure (many registered hypotheses at p<0.05 -> expected false positives),
  requires unseen-data confirmation (a different period or instrument) before
  any CONFIDENCE_THRESHOLD change ships. Not actionable on a single PASS alone.

**Data:** already-local `oos/data/{ES,MNQ}_5min.csv` -- no new pull required.

**Runner:** `oos/round19_confidence_tiers.py` (written alongside this
registration, not yet executed).

## Round 19 — results (2026-07-15)

Data: `oos/data/{ES,MNQ,MES}_5min.csv` freshly pulled via `fetch_databento.py`
this round ($38.64, matches the pre-verified estimate, signup credits).
Runner: `oos/round19_confidence_tiers.py`; full output `oos/round19_results.json`.

**Verdict: FAIL, decisively.** PARTIAL (strength==0.5, the judged, previously-
untested cell) is dead on ES/RTH -- not a marginal miss, a wrong-signed result
on every dimension at a very large sample:

| cell | n | PF | t | p (one-sided) | yrs+ |
|---|---|---|---|---|---|
| **ES PARTIAL_RTH (judged)** | 13,131 | 0.846 | -6.012 | 1.0 (t) / 1.0 (boot) | **0.0%** |
| ES PARTIAL_overnight (exploratory) | 32,745 | 0.743 | -15.489 | 1.0 / 1.0 | 5.9% |
| ES FULL_RTH (replication check) | 776 | 0.791 | — | 0.984 / 0.984 | 41.2% |
| MNQ PARTIAL_RTH (exploratory) | 5,830 | 1.001 | — | 0.492 / 0.485 | 37.5% |
| MNQ FULL_RTH (exploratory) | 361 | 0.741 | — | 0.983 / 0.985 | 0.0% |

ES PARTIAL_RTH: total -$427,235 over 13,131 trades, **17/17 calendar years
(2010-2026) negative, zero exceptions.** p=1.0 on both t-test and 20k
bootstrap is not "not significant" -- it is maximal evidence the true mean is
at or below zero, the opposite tail from what a PASS needed. FULL_RTH
replicates the original trial's dead result (PF 0.791, within the previously
reported 0.74-0.95 range) -- confirms this runner is not the original
gate's own generator (already dead, correctly not re-judged). MNQ PARTIAL is
the least-bad cell in the whole grid (PF~1.00, p~0.49) but that is
indistinguishable from a coin flip, nowhere near the PASS bar, and not the
judged cell regardless.

**Answer to the motivating question.** Both discrete strength tiers of the
live quant signal (0.5 and 1.0 -- the entire domain `strength` can take) are
now confirmed dead on the judged instrument. There is no CONFIDENCE_THRESHOLD
value that rescues this signal family -- raising or lowering the number only
changes which of two already-dead cells gets traded. Per the verdict rule
registered above: the quant stream needs replacement, not retuning. This
closes the loop opened by "how high should CONFIDENCE_THRESHOLD be" with a
direct, evidence-based no -- not a guess, not a default left as a placeholder.
No re-tuning of the 0.5/1.0 boundary or session split to rescue this result.

---

# Round 20 — maker-side order flow with honest queue-position fill modeling
(registered 2026-07-16, before pull)

**Mechanism (stated before any data is touched).** Round 5 (order-book
imbalance + CVD, TAKER fills, 5-min hold) FAILED: PF 0.742, t=-5.6, n=3,586 --
consistent with OFI literature that imbalance alpha decays in tens of seconds,
faster than the 5-min hold could capture. That result indicts the EXECUTION
STYLE and HOLD HORIZON, not necessarily the OBI/CVD signal itself: a taker
order crossing the spread pays for immediacy on a signal that's already stale
by the time a retail-latency (~1s) order reaches the exchange, then the
strategy holds 5 more minutes past that. This round tests the untested half:
does the SAME entry signal earn a positive edge as a MAKER (resting limit
order, earning queue priority / the spread) instead of paying to cross it?
This is not a re-run of Round 5 -- it is a different execution mechanism on a
signal whose directional read was never itself shown to be wrong, only
un-capturable at taker/5-min settings. Per CLAUDE.md's own priority list,
this is explicitly gated on "fill modeling [being] honest about adverse
selection" -- a resting order that only fills when the market is about to
move against it is not a real edge, and this spec is written to make that
failure mode visible rather than hidden.

**Why MBO, not another mbp-10 pull (the level Round 5 already used).**
mbp-10 gives aggregate size at each of 10 price levels -- enough for OBI/CVD
features, not enough to know where a hypothetical resting order sits in the
queue at a price, which is exactly what determines whether and when a
passive order would realistically fill. Market-By-Order (MBO) carries every
individual add/modify/cancel/fill message, enabling an honest (if still
simplified -- see Fill Rule below) queue-position simulation instead of
Round 8's coarser "any print trades through the price" convention.

**Instrument.** MES only -- NOT ES (which Round 5 used) and NOT MNQ.
Narrowed from the original MES+MNQ scope (2026-07-16, before any data
pulled) per the account holder's explicit statement that MES is the only
instrument they intend to trade -- not a cost-driven cut (MNQ's estimate,
$181.39 of the $236.82 total, was disclosed in full before this narrowing;
see cost note below), a scope decision. This account trades micros;
micro-contract book depth, queue length, and participant mix differ from
the full-size contract, and validating microstructure on an instrument this
account cannot or will not trade would not validate anything tradeable.

**Data.** Databento MBO, GLBX.MDP3, MES.v.0 only. TWO non-adjacent one-month
windows (not one -- Round 5's own registration disclosed its single month as
a single-regime risk requiring a second independent sample before shipping;
that second sample never happened. This round provides it by design, both
windows judged, not sequentially rescued). Windows: 2026-01-06..2026-02-06
and 2026-05-06..2026-06-06 (the second matches Round 5's own mbp-10 window
exactly, chosen as a natural reference point before any tick data was
examined -- not a favorable-regime pick). Cost estimate confirmed
2026-07-16: MES $27.16 (window 1) + $28.27 (window 2) = **$55.43 total**.

**Frozen signal (identical to Round 5 -- reusing an existing entry read
verbatim, not proposing a new one).** OBI10 = (Σbid_sz - Σask_sz)/(Σbid_sz +
Σask_sz) over 10 levels; z = trailing 30-min z-score of OBI10; CVD5 = signed
trade volume, trailing 5 min. z >= +1.5 AND CVD5 > 0 -> LONG signal. z <=
-1.5 AND CVD5 < 0 -> SHORT signal.

**Frozen execution (the actual new mechanism under test).** On a signal,
place a resting LIMIT order at the current best bid (long) / best ask
(short) -- i.e., join the back of the queue at the touch, do not improve
price. Track that order's queue position via MBO (its rank among resting
size ahead of it at that price, from the order-add sequence). FILL RULE:
counted as filled only when MBO shows the order's queue position would have
been reached AND consumed by subsequent trade prints at that price (not
merely "a print occurred at this price" -- must be enough executed volume
at the price, in sequence, to have reached this order's queue rank). Order
expires unfilled after 30 seconds resting (a signal that never gets
front-of-queue in 30s produces no trade, not a market fallback -- Round 8's
"unfilled = no trade" convention). Exit: EITHER a resting take-profit limit
at entry +/- 1x the entry-time OBI-implied move (frozen, not swept) with the
same queue-fill logic, OR a 5-min taker time-stop if the passive target
never fills (crossing the spread to exit is judged separately from crossing
to enter -- an asymmetric exit is not the mechanism under test and a taker
exit is the conservative/honest choice when a passive one hasn't triggered).

**Costs.** $1.40 RT commission (micros). No slippage line item on the entry
(the queue-fill rule already prices the entry cost as time-in-queue, not a
tick assumption) -- 1-tick slippage still applied to any taker time-stop
exit, same convention as every prior round.

**PASS bar.** n >= 1000 (tick-level convention, matching Round 5's bar, not
the 5-min-bar n>=200 convention), PF >= 1.10, one-sided p < 0.05 (t AND 20k
bootstrap), on EACH of the two independent one-month windows separately (not
pooled) -- a single-window PASS is exactly the single-regime risk Round 5
disclosed and this round exists to close. BOTH windows must clear the bar.
Fail either -> dead, no re-tuning of the queue-fill rule, order-expiry
timeout, or target multiple after seeing results.

**Process (before any data is pulled).** 1) Run Databento's
`metadata.get_cost()` for the proposed MBO windows and report the estimate
before downloading anything -- MBO event volume is far higher than mbp-10 or
5-min bars, cost is not assumed to be small. DONE (2026-07-16): $55.43 for
MES across both windows, confirmed via `metadata.get_cost()`, nothing
downloaded yet at estimate time. 2) If cost is material, this is a decision
point for the account holder, not an auto-proceed. 3) Runner:
`oos/round20_maker_orderflow.py` (not yet written -- written once the data-cost estimate is confirmed, so the fill-simulation
code is built against the real MBO schema rather than guessed).

**Verdict rule.** PASS on both windows -> genuinely new lead; per this
file's family-wise discipline, still requires a third, forward/live
confirmation window before sizing real capital (this is a 2-sample bar
already, one step past the single-PASS-needs-confirmation default). FAIL
either window -> maker-side execution does not rescue this signal either;
the OBI/CVD family is dead in both its taker (Round 5) and maker (this
round) forms, and no further execution-style variant should be tried on it
without a genuinely different signal, not just a third fill convention.

## Round 20 — pre-run amendment (2026-07-16, before the simulator processed
## either window; resolves terms the registration left underdetermined)

1. **"OBI-implied move" (TP distance)** := |z_entry| × σ30(mid), where
   σ30(mid) is the standard deviation of the SAME trailing 30-min 1-second
   mid-price series the z-window already maintains. Snapped to the tick
   grid, minimum 1 tick. No new free parameters; not swept.
2. **Queue-ahead accounting** (the honest reading of the registered fill
   rule): at join, ahead = the set of orders resting at the join price
   (from the live MBO order book). It decrements on those orders' fills,
   cancels, and size REDUCTIONS; a modify that raises size or moves price
   drops the order from the set (priority lost). Our 1-lot fills when
   ahead ≤ 0 AND a subsequent trade executes at our price on our side.
3. **TP exit** uses the identical queue simulation, joining the target
   price's queue at position-open time. TP-vs-timeout tie in the same
   second → the taker time-stop wins (conservative).
4. Sampling/latency per Round 5's registered convention: 1-second samples;
   the entry order joins the queue at the FIRST snapshot after the signal,
   at that snapshot's touch. No re-pegging while resting; 30 s expiry as
   registered.
5. Taker time-stop executes at the first snapshot ≥ fill+300 s with a
   valid two-sided book (maintenance-halt gaps roll forward), at the
   opposite touch worsened by 1 tick (the registered slippage convention).
6. Runner: oos/round20_maker_orderflow.py. Adverse-selection visibility
   (registered requirement): the run reports signal count, fill rate,
   median time-in-queue, and expired-unfilled count per window alongside
   the judged trade stats.

## Round 20 — results (2026-07-16, oos/round20_maker_orderflow.py,
## round20_results.json; both windows judged, PASS required on both)

**Verdict: FAIL — both windows, decisively and consistently.**

| window | n | total $ | PF | t | p (t / boot) | fill% | med queue | TP exits | timestop |
|---|---|---|---|---|---|---|---|---|---|
| W1 2026-01 | 3,730 | −9,484.50 | 0.649 | −8.90 | 1.0 / 1.0 | 63.6% | 6 s | 763 (20%) | 2,967 |
| W2 2026-05 | 3,379 | −9,795.60 | 0.625 | −9.58 | 1.0 / 1.0 | 71.5% | 5 s | 636 (19%) | 2,743 |

**The registered failure mode is exactly what the honest fill model
exposed: adverse selection.** Fill rates are HIGH (64-72%, median 5-6 s in
queue) — the resting order gets filled easily, precisely because it fills
when flow is coming through it. Only ~20% of positions ever reach the
passive take-profit; ~80% bleed out through the 5-min taker time-stop.
Avg net −$2.54 (W1) / −$2.90 (W2) per trade against $1.40 commission →
negative GROSS in both windows: not a costs story, and queue priority
cannot rescue a signal whose fills are self-selected against it. The two
non-adjacent months agree to within 0.024 PF — regime-independent.

**Consequence (per the pre-registered verdict rule, verbatim):** maker-side
execution does not rescue this signal; the OBI/CVD family is dead in both
its taker (Round 5) and maker (this round) forms, and no further
execution-style variant should be tried on it without a genuinely
different signal, not just a third fill convention.

---

# Round 21 — GEX vol-regime toggle: the live gex_strategy.py engine as deployed
(registered 2026-07-16, before running — code shipped in e08b270 behind an
armed kill switch; this round decides whether the switch may ever be lifted)

**Mechanism (stated before any run).** Dealers long gamma (net GEX > 0) hedge
against price moves — sell rallies, buy dips — suppressing volatility and
creating intraday mean-reversion toward fair value. Dealers short gamma
(net GEX < 0) hedge WITH moves, amplifying them — favoring continuation of
range breaks. Near-zero net GEX carries no reliable hedging pressure. The
JARVIS-side pre-check (research/gamma_rv_precheck.py, SqueezeMetrics daily,
2011-2026) found the vol-suppression claim SURVIVES its vol-persistence
control at daily frequency. This round tests the INTRADAY, TRADEABLE form —
the exact rules now live in gex_strategy.py. Prior related verdicts stand:
Round 2 C3 (UNCONDITIONAL VWAP reversion) FAILED; C2 (unconditional ORB)
FAILED; Round 18 (daily gamma-REVERSAL, a different claim) FAILED. The new,
registered claim is strictly the GEX CONDITIONING of these entry shapes.

**Data.** oos/data/{ES,MES}_5min.csv (Databento, ends 2026-06-06). Daily net
GEX: oos/data/squeeze_dix_gex.csv (SqueezeMetrics SPX series, 2011-05-02 →
2026-07-09; sign convention > 0 = dealers long gamma, matching uw_gex.py).
Judged window: 2011-05-03 → 2026-06-06 on ES. MES (2019-05→) exploratory.
Proxy caveat registered up front: the series is SPX dealer gamma; the live
feed uses per-proxy UW greek-exposure (MES→SPY). Judged on ES only.

**Frozen rules (mirror gex_strategy.py + the engine's bracket defaults).**
Regime label: GEX_t (known at close of day t) governs session t+1 — strictly
forward, no leakage. neutral band = 0.25 × rolling 250-day median |GEX|
(min 60 obs; mirrors uw_gex.classify_gex with the endpoint's ~250-row
history). |GEX_t| inside band → session t+1 takes NO entries.
RTH only: bars 09:30–15:55 ET; entries signal on bar close 09:35–15:30,
filled at the NEXT bar close; hard flatten at the 15:55 bar close. One
position at a time per symbol; 1 contract.
POSITIVE-gamma session — VWAP-MR: session VWAP = cum(close×vol)/cum(vol)
from 09:30 (the engine's _session_vwap uses closes, not typical price);
ATR(14) on 5-min bars (rolling simple mean of TR, oos/candidates.py _atr).
(close − vwap)/ATR ≤ −1.0 → LONG; ≥ +1.0 → SHORT (GEX_MR_ATR_DEV=1.0).
NEGATIVE-gamma session — breakout: close > max(high of prior 20 RTH bars) →
LONG; close < min(low of prior 20 bars) → SHORT (GEX_BREAKOUT_LOOKBACK=20;
the live 0.5× risk haircut is sizing-only and does not change the signal).
Exits (both legs): stop = entry ∓ 2×ATR(14 at signal bar), target = entry ±
3×ATR (ATR_STOP_MULT=2.0 / ATR_TARGET_MULT=3.0 live defaults); stop and
target checked on bar high/low, BOTH-hit-same-bar counts as the STOP
(conservative); else 15:55 flatten. Costs: round-trip commission (ES $4.00,
MES $1.40) + 1-tick slippage per side. Judged net at 1 tick.

**Judged cells (named in advance).**
PRIMARY: ES, all regime-toggled trades pooled (MR on positive days +
breakout on negative days) — the engine as it would trade.
SECONDARY-A: ES, MR-on-positive-days leg alone.
SECONDARY-B: ES, breakout-on-negative-days leg alone.
PASS bar (each): n ≥ 200, PF ≥ 1.15, one-sided p < 0.05 (t AND 20k
bootstrap, seed 7), ≥ 60% of calendar years positive. MES cells exploratory
only. No other cells count.

**Verdict rule.** PRIMARY passes → the toggle survives its first OOS test;
proceed to a forward paper trial (kill switch still armed for live).
PRIMARY fails → ENTRY_ENGINE=gex must NOT trade — entries stay locked; a
passing SECONDARY alone is a lead for a future registration, not a license.
No re-tuning of the frozen parameters in either case.

## Round 21 — results (2026-07-16, oos/round21_gex_regime.py, round21_results.json)

**Verdict: FAIL, decisively.** The judged PRIMARY (ES pooled) misses every
dimension of the PASS bar:

| cell (ES, judged) | n | total $ | PF | t | p (one-sided) | boot | yrs+ |
|---|---|---|---|---|---|---|---|
| PRIMARY pooled | 12,382 | −513,247 | 0.809 | −8.95 | 1.0 | 1.0 | 0% |
| SEC-A MR/positive | 11,996 | −516,533 | 0.795 | −9.73 | 1.0 | 1.0 | 0% |
| SEC-B breakout/negative | 386 | +3,286 | 1.02 | 0.15 | 0.44 | 0.45 | 50% |

MES (exploratory) mirrors ES: pooled PF 0.817, t=−6.37, 0% years positive.
Regime mix over the window: 3,259 positive / 180 negative / 437 neutral
sessions (ES) — the MR leg dominates exposure and does all the damage,
losing in ALL 15 calendar years.

**Not a costs story.** ES avg net −$41.66/trade vs $29.00 round-trip cost:
avg GROSS is −$12.66/trade. The signal loses before commissions and
slippage exist; no execution improvement can rescue it.

**Reading.** The daily vol-regime DESCRIPTION (JARVIS pre-check: positive
GEX → lower next-day RV, survives the vol-persistence control) is not a
tradeable intraday edge in this form: fading 1-ATR VWAP stretches on
vol-suppressed days at a 2:3 ATR bracket is structurally the same dead
trade Round 2's unconditional C3 was — GEX conditioning changed almost
nothing (PF 0.795 conditioned vs the C3 family's known-dead profile). The
negative-gamma breakout leg is a coin flip on n=386. Implementation note:
the breakout lookback was resolved to WITHIN-session bars (conservative,
no overnight-gap pseudo-breakouts), decided before results were seen.

**Consequence (per the pre-registered rule).** ENTRY_ENGINE=gex is
falsified as deployed and must not trade: config default flipped to
ENTRY_ENGINE=off (no entry signals at all; open positions still managed) —
as of this round NO candidate entry strategy in this repo has survived OOS
testing. gex/legacy remain opt-in for research runs only. The kill switch
stays armed regardless.

---

# Round 22 — hidden-liquidity absorption (iceberg defense) on MES MBO
(registered 2026-07-16, before the detector was written or either window
scanned; uses ONLY data already owned — no new purchase)

**Mechanism (stated before any data is touched).** An institution working a
large passive order hides most of its size: the DISPLAYED depth at its price
stays modest while fills keep landing there and the level refuses to deplete
(iceberg refills). That behavior is invisible to displayed-book features —
Round 5/20's OBI measured displayed size (dead in taker and maker forms) and
CVD measured aggressor flow (same family, dead) — but it is directly
observable in MBO: repeated fill events at one price whose level survives
them. Absorption at/below the bid is a defended floor (institutional buyer)
→ LONG; at/above the ask is a defended ceiling → SHORT. Unlike the
seconds-decay OBI alpha, a defended level persists for minutes — the first
hypothesis in this book whose horizon plausibly tolerates ~1 s retail
latency. Distinctness claim: this is revealed-HIDDEN-liquidity flow, not
displayed-size imbalance, not aggressor-volume sign; it is not a member of
any family this file has killed.

**Data.** The two owned MES MBO windows (2026-01-06..02-06,
2026-05-06..06-06), judged separately, both must pass — same two-window
discipline as Round 20. The nightly T+1 MBO forward capture becomes the
forward-confirmation window if both pass.

**Frozen detector (all parameters fixed here, chosen from mechanism logic
before any scan; no sweeps).** Per price level P and side S, over a rolling
120 s window: absorption fires when (a) fills at (P,S) total ≥ 100 contracts
across ≥ 5 distinct fill events, (b) the displayed size at (P,S) never
reaches zero inside the window, and (c) P is within 4 ticks of the current
touch on side S. Rationale anchors: 100 contracts ≈ 25× the MES median
trade; 5 events excludes single sweeps; 120 s matches the minutes-scale
defense horizon; 4 ticks keeps the level battle-relevant.

**Frozen entry/exit (taker; the maker variant is NOT licensed by this
registration — Round 20 closed execution-variant fishing).** Signal
evaluated on the 1 s grid (Round 5 convention). Entry at the next 1 s
snapshot, crossing the spread (buy ask / sell bid). Bracket from entry:
stop = 1.0×, target = 1.5× the trailing 30-min σ of 1 s mid prices (same σ
construction as Round 20's amendment), both snapped to ticks (min 1);
stop-first on both-hit bars/events. Max hold 15 min → taker exit. RTH only
(09:35–15:30 entries, hard flatten 15:55 ET). One position at a time,
1 contract. Re-arm: a level that already produced an entry cannot re-signal
for 10 min. Costs: $1.40 RT commission + 1-tick slippage per side (taker
both ways — the conservative convention).

**PASS bar (tick-level convention).** On EACH window separately: n ≥ 1000
… relaxed to n ≥ 500 IF the detector's event rate makes 1000 structurally
impossible at these frozen thresholds (disclosed now: absorption is rarer
than OBI z-crossings; the n floor is not license to loosen the detector) —
with PF ≥ 1.10, one-sided p < 0.05 (t AND 20k bootstrap seed 7). BOTH
windows must clear. n < 500 in either window → UNDERPOWERED verdict: report,
do not judge, do not re-tune; the forward capture accumulates until n
suffices.

**Verdict rule.** PASS both → first surviving intraday entry candidate;
forward-confirm on the accumulating T+1 capture before any live arming
(kill switch / ENTRY_ENGINE=off stay as they are regardless). FAIL either →
the absorption family is dead at these thresholds; no re-tuning, and the
next registration must again be a different mechanism, not a parameter
variant of this one.

## Round 22 — diagnostic-first amendment (2026-07-16, BEFORE any P&L was
## computed; Round 18 "diagnostic-first" precedent)

Full disclosure of what was observed before this amendment: (1) a smoke run
of the as-registered sim produced ZERO signals in 20M messages — traced to
a UTC-vs-ET day-bounds bug (fixed; the bug gated ALL scans off, so no
outcome information leaked); (2) a base-rate diagnostic over W1's first
three RTH sessions (funnel counters ONLY, no trades simulated, no P&L)
measured near-touch level activity at: median 125 fill events / 270
contracts per 120 s window, and 82% of RTH seconds satisfying the
registered ≥100-contract/≥5-event gate. The registered detector is
therefore DEGENERATE — it fires on ordinary MES touch churn and cannot
measure the registered mechanism (hidden refills). No P&L, win rate, or
directional statistic existed when this amendment was written.

Amended detector (replaces the volume/event gate; conditions (b)/(c) of
the original stand):
  (a′) fills at (P,S) total ≥ 300 contracts across ≥ 5 fill events in the
       120 s window (raised from 100 to sit above the measured p90 of
       ordinary churn), AND
  (b′) REFILL RATIO ≥ 3: windowed fill volume ≥ 3× the maximum DISPLAYED
       size observed at the level across those fill events — the actual
       iceberg signature (consumed ≫ displayed). Displayed size is sampled
       at each fill event (post-fill), a documented approximation of the
       window max.
Everything else (120 s aliveness, ≤4 ticks from touch, 10-min re-arm,
entry/exit/costs, PASS bar incl. the UNDERPOWERED escape, both-windows
discipline) is unchanged. One further duty-cycle diagnostic (signal-rate
only, still no P&L) is licensed to CONFIRM non-degeneracy; if the amended
detector still fires >5% of RTH seconds the round is declared UNTESTABLE
as specified rather than re-tuned again.

## Round 22 — outcome (2026-07-16): UNTESTABLE at level granularity.
## No P&L was ever computed in this round.

The licensed duty-cycle diagnostic measured the amended detector at 0.0000%
of RTH seconds (0 hits over three sessions) — from 82% before the amendment
to zero after it. Diagnosis: the measurement OBJECT was wrong, not the
mechanism. On CME, an iceberg is a per-ORDER phenomenon (an order id whose
cumulative fills exceed its displayed size, replenishing via modify);
per-LEVEL accounting drowns any hidden component in MES's thick ordinary
depth (no level clears consumed ≥ 3× displayed while staying alive 120 s).

A final P&L-blind feasibility count (oos/_r23_feasibility.py — order-id
fill/display ratios only, no direction, no outcomes) found TRUE per-order
icebergs on MES are real but scarce and small: ~12 per session at
cum_fill ≥ 2× max_displayed with ≥ 20 contracts filled; median hidden
volume 37 contracts. Two structural conclusions, recorded for the next
registration: (1) a per-order MES iceberg round would be UNDERPOWERED by
construction (~250 trades/window vs the 500 floor); (2) the institutional
hiding this mechanism needs lives in the FULL-SIZE ES book, not the micro —
any viable absorption hypothesis is an ES-book-signal → MES-execution
design (prices arb-locked), which is a genuinely different registration,
not a parameter variant of this one. Round 22 is closed UNTESTABLE; the
absorption MECHANISM remains unfalsified and unconfirmed.

---

# Round 23 — per-ORDER iceberg absorption in the FULL-SIZE ES book,
# executed on MES (registered 2026-07-16, before any ES tick data was
# pulled; $0 data cost — trailing-month GLBX L3 is plan-covered, verified
# by quote before download, and the account holder has directed that no
# further Databento money be spent — any nonzero quote aborts the pull)

**Mechanism.** Same absorption thesis as Round 22 (an institution defending
a price reveals hidden size: fills at one ORDER exceed what it ever
displayed, refilling via modify), corrected on two counts Round 22's
diagnostics established with zero P&L observed: (1) the measurement object
is the ORDER, not the price level; (2) the institutions hide in the
full-size ES book, not the retail-dominated micro (MES: ~12 icebergs/
session, median 37 contracts — structurally underpowered). ES and MES are
cash-settled to the same index and arb-locked to within a tick, so an
ES-book signal prices MES execution directly.

**Data ($0, quote-verified per pull).** ES.v.0 MBO, GLBX.MDP3, trailing
month 2026-06-17 → 2026-07-15 (one file per day, per-day $0 quote check,
any nonzero → skip + disclose). DISCLOSED LIMITATION: one month, one
regime — a PASS here is promising-not-proven and the nightly T+1 forward
capture (extended to ES.v.0 from 2026-07-17, also $0/day-guarded) is the
mandatory second window before anything arms. This is the same
single-month honesty Round 5 registered.

**Phase A — mechanical threshold fixing (P&L-blind, licensed here).**
Iceberg CANDIDATE: an order with ≥1 size-uptick modify (refill), cumulative
fills ≥ 2.0× its maximum displayed size (literature-standard ratio,
frozen), and cumulative fills ≥ 25 ES contracts. Phase A scans the window
ONCE recording only candidate counts and their cum-fill distribution — no
prices beyond grouping, no direction, no outcomes. The SIGNAL threshold is
then fixed mechanically: cum_fill* = max(25, P75 of the Phase-A candidate
cum-fill distribution). If Phase A finds < 8 candidates/session on average,
the round is declared UNDERPOWERED before any P&L and defers to the
accumulating forward capture. No other quantity may be read from Phase A.

**Phase B — frozen trading rules.** Signal: the moment an order FIRST
satisfies (refill ≥1, ratio ≥ 2.0, cum_fill ≥ cum_fill*) — online-causal,
no lookahead — while resting within 4 ticks of the ES touch on its side.
Direction: bid-side iceberg → LONG, ask-side → SHORT. Entry: taker at the
next 1 s snapshot's opposite touch (ES prices, MES economics — $5/pt,
$1.40 RT commission, MES 0.25 tick; the ≤1-tick occasional ES/MES touch
discrepancy is absorbed by the 1-tick-per-side slippage convention,
disclosed). Bracket: stop 1.0× / target 1.5× trailing 30-min σ of 1 s ES
mids, tick-snapped, stop-first; 15-min max hold; RTH entries 09:35–15:30
ET, flatten 15:55; one position at a time; one signal per iceberg order
id; 10-min re-arm per price. Costs: $1.40 RT + 1 tick per side.

**PASS bar.** n ≥ 500 (single-window tick-level floor; n < 500 →
UNDERPOWERED, defer to forward capture, no re-tune), PF ≥ 1.10, one-sided
p < 0.05 (t AND 20k bootstrap seed 7).

**Verdict rule.** PASS → promising-not-proven; the ES forward capture must
independently clear the same bar before ENTRY_ENGINE ever exposes it (kill
switch and ENTRY_ENGINE=off stand regardless). FAIL → the absorption
mechanism is dead in the venue where it demonstrably exists — the family
closes for good, and with it, per this file's own accumulated record, the
last untested intraday-microstructure family this data can support.

## Round 23 — results (2026-07-16, oos/round23_reduce.py +
## round23_phase_ab.py, round23_results.json)

Phase A (mechanical, P&L-blind): 25 sessions, 5,488 iceberg candidates
(219.5/session — the ES-vs-MES premise confirmed at ~80:1 on a same-day
cross-check: ES 165 vs MES 2 on 2026-07-10), cum_fill* fixed at 66
contracts (P75 rule). Viability floor cleared 27×.

Phase B (frozen rules): n=409, PF 0.588, t=−4.43, win 36.4%, −$2,961.35
net, p=1.0 (t and bootstrap). Avg −$7.24/trade against $3.90 costs → avg
GROSS −$3.34: negative before costs, the third distinct signal family to
show the same adverse-selection failure shape at ~1 s retail latency
(after OBI/CVD maker fills and GEX-conditioned entries).

**Formal verdict: UNDERPOWERED** (n=409 < 500) — per the registered escape,
NO judgment is entered, nothing is re-tuned, and the nightly $0 ES forward
capture (reduce-and-delete, same artifacts) accumulates until n ≥ 500
(~6 further sessions), at which point the combined sample is judged at the
registered bar. Recorded candidly alongside the formal status: the point
estimate makes an eventual PASS (PF ≥ 1.10, p < 0.05) arithmetically
implausible; this note exists so a later reader does not mistake deferral
for hope.

---

# Round 24 — Market/Volume-Profile value-area rotation (registered 2026-07-17,
# before the runner was written; genuinely untested family — NOT a member of
# the dead VWAP-MR / OBI / GEX families)

**Mechanism (auction market theory; Steidlmayer/Dalton, *Mind Over Markets*).**
A session's VALUE AREA — the price band containing 70% of the day's volume
around the point of control (POC, the max-volume price) — marks where the
market agreed on fair value. On BALANCED (rotational, non-trend) days, the
next session opens inside prior value and price rotates AROUND value,
reverting from the value-area extremes back toward the POC as responsive
participants fade moves away from perceived fair value. This is distinct
from Round 2 C3 (fade intraday-VWAP deviation) and Round 21 (GEX-conditioned
VWAP-MR): the reference levels are the PRIOR SESSION's settled value-area
boundaries (lookahead-safe by construction), and the trade is gated on a
balanced open — the market-profile definition of a rotational setup, not an
add-on filter.

**Institutional reasoning.** Value-area edges are where responsive liquidity
(mean-reverting institutional flow) concentrates on non-trend days; the POC
is the session's fair-value magnet. This is the same responsive-vs-initiative
framework CME's own Market Profile literature describes.

**Data.** PRIMARY (multi-year OOS): oos/data/ES_5min.csv (Databento GLBX,
2010→2026-06-06, owned, $0). Value area is a session-level construct, so
5-min volume bucketing is the standard, adequate granularity — finer tick/MBP
data would add nothing to a DAILY level and would cost money across years.
Data-usage plan (per account-holder directive, all $0): the free trailing-
month GLBX L2/L3 (MBO) is reserved as the FORWARD-confirmation window and to
build a finer intraday developing-profile in live use; UW GEX/greek-exposure
is reserved as a registered regime-conditioning SECONDARY only if the primary
passes (conditioning a dead-on-its-own signal is not itself tested here).

**Frozen rules (ES, RTH 09:30–16:00 ET; judged net at 1-tick slippage).**
Prior-session profile: bucket each RTH 5-min bar's volume at its CLOSE price
on a 1-point ES grid; POC = max-volume bin; expand outward from POC (adding
the larger-volume of the two adjacent unclaimed bins each step) until ≥70% of
session volume is enclosed → VAH (upper), VAL (lower). These settle at the
16:00 close and become the NEXT session's reference (strictly forward).
Balanced-open gate: today's 09:30 open must satisfy VAL ≤ open ≤ VAH (prior
value) — else NO trades today (an out-of-value open is initiative/trend, not
rotational). Entry (09:35–15:30 ET, one position at a time): first 5-min
close at/above prior VAH → SHORT; at/below prior VAL → LONG. Target: prior
POC. Stop: 1.0×ATR(14, 5-min) beyond the entered edge (SHORT stop = VAH +
1·ATR; LONG stop = VAL − 1·ATR). Hard flatten 15:55 ET. Costs: $4.00 RT
commission + 1-tick slippage per side (ES).

**PASS bar (standard entry-edge bar).** n ≥ 200, PF ≥ 1.15, one-sided
p < 0.05 (t AND 20k bootstrap seed 7), ≥ 60% of calendar years positive.
Any fail → dead, no parameter sweep. A single PASS still needs forward/unseen
confirmation before sizing capital (family-wise discipline).

**Verdict rule.** PASS → first surviving intraday entry candidate; forward-
confirm on the accumulating free GLBX capture and consider the UW-GEX
conditioning secondary. FAIL → value-area rotation is dead at these frozen
levels; no re-tuning, next registration must be a different mechanism.

## Round 24 — results (2026-07-17, oos/round24_value_area.py, round24_results.json)

**Verdict: FAIL.** ES n=4,843, PF 0.799, t=−4.49, win 24.1%, −$85,053,
5.9% of years positive (p=1.0 t and bootstrap). MES mirrors it (PF 0.857,
t=−2.16, 12.5% years+). Fails every PASS-bar dimension. Fading prior-day
value-area edges gets run over: on the balanced-open days that qualify, price
breaks THROUGH the faded edge more often than it rotates back to the POC, so
the near stop (1 ATR beyond the edge) is hit far more than the distant POC
target — the same "reversion target too far, trend risk too near" failure
that killed Round 2 C3 and Round 21.

**Process note — a fill-modeling artifact was caught and corrected BEFORE any
verdict was recorded (integrity disclosure).** The first two runs printed
PF ~15, t ~89, 100% years positive, +$8.8M — an obvious too-good-to-be-true
result treated as a bug, not a discovery. Root cause: entries were booked at
the next bar's CLOSE, which can float past the fixed VAH/VAL level, leaving a
short's stop (VAH+1·ATR) BELOW its entry; hitting that "stop" then booked a
PROFIT in the (exit−entry)·side math (a stop-loss recorded as a win), and
momentum breaks that retraced intrabar manufactured those fake wins en masse.
Fix (committed with this result): enter at the next bar's OPEN, and admit only
valid geometry — entry strictly between target and stop in the trade's favour
(degenerate fills skipped, not booked). The corrected run is the FAIL above.
Recorded so no future round repeats the artifact or mistakes it for edge.

---

# Round 25 — Failed-auction (false-breakout rejection) fade (registered
# 2026-07-18, before the runner was written)

**Mechanism (auction market theory).** When price breaks beyond a reference
extreme (prior-day high/low) but FAILS to find acceptance — no follow-through,
a bar closes back inside the prior range — it is a failed auction: the
breakout attracted responsive sellers (above) / buyers (below) who reject the
excursion and push price back toward value. Fade the failure. Distinct from
Round 24 (which fades at value-area EDGES regardless of a breakout); this
requires a breakout THEN a rejection, the classic "false break" the CME
Market Profile literature calls a failed auction.

**Data.** oos/data/ES_5min.csv (owned, $0), 2010→2026-06-06. MES exploratory.

**Frozen rules (ES, RTH 09:30–16:00 ET; net at 1-tick slip; $4 RT comm).**
Prior-session references settled at 16:00: PDH, PDL, PDMID=(PDH+PDL)/2.
Track today's running RTH session high/low. SHORT trigger: session high has
exceeded PDH at some point today AND a 5-min bar (09:35–15:30 ET) closes back
below PDH → failed auction above. LONG trigger: session low below PDL AND a
bar closes back above PDL. One trade per direction per day, one position at a
time. Entry: NEXT bar OPEN. Target: PDMID. Stop: SHORT = today's session high
+ 0.25·ATR(14); LONG = session low − 0.25·ATR(14) (just beyond the failed
extreme). Valid-geometry-only (Round-24 rule): admit the trade only if entry
is strictly between target and stop in its favour, else skip. Both stop+target
in one bar → STOP (conservative). No exit on the entry bar. Hard flatten 15:55.

**PASS bar (standard).** n ≥ 200, PF ≥ 1.15, one-sided p < 0.05 (t AND 20k
bootstrap seed 7), ≥ 60% years positive. Fail → dead, no sweep.

---

# Round 26 — Overnight inventory reversal (registered 2026-07-18, before the
# runner; Topstep-LEGAL — an RTH fade, NOT an overnight hold)

**Mechanism.** A one-directional Globex (overnight) session leaves participants
with skewed inventory: an overnight rally leaves the market "long inventory"
that responsive flow corrects after the RTH open, and vice-versa. This is the
RTH-legal cousin of the overnight-drift family (Rounds 3/4/7/8/11/12): drift
HOLDS overnight (Topstep-illegal); this FADES the overnight move during RTH and
is flat by the close. Bogousslavsky (2016, "Infrequent Rebalancing") and the
inventory-risk microstructure literature (Ho-Stoll) motivate the reversion.

**Data.** oos/data/ES_5min.csv (owned, $0). MES exploratory.

**Frozen rules (ES, RTH; net at 1-tick slip; $4 RT comm).**
Overnight move ON = (RTH 09:30 open) − (prior RTH 16:00 close). Signal only
when |ON| is in the TOP TERCILE of the trailing 60-session |ON| distribution
(a genuinely skewed overnight, not noise). ON > 0 (rallied) → SHORT at the
09:30 open; ON < 0 → LONG at the 09:30 open. One trade per day. Target: the
prior RTH close (full reversion of the overnight move). Stop: entry ∓ 1·ATR(14,
5-min) against the trade (SHORT stop above, LONG stop below). Valid-geometry-
only. Both-hit → STOP. Hard flatten 15:55 ET.

**PASS bar (standard).** n ≥ 200, PF ≥ 1.15, one-sided p < 0.05 (t AND 20k
bootstrap seed 7), ≥ 60% years positive. Fail → the RTH-legal inventory fade
is dead; the overnight-drift family (illegal hold) stays the only ever-passer.

## Rounds 25 & 26 — results (2026-07-18, oos/round25_26_auction_inventory.py,
## round25_26_results.json)

**Round 25 (failed-auction fade): FAIL.** ES n=2,881, PF 0.85, t=−2.89,
win 40.5%, −$95,383, 17.6% years positive. Fading false breakouts beyond
prior-day extremes loses — the "failed" auction resumes often enough that
the beyond-extreme stop is hit more than the PDMID target. Dead, no sweep.

**Round 26 (overnight inventory reversal): passes the mechanical bar but is
NOT ACTIONABLE — killed by sensitivity analysis.** At the registered 1-tick
slippage it clears every dimension (ES n=1,367, PF 1.262, t=2.33, p=0.010 t /
0.008 boot, 70.6% years positive) — the FIRST intraday signal in 26 rounds to
do so. But the disciplined stress tests the account holder asked for falsify
it as a real edge:
  1. REGIME-CONCENTRATED. Split by decade: 2010–2019 nets ≈ −$116 (flat/
     negative, losing in 2011/12/16/17/19); ALL $56k of profit is 2020–2026.
     The "edge" is a post-COVID high-vol-regime artifact, absent for the first
     decade of the sample.
  2. SLIPPAGE-FRAGILE. Entry is AT THE 09:30 RTH OPEN — the worst-slippage
     moment of the session (opening auction, wide spreads), so 2 ticks is the
     HONEST execution model, not 1. At 2-tick slippage the result collapses:
     PF 1.262→1.091, t 2.33→0.91, p 0.010→0.181, years-positive 70.6%→41.2%.
     A t of 0.91 is indistinguishable from noise.

**Verdict: NOT ACTIONABLE.** The single mechanical PASS in the whole program
survives only on an optimistic 1-tick cost assumption for an open-entry trade
and only in the recent regime; it does not clear realistic execution costs and
is not robust across regimes. Not moving the goalpost — this is the
pre-requested sensitivity/robustness analysis doing its job. Recorded honestly
as a fragile lead, NOT an edge: if pursued at all, only via forward paper-
logging at realistic open-fill costs, never sized on this backtest.

**Program status after 26 rounds: still ZERO robust, actionable intraday
edges.** The overnight-drift HOLD (Topstep-illegal) remains the only signal
that survives realistic costs across regimes.

---

# Full-ledger re-evaluation (2026-07-20) — account holder wants to stay on
# Topstep; re-read all 26 rounds + the original trial end-to-end before
# proposing anything new, rather than re-litigating any dead family

**Re-confirmed, not re-tested (no data touched for this section):**
- Topstep-legal overnight-drift is EXHAUSTED (Round 12 synthesis): 0/4
  instruments (NQ/MNQ/RTY/GC) pass the 18:00-entry variant. Only the
  16:00-entry variant (violates the flatten rule) has ever passed, Nasdaq-
  linked only. No new instrument or parameter is licensed on this family.
- Every single-bar OHLCV pattern family tested (ORB, VWAP-reversion, SMA/RSI
  both strength tiers, intraday momentum, overnight-gap fade, regime-
  transition-confluence proxy, value-area rotation, failed-auction fade) is
  dead, cross-validated against outside literature (Round 14 addendum).
- Every order-flow-imbalance construction tested (taker OBI/CVD Round 5,
  maker/queue-fill Round 20, per-level iceberg Round 22 UNTESTABLE, per-order
  iceberg Rounds 22/23 UNDERPOWERED but decisively negative point estimate)
  is dead or effectively dead. OFI decay at ~1s is independently reconfirmed
  by 2025-26 literature (Takahashi 2025 SVAR on ES E-mini BBO: OFI/price
  shocks "dissipate almost entirely within a second") — nothing left to
  retry here without a genuinely different signal.
- Every GEX/dealer-gamma construction (daily sign Round 6, live engine
  regime-toggle Round 21, intraday-magnitude fade Round 18) is dead or
  inverted-in-window. The one untested leg — Round 18's negative-gamma
  ("momentum-following") side, explicitly stood aside on rather than tested
  — is registered below as Round 27, not a rescue of Round 18's dead fade.
- Overnight inventory reversal (Round 26) is the only mechanical PASS in the
  whole program and remains NOT ACTIONABLE (regime-concentrated,
  slippage-fragile). Not re-opened.

**The one mechanism in this file that is genuinely UNTESTED, not dead —
MOC/closing-auction imbalance drift (Round 17).** Re-checked Databento's own
schema docs (2026-07-20): both legs (NYSE `imbalance` on `XNYS.PILLAR`,
Nasdaq NOII on `XNAS.ITCH`) are real, purchasable products, timestamped
exactly to this round's frozen 15:50 ET signal. Status updated above
(still DATA-BLOCKED on cost confirmation, not on mechanism or data
existence) — this is the highest-value next step if the account holder
wants to spend to find out, because unlike everything else in this file it
has never been run, not merely failed.

**Considered and NOT registered (2026-07-20): large-trade / sweep
detection.** Motivation: OFI's own seconds-scale decay is well-established
(Rounds 4/5/20 + the literature above), but a handful of 2025-26 papers
distinguish large/aggressive trades from ordinary OFI as a separately
informative signal (Jain et al., intermarket sweep orders in EQUITIES carry
disproportionate information share). **Rejected before any data pull**: the
most recent and most directly relevant paper (arXiv 2607.01198, "When large
trades are not news," 2026) finds a large trade's informativeness is NOT
unconditional — it depends on the prevailing liquidity-tail regime (thin-
tailed liquidity demand → large trades are informative; heavy-tailed →
they're liquidity shocks that get quickly reverted, not signal). Any
tradeable version of this idea would therefore need a live liquidity-regime
classifier bolted on before the entry signal even fires — structurally the
same regime-conditioning shape that already failed three times in this file
(Round 6 GEX-sign, Round 18 gamma-magnitude, Round 21 live-engine GEX
toggle — the last two inverted or wrong-signed, not just insignificant).
Also, the sweep-order literature itself is EQUITY market-structure specific
(Reg NMS order-routing fragmentation creates the ISO mechanism); CME futures
do not have an equivalent routing structure, so the mechanism may not even
transfer. Not registered — flagged here so it is not re-proposed later
without this context, same convention as the FOMC/CPI-generalization
rejection above.

---

# Round 27 — UW dealer-gamma NEGATIVE-tercile leg (momentum-following),
# the untested half of Round 18 (registered 2026-07-20, before any new pull;
# a NEW hypothesis per Round 18's own text, not a sign-flip rescue of it)

**Why this is not a rescue.** Round 18 froze and judged exactly one leg:
fade the trailing 10-min move when `gamma_per_one_percent_move_oi` is in the
top (most-positive) tercile; the bottom tercile was explicitly "stand aside
— research does not establish a clean momentum-side edge, only the
dampening side," and its own writeup names a sign-flip test as "a NEW
hypothesis (would need its own pre-registration)" if ever wanted. This round
IS that pre-registration, for the OTHER, never-tested leg: momentum
continuation (not reversal) when dealers are net SHORT gamma. Round 18's own
top-tercile result (decisively wrong-signed, PF 0.767, p=0.998) is NOT
reused, re-judged, or re-interpreted here.

**Frozen trade rule (mirrors Round 18's structure exactly except direction
and tercile; Topstep-legal, no overnight hold):**
1. Same underlier (SPX/ES judged; NDX/NQ exploratory), same field
   (`gamma_per_one_percent_move_oi`, OI-based, not `_vol`/`_dir`), same
   trailing-5-trading-day rolling tercile split, same execution proxy
   (nearest 5-min bar within 10min tolerance).
2. Entry trigger: current snapshot in the BOTTOM tercile (strongly negative
   net gamma) AND 09:30–15:00 ET AND no position open. Direction: WITH the
   trailing 10-minute move (if price rose, go long; if it fell, go short;
   flat → skip) — momentum, the mirror of Round 18's fade. Top tercile →
   stand aside (Round 18's leg is dead; not re-tested here). Middle tercile
   → no trade.
3. Exit: same three frozen hold times as Round 18 (10min primary/judged,
   30min + 60min exploratory), all hard-capped at 15:59 ET flatten.
4. Costs: identical to Round 18 (ES/NQ $4.00 RT + 1-tick slippage both
   sides).

**PASS bar / actionability tier.** Identical constraint to Round 18: UW's
intraday gamma history was ~90-120 trading days as of 2026-07 (< 1 calendar
year) → **whatever this round finds is EXPLORATORY ONLY, not actionable**,
regardless of PF/p/n, until the accumulating `com.jarvis.uwcapture` history
clears a full year (~Oct 2026, same maturity date as Round 14's UW
market-tide leg — both should be revisited together then). Registered now,
before running, purely so it is ready to test the moment the data matures
rather than being designed after looking at a year of results. n ≥ 200
(primary 10-min cell, ES only), PF ≥ 1.15, one-sided p < 0.05 (t AND 20k
bootstrap) is the bar that will apply once the tier allows actionability;
reported-only numbers before then use the same bar for comparison purposes,
not as a trading decision.

**Data.** `oos/round18_gamma_scores.json` (190 ticker-days already pulled,
2026-02-26→2026-07-08) can be re-used for the exploratory read AS-IS — this
round adds no new UW pull. A full re-run once the capture matures past 1
year will need a fresh pull covering the extended window.

**Runner.** Extend `oos/round18_gamma_reversal.py` with the mirrored
direction/tercile rule (or a sibling `oos/round27_gamma_momentum.py`) —
not yet written. [Update 2026-07-20: written, see status note below —
oos/round27_gamma_momentum.py reads oos/round18_gamma_scores.json verbatim.]

## Round 27 — BLOCKED, not failed (2026-07-20: UW subscription cancelled by
## the account holder, same day this round was registered)

The account holder cancelled the Unusual Whales subscription today, before
this round's own maturity clock (~Oct 2026, ~90-120 usable days as of
2026-07) could run out. The intraday gamma capture that was supposed to
mature into an actionable sample has stopped accumulating. `oos/
round27_gamma_momentum.py` exists and still reads the frozen `oos/
round18_gamma_scores.json` cache (whatever history it holds as of the
cancellation date) — running it now would still only produce an
EXPLORATORY-tier read per this round's own PASS-bar tiering, same
constraint as before, just with a permanently-capped n instead of a growing
one. **Status: BLOCKED indefinitely, not dead** — a subscription
cancellation is not a test result and must not be conflated with the DEAD
list (CLAUDE.md) or reported as a verdict either way. Reopen only if UW
service is resubscribed (accumulation restarts from zero, new maturity
date) or an equivalent dealer-gamma data source is sourced elsewhere.

---

**Honest bottom line for the account holder (2026-07-20):** after re-reading
every registered round end-to-end, there is still no proven, actionable,
Topstep-legal intraday edge. That has not changed by re-reading it — it is
what a correct re-read of an honestly-run 26-round program looks like. The
two live paths forward were (1) spend to unblock Round 17 (MOC imbalance —
get the exact Databento cost, then decide), and (2) wait for the UW gamma
and market-tide captures to clear ~1 year of history (~Oct 2026) so Round 27
and Round 14 stop being exploratory-only. Nothing on the dead list is
eligible to be revisited by relabeling or re-parameterizing it.

**Update, same day:** the account holder cancelled the Unusual Whales
subscription. Path (2) is now BLOCKED, not merely deferred — Round 14 and
Round 27 lose their data source before maturing (see their own status notes
above) and cannot be reopened without a fresh UW subscription (accumulation
restarts from zero) or an equivalent data source. **Path (1), Round 17 /
Databento MOC-imbalance, is the only live path remaining** in this ledger;
`oos/fetch_round17_imbalance.py` is written and ready to price the exact
pull the moment `DATABENTO_API_KEY` is available. Nothing in the live
trading bot is affected by the UW cancellation: `ENTRY_ENGINE` already
defaults to `off` (Round 21 killed the GEX entry engine), `UW_FLOW_ENABLED`
already defaults to `False`, and every UW-keyed code path (uw_gex.py,
uw_flow.py, marketdata.py's regime-fallback) fails closed/safe on a
dead key by design — no code change was required.
