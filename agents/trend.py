"""Scanner source 2: Solana coins already trending with real liquidity.
GeckoTerminal trending_pools (free, ~30 req/min; we use 1 req per cycle) + DexScreener token-profiles/latest.
Boosts (paid promos) are ignored. Candidates go into the same waiting room as PumpPortal migrations
and pass the exact same Scanner -> Auditor -> Analyst -> Executor -> Risk pipeline."""
import time, requests, db, util, config
log = util.logger("scanner")
EVERY_S = 60; REJ = {}; MAX_AGE_H = 72
SKIP = {"So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qJxNZoBmBFNdjYddw4ZQwKYsjv",
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}
H = {"Accept": "application/json", "User-Agent": "desk/1.0"}

def _gt(path, ttl):
    import gt; out = []
    for d in gt.get(path, ttl).get("data") or []:
        b = (((d.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
        if b.startswith("solana_"): out.append(b[7:])
    return out
def gecko():  # trending pages 1-3
    return [m for pg in (1, 2, 3) for m in _gt(f"/networks/solana/trending_pools?page={pg}", 110)]
def gecko_new(): return _gt("/networks/solana/new_pools?page=1", 55) + _gt("/networks/solana/new_pools?page=2", 110)
def gecko_vol(): return _gt("/networks/solana/pools?page=1&sort=h24_volume_usd_desc", 170)
def takeovers():
    r = requests.get("https://api.dexscreener.com/community-takeovers/latest/v1", headers=H, timeout=15); r.raise_for_status()
    return [x["tokenAddress"] for x in r.json() or [] if x.get("chainId") == "solana" and x.get("tokenAddress")]
def dexsearch():
    out = []
    for q in ("pumpswap", "pump"):
        util.DEX_RL.wait()
        r = requests.get("https://api.dexscreener.com/latest/dex/search", params={"q": q}, headers=H, timeout=15)
        if r.status_code == 429: raise util.Http429()
        r.raise_for_status()
        out += [util.get(p, "baseToken.address") for p in (r.json().get("pairs") or []) if p.get("chainId") == "solana"]
    return [m for m in out if m]

def profiles():
    r = requests.get("https://api.dexscreener.com/token-profiles/latest/v1", headers=H, timeout=15)
    r.raise_for_status()
    return [x["tokenAddress"] for x in r.json() or [] if x.get("chainId") == "solana" and x.get("tokenAddress")]

def add(mints, src):
    import stats; mints = list(dict.fromkeys(mints))
    now = time.time()
    for k in [k for k, v in REJ.items() if now - v > 6*3600]: REJ.pop(k)
    new = [m for m in mints if m not in SKIP and m not in REJ and not db.q("select 1 from coins where mint=?", (m,), True)]
    stats.ev("seen_trend", len(new)); stats.ev("seen_repeat", len(mints) - len(new))
    for m in new: REJ[m] = now  # each mint is screened once per 6h; accepted ones live in coins anyway
    if not new: return 0
    pairs = util.dex_tokens(new); n = 0
    for m in new:
        p = pairs.get(m)
        if not p: stats.ev("pre_kill"); stats.ev("pre_kill:no SOL pool"); continue  # not on a Solana DEX FOMO can route / no SOL/USDC pair
        created = (p.get("pairCreatedAt") or 0) / 1000
        if not created or time.time() - created > MAX_AGE_H*3600: stats.ev("pre_kill"); stats.ev("pre_kill:too old"); continue  # memecoins only, not majors
        db.q("insert or ignore into coins(mint,name,symbol,pool,migrated_at,source,found_at,status) values(?,?,?,?,?,?,?,'waiting')",
             (m, util.get(p, "baseToken.name"), util.get(p, "baseToken.symbol"), p.get("pairAddress"), created, src, time.time()))
        n += 1; stats.ev("pre_pass"); log.info(f"waiting {m} source={src} dex={p.get('dexId')}")
    return n

def loop(stop):
    while not stop.is_set():
        if not db.kv_get("paused:scanner"):
            for name, f in (("gecko", gecko), ("gecko_new", gecko_new), ("gecko_vol", gecko_vol), ("dexprofile", profiles), ("dexcto", takeovers), ("dexsearch", dexsearch)):
                try: add(f(), name)
                except util.Http429: log.info(f"{name} 429")
                except Exception as e: log.info(f"{name} err {type(e).__name__}: {e}")
        stop.wait(EVERY_S)
