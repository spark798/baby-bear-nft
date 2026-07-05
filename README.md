# 🐻 Baby Bear NFT — Sentiment Edition

A **living NFT** collection of 10,000 unique pixel-art baby bears on **Polygon**.
Every bear's background color shifts in real time with the **20-minute Bitcoin price change** — so the whole collection "breathes" with the market, 24/7.

| BTC 20-min change | Mood | Background |
|-------------------|------|------------|
| ▲ ≥ +2%  | HOT BULL     | 🌸 Pink |
| ▲ +1–2%  | BULLISH      | 🟡 Yellow |
| ▲ 0–1%   | SLIGHT UP    | 💜 Purple |
| ▼ 0–1%   | SLIGHT DOWN  | 💙 Blue |
| ▼ ≥ 1%   | BEARISH      | 🖤 Black |

Each bear is algorithmically generated from 8 trait layers (background, fur, eyes, mouth, cheeks, hat, eyewear, neck) with varying rarity.

## How it works

`bear_sentiment_server.py` is a **zero-dependency** (stdlib-only) HTTP server that:

- Loads all 10,000 bears' traits from `all_metadata.json`.
- Renders each bear's PNG on the fly (hand-rolled PNG encoder — no Pillow).
- Fetches the 20-minute BTC change from CryptoCompare → Kraken → CoinGecko (3-source fallback), cached for 5 minutes.
- Serves a live dashboard, a wallet-connect mint page, and per-token image + metadata endpoints whose background reflects current sentiment.

### Endpoints

| Path | Description |
|------|-------------|
| `/` | Live dashboard (sentiment card, price, collection preview, browser) |
| `/mint` | MetaMask mint page (Polygon) |
| `/bears/<id>/image.png` | Rendered PNG for token `<id>` (1–10000) |
| `/bears/<id>/metadata.json` | Live metadata (sentiment attributes injected) |
| `/api/sentiment` | Current BTC sentiment as JSON |
| `/simulate/<pct>` | Preview which mood a given % would map to |
| `/banner.png` | 1400×350 collection banner |
| `/collection.json` | OpenSea-style collection metadata |

## Run locally

```bash
pip install -r requirements.txt   # only 'certifi'
python3 bear_sentiment_server.py
# → http://localhost:8889
```

## Deploy

Any Python host works (Render, Railway, Fly.io). A `Procfile` is included:

```
web: python3 bear_sentiment_server.py
```

Set these environment variables in production:

| Var | Purpose | Example |
|-----|---------|---------|
| `PORT` | Port to bind (host usually sets this) | `8889` |
| `BASE_URL` | Public https base URL — used in served metadata `image`/`external_url` | `https://baby-bear.onrender.com` |

> **Note:** the on-chain token metadata is served **dynamically** by this server (`/bears/<id>/metadata.json`), which is what makes the NFT "live." The `image`/`external_url` fields inside `all_metadata.json` are **not** used at runtime — that file is only a trait source.

## Contract

- **Network:** Polygon (chainId 137)
- **Contract:** `0xAF6a5e744Ff06d50c2F236b90344F84A640381A9`
- **Mint price:** 0.003 POL per bear
- **Supply:** 10,000
