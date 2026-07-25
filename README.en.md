# GEX Dashboard — Gamma/Delta Exposure analytics (SPX/ES, NDX/NQ)

*[Version française](README.md)* · *[FAQ](FAQ.en.md)* · *[Disclaimer](DISCLAIMER.md)*

[MIT License](LICENSE) — **analysis tool only**: no trading, no execution,
no investment advice. Every instance pulls its own data from CBOE's public
delayed endpoint; this project redistributes no market data.

> ⚠️ **Trading options and derivatives involves a high risk of loss.**
> This tool is provided for educational purposes, without warranty, and does
> not constitute investment advice. Read the [full disclaimer](DISCLAIMER.md)
> before using it.

## Screenshots

| Main view | Gamma Profile |
|---|---|
| ![Main view](docs/screenshots/01-vue-principale.png) | ![Gamma Profile](docs/screenshots/02-gamma-profile.png) |
| GEX/DEX by strike, 0DTE levels, delta flow, history | Net GEX profile vs spot, broken down by expiration |

| Vanna & Charm | Positioning |
|---|---|
| ![Vanna and Charm](docs/screenshots/03-vanna-charm.png) | ![Positioning](docs/screenshots/04-positionnement.png) |
| Second-order Greeks by strike | Open interest change between sessions |

A free, self-hosted alternative in the spirit of SpotGamma: rebuild the
market structure metrics that options dealers' hedging creates — Gamma
Exposure by strike, Gamma Flip (zero gamma), Call/Put Walls, delta flow.

## Data source

CBOE public delayed endpoint (undocumented):
`https://cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json`
(indices prefixed with `_`). One GET returns the full chain — bid/ask, IV,
open interest, volume, Greeks — plus spot. **~15 min delayed**, regenerated
~every 60 s (feed timestamps are UTC). Default underlyings: SPX and NDX;
SPY/QQQ available as fallbacks (`gex/config.py`).

## Assisted install (Claude Code)

If you use [Claude Code](https://claude.com/claude-code), open it in an empty
folder and paste this prompt — it handles everything, including registering the
MCP server:

```
Install the GEX dashboard (SPX/NDX options analytics) on my machine.

Repository: https://github.com/Darthreign/gex-dashboard

Steps:
1. Check that Python 3.11+ and git are available. If either is missing,
   tell me how to install it and stop there.
2. Clone the repository into the current folder and cd into it.
3. Create a .venv virtual environment and install requirements.txt.
4. Run the test suite (pytest tests/ -q) to validate the install:
   62 tests must pass.
5. Adapt .mcp.json to my system: replace the "command" value with the
   ABSOLUTE path to the venv python (Windows: .venv\Scripts\python.exe,
   macOS/Linux: .venv/bin/python). The shipped file contains a relative
   Windows path that does not work elsewhere.
6. Start the dashboard (python run.py) and give me the URL to open.
7. Tell me to restart Claude Code from this folder to activate the
   "gex-data" MCP server, and list the tools it exposes.

Important: no account, API key or subscription is required — data comes from
CBOE's free public endpoint. Do not ask me for any credentials. The
backfill.py (Databento) and tt_auth.py (tastytrade) modules are optional and
paid: ignore them entirely.
```

The MCP server then lets you query your own data in natural language
("analyse the current gamma structure", "where are the NDX walls?").

## Quick start

```
python -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip
.venv/bin/python run.py                          # dashboard on http://127.0.0.1:8050
```

Tests: `.venv/bin/python -m pytest tests/`

## MCP server — query your data in natural language

This is what really sets the tool apart from a plain dashboard: once the MCP
server is active, you can ask Claude directly, and it reads your Parquet files
to answer on **your** data.

```
"Where are the gamma walls on NDX?"
"Analyse the current gamma regime on SPX"
"How has net GEX evolved this week?"
"Show me the delta flow from the last session"
```

⚠️ **The MCP server only activates when Claude Code starts, from the project
folder.** If you have just installed the tool, quit Claude Code and relaunch it
from this folder — otherwise the tools stay invisible. This is the one step
that trips people up during installation.

The [`.mcp.json`](.mcp.json) file registers the server automatically. It
contains a **relative Windows path**: on macOS or Linux, replace the `command`
value with the absolute path to `.venv/bin/python`, otherwise the server fails
without a clear error message.

Tools exposed: `get_gex_summary`, `get_gex_by_strike` (gamma walls),
`get_flow_delta`, `get_history`, `get_reports`, `get_log_tail`.

## Features

- GEX / DEX by strike (±2/4/10 % window), calls/puts breakdown on hover
- 0DTE levels: **Call Wall / Put Wall / GEX3-5** (top gamma strikes),
  **Gamma Flip** (OI-weighted zero gamma), **HVL** (volume-weighted flip)
- Net GEX, P/C ratios, IV skew by expiration, expiry buckets (0DTE/week/month)
- 1-min delta flow (Δvolume×δ proxy) with session picker
- Net GEX & spot-vs-flip history (accumulates automatically while running)
- Optional historical backfill via Databento (`gex/backfill.py`, paid,
  cost quote shown before any download)
- MCP server (`gex/mcp_server.py`) to query the data from Claude
- FR/EN interface (browser language auto-detected, manual toggle remembered)

## Computation conventions

- **GEX** ($ per 1 % move) = γ × OI × 100 × spot² × 0.01 — calls positive,
  puts negative (SpotGamma's "naive" convention: dealers long calls, short puts).
- **Gamma Flip**: net GEX profile recomputed over a ±8 % spot grid (IV and
  maturities frozen), zero crossing nearest to spot interpolated.
- **Delta flow** (proxy) = Δvolume between pulls × δ × 100 × spot. Taker
  direction is not observable in this feed: delta-weighted pressure, not
  true signed order flow.
- Expiries set at 16:00 ET; expired contracts excluded; 0DTE kept intraday
  with a 5-minute floor on t.

## Supporting the project

The dashboard is free, ad-free and collects no data — and it will stay that
way. If you use it and it saves you time, you can buy the development a
coffee:

[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-dwarfsquirrel-FFDD00?style=flat-square&logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/dwarfsquirrel)

Entirely optional. A donation grants no support, no priority on features and
no warranty — the terms of the [MIT licence](LICENSE) and the
[disclaimer](DISCLAIMER.md) are unchanged. Reporting a bug or suggesting an
improvement helps just as much.

## Known limits

- 15-min delayed data — structure-reading tool, not an execution tool.
- The CBOE endpoint is not contractual: format may change (ingestion is
  isolated so another source, e.g. Tradier, can be plugged in).
- Levels are in index points (SPX/NDX), not converted to ES/NQ futures.
