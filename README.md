# grok-desk

PAPER-only Solana memecoin desk (Python + SQLite) plus a GitHub Pages iPhone app on the `gh-pages` branch.

Agents watch PumpPortal, DexScreener and GeckoTerminal, then route coins through Scanner → Auditor → Analyst → Executor → Risk. Nothing in this repo places real orders or stores secrets. Runtime secrets live only in `~/.desk_secrets/` on the machine that runs `./start.sh`.

Extra read-only agents:

- `winners` — tracks pump.fun coins that already exploded and clusters wallets that share a SOL funder
- `gems` — scores currently cheap coins with early-breakout signals

Telegram: `/winners` and `/gems`. Mode stays PAPER unless you deliberately arm LIVE on the running desk.
