# FAQ

*[Version française](FAQ.md)*

Everything you need to understand the data shown and to run your own instance.

---

## The data

### Where does the data come from?

From **CBOE's public delayed endpoint** — CBOE operates the US options markets,
so this is the official source for SPX and NDX chains: bid/ask prices, implied
volatility, open interest and volume, for every strike and expiration.

**No account, no API key, no subscription.** The dashboard queries the public
endpoint directly, for free.

### Why a 15-minute delay?

That's the delay on CBOE's free public feed. *Redistributing* real-time data
would require an expensive professional licence — but for personal use, a broker
account is enough (see [Real time via a broker account](#real-time-via-a-broker-account-included-with-the-account)).

**In practice it matters far less than you'd think**: the metric everything is
built on — open interest — is published **only once a day**, in the morning, by
the OCC. Gamma walls, the Gamma Flip and the key levels all derive from it, so
they barely move intraday. The delay really only affects the reference spot
price and the intraday flow.

### How often does the data refresh?

The CBOE feed is regenerated roughly every **60 seconds**, and the dashboard
polls at the same pace during market hours (9:30am–4:15pm New York time).
Outside those hours it sleeps and shows the last known state.

### Why is the "Positioning" tab often empty?

Because it compares open interest from one session to the next, and OI is
published once a day. Until the morning publication happens, there is nothing
to compare. This tab becomes useful after a few days of collection.

### Are levels in index points or futures points?

Either — your choice. The **Index / ES** (or NQ) toggle switches the display.

This matters: the gap between an index and its future is not small (around
+30 points on ES, +150 on NQ). Putting a raw SPX level on an ES chart would
throw everything off. The basis is recomputed on every refresh from put-call
parity, and follows the quarterly roll automatically.

---

## Running your own instance

### Why can't I just use your dashboard?

Two reasons, and the first is the simplest: **you don't need to**. The CBOE
source is free and account-free — your instance will show exactly the same
data.

The second is licensing. If an instance is enriched with optional data
(Databento, or a real-time broker feed), that data is licensed for *personal,
non-redistributable* use. Sharing it would count as redistribution, which is
prohibited and would reclassify the operator as "professional", with the
corresponding fees.

Hence the principle: **the code is shared, the data is not.**

### How do I install it?

You need Python 3.11 or newer, and Git.

```
git clone https://github.com/Darthreign/gex-dashboard.git
cd gex-dashboard
python -m venv .venv
```

Then, depending on your system:

```
.venv\Scripts\pip install -r requirements.txt      # Windows
.venv/bin/pip install -r requirements.txt          # macOS / Linux
```

### How do I run it?

```
.venv\Scripts\python run.py       # Windows
.venv/bin/python run.py           # macOS / Linux
```

Then open **http://127.0.0.1:8050** in a browser.

No configuration needed: the dashboard starts collecting immediately. The
interface is in English or French, detected from your browser language and
switchable with the FR/EN toggle.

### Do I need to leave it running all the time?

No — with one nuance.

The **levels** (GEX, walls, Gamma Flip, HVL) are snapshots of the current
state: they rebuild completely on the first refresh, no matter how long the
program was off. Nothing to catch up on.

The **intraday delta flow**, however, is measured between two consecutive
polls: it can only be captured while the program is running during the session.
Same for the level history, which accumulates over time.

In practice: start it before the US open on days you're trading. Outside market
hours it sleeps and costs nothing.

### Does my data stay on my machine?

Yes, entirely. Everything is stored locally in the `data/` folder (Parquet
format). Nothing is sent anywhere — the dashboard only listens on `127.0.0.1`,
i.e. your own machine.

---

## Understanding the indicators

### GEX (Gamma Exposure)

An estimate of the gamma market makers have to hedge, expressed in **dollars
per 1% move** of the index. Computed strike by strike from open interest and
Black-Scholes gamma.

- **Positive net GEX** → *stabilizing* regime. Market makers sell into strength
  and buy into weakness: volatility is dampened.
- **Negative net GEX** → *destabilizing* regime. They do the opposite, which
  amplifies moves.

### Gamma Flip (or Zero Gamma)

The price level where net GEX **changes sign** — the boundary between the two
regimes above. It's the single most-watched metric in gamma analysis.

It isn't simply read off the chart: the full profile is recomputed across a
grid of hypothetical prices (visible in the **Gamma Profile** tab), then the
crossing is interpolated.

### HVL (High Volatility Level)

Same computation as the Gamma Flip, but weighted by **today's volume** instead
of open interest. Where the Flip describes inherited structure, the HVL
reflects what is actually trading — and therefore being hedged — today.

A wide gap between the two is itself information about the session's flow.

### Call Wall and Put Support

The largest gamma concentrations, **directionally constrained**:

- **Call Wall**: the largest call wall **above** price — resistance.
- **Put Support**: the largest put wall **below** price — support.

That constraint isn't cosmetic. The largest put wall in absolute terms can
easily sit above price, in which case calling it "support" would be meaningless.

### 1D Min and 1D Max

The expected move boundaries for the nearest expiration, derived from the
**at-the-money straddle** price. The straddle *is* the market's own estimate of
the move — no model assumption is involved.

### GEX1 to GEX5

The five strikes with the largest gamma in absolute terms, with no directional
constraint. These are the raw walls, ranked by weight. The **Major Walls only**
checkbox filters out those weighing less than 25% of the largest.

### DEX (Delta Exposure)

The delta equivalent of GEX: the directional exposure market makers carry at
each strike.

### Vanna and Charm (dedicated tab)

Second-order Greeks, which explain hedging flows that gamma alone misses:

- **Vanna** — sensitivity of delta to implied volatility. When IV compresses,
  market makers must buy back delta: this is the mechanism behind slow grinds
  higher with no obvious catalyst.
- **Charm** — decay of delta as **time passes**. This flow is purely mechanical
  and therefore predictable; it explains part of end-of-day drifts and
  expiration-week behaviour.

### The delta flow

An estimate of delta traded, minute by minute, obtained by multiplying each
contract's volume change by its delta.

**Its limitation must be understood**: this feed does not say whether a trade
was buyer- or seller-initiated. So it measures *delta-weighted pressure*, not
true signed order flow. It shows intensity and concentration, not aggressive
direction.

---

## Advanced options (optional, paid)

The dashboard works fully without any of the following.

### History via Databento

Lets you pre-fill several months of daily history (net GEX, Gamma Flip) and
retrieve intraday flow for past sessions.

Billed per unit of data downloaded. The script displays **a cost quote before
any download** and refuses to exceed a ceiling you set (`--max-cost`). Raw
files are kept locally: re-running never re-bills what was already fetched.

Requires a Databento account and the `DATABENTO_API_KEY` environment variable
(see `.env.example`).

Worth knowing: the most recent session's data stays under a "live" licence for
about one business day. A licence error on yesterday's data is normal — just
wait.

### Real time via a broker account (included with the account)

A broker account with dxFeed access — the dashboard is written for tastytrade,
which includes this data at no extra cost — makes the following live:

- the **spot** of underlyings and futures;
- **net GEX recomputed at that spot**, hence the distance to the Gamma Flip and
  the regime read, which go stale within minutes;
- **1-minute bar** recording, and several weeks of **history** retrievable in
  one pass.

**Option chains stay delayed**: they still come from CBOE. Gamma walls and the
Gamma Flip do not move any more for it, since they rest on open interest
published once a day.

Setup: create an OAuth application from the account settings, run
`python -m gex.tt_auth` to obtain a token, then set `TASTYTRADE_CLIENT_ID`,
`TASTYTRADE_CLIENT_SECRET` and `TT_REFRESH` as environment variables — never in
a repository file. Without them the module stays inert and nothing changes.

Opening a brokerage account is a personal and consequential step; the dashboard
works perfectly without one, and this is not a recommendation.

**That data is never redistributable**: it stays on its holder's local instance.
The program enforces this by construction — provenance stamped at write time,
and export restricted to CBOE-sourced data only.

---

## Known limitations

- **15-minute delay** on the free source. This is a structure-reading tool,
  never an execution tool.
- **Open interest is daily.** No provider, free or paid, changes that: the OCC
  publishes it.
- **Trade direction is not observable** in the free feed (see the delta flow
  section).
- **Market maker positioning assumption.** Like every tool of this kind, the
  computation assumes dealers are long calls and short puts. That's a common
  and useful convention, not a measured fact.
- **The CBOE endpoint is not contractual**: its format can change without
  notice. Ingestion is isolated so another source can be plugged in.

---

## Disclaimer

This tool is for **analysis only**. It places no orders, connects to no trading
account, and constitutes neither investment advice nor a recommendation.
Calculations rest on public conventions and on the assumptions stated above,
which may be wrong.

Distributed under the [MIT licence](LICENSE), with no warranty of any kind.
