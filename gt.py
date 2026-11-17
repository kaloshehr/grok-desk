"""GeckoTerminal free API (public, no key). ~30 req/min -> we space calls >=2.5s across all threads."""
import time, threading, requests, util
log = util.logger("gt")
_l = threading.Lock(); _last = [0.0]; _cache = {}
H = {"Accept": "application/json", "User-Agent": "desk/1.0"}
def get(path, ttl=60):
    hit = _cache.get(path)
    if hit and time.time() - hit[0] < ttl: return hit[1]
    with _l:
        w = 2.5 - (time.time() - _last[0])
        if w > 0: time.sleep(w)
        _last[0] = time.time()
        r = requests.get("https://api.geckoterminal.com/api/v2" + path, headers=H, timeout=15)
    if r.status_code == 429: raise util.Http429()
    r.raise_for_status(); j = r.json(); _cache[path] = (time.time(), j)
    if len(_cache) > 300: _cache.clear()
    return j
def trades(pool):
    """Last ~300 trades of a Solana pool: [{wallet, kind, usd, ts, tx}] newest first."""
    out = []
    for d in get(f"/networks/solana/pools/{pool}/trades").get("data") or []:
        a = d.get("attributes") or {}
        try: ts = time.mktime(time.strptime(a["block_timestamp"], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
        except Exception: ts = 0
        out.append({"wallet": a.get("tx_from_address"), "kind": a.get("kind"), "usd": float(a.get("volume_in_usd") or 0), "ts": ts, "tx": a.get("tx_hash")})
    return out
