import re
import time, json, logging, os, requests, datetime, threading
from config import BASE
os.makedirs(os.path.join(BASE, "logs"), exist_ok=True)
def now_iso(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z")
def now_iso_ago(sec): return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=sec)).isoformat(timespec="seconds").replace("+00:00","Z")
class RedactFmt(logging.Formatter):
    def format(self, r):
        out = super().format(r); u = os.environ.get("DESK_RPC_URL"); t = os.environ.get("TG_TOKEN"); g = os.environ.get("GH_DESK_TOKEN")
        k = re.search(r"api-key=([\w-]+)", u or ""); k = k.group(1) if k else None
        for sec, lab in ((u, "[rpc]"), (k, "[rpc-key]"), (t, "[tg]"), (g, "[gh]")):
            if sec: out = out.replace(sec, lab)
        return out
def logger(name):
    l = logging.getLogger(name)
    if not l.handlers:
        h = logging.FileHandler(os.path.join(BASE, "logs", f"{name}.log")); h.setFormatter(RedactFmt("%(asctime)s %(message)s"))
        l.addHandler(h); l.setLevel(logging.INFO)
    return l
def get(d, path):
    for p in path.split("."):
        if not isinstance(d, dict): return None
        d = d.get(p)
    return d
class RateLimit:
    def __init__(s, per_min): s.n = per_min; s.t = []; s.l = threading.Lock()
    def wait(s):
        with s.l:
            t = time.time(); s.t = [x for x in s.t if t - x < 60]
            if len(s.t) >= s.n: time.sleep(60 - (t - s.t[0]) + 0.05)
            s.t.append(time.time())
DEX_RL = RateLimit(250)
class Http429(Exception): pass
SOL_DEXES = {"pumpswap", "raydium", "meteora", "orca", "meteoradbc"}
QUOTES = {"So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qJxNZoBmBFNdjYddw4ZQwKYsjv"}
_dex_cache = {}; _dex_l = threading.Lock()

def best_pair(pairs, mint=None):
    """Highest-liquidity SOL/USDC pair. Prefer pumpswap/pumpfun only if it holds at least half of the deepest pool."""
    usable = []
    for p in pairs or []:
        if (p.get("chainId") or "solana") != "solana": continue
        if get(p, "quoteToken.address") not in QUOTES: continue
        usable.append(p)
    if not usable: return None
    hi = max(usable, key=lambda p: get(p, "liquidity.usd") or 0)
    hi_liq = get(hi, "liquidity.usd") or 0
    canon = [p for p in usable if (p.get("dexId") or "").lower() in ("pumpswap", "pumpfun")]
    if canon:
        best_c = max(canon, key=lambda p: get(p, "liquidity.usd") or 0)
        c_liq = get(best_c, "liquidity.usd") or 0
        if c_liq >= 0.5 * hi_liq or hi_liq < 1000:
            return best_c
    return hi

def pair_mc(p, mint=None):
    """(usd, source). Use Dex marketCap when it agrees with price; for pump mints, price x 1B supply if Dex MC lags."""
    if not p: return 0.0, "none"
    def fnum(x):
        try:
            if x is None or x == "": return None
            return float(x)
        except Exception:
            return None
    price = fnum(p.get("priceUsd"))
    mc = fnum(p.get("marketCap"))
    fdv = fnum(p.get("fdv"))
    computed = price * 1_000_000_000 if price and (mint or "").endswith("pump") else None
    if computed:
        ref = mc or fdv
        if ref and abs(computed - ref) / max(computed, ref) <= 0.25:
            return ref, "marketCap" if mc else "fdv"
        return computed, "price x 1B supply"
    if mc: return mc, "marketCap"
    if fdv: return fdv, "fdv"
    return 0.0, "unknown"

def dex_tokens(mints, ttl=15, curve=False):
    """Returns {mint: best pumpswap pair}. Batches of 30. Per-mint cache `ttl` seconds (0 = no cache)."""
    out = {}; now = time.time(); need = []
    with _dex_l:
        for m in mints:
            h = _dex_cache.get(m)
            if ttl and h and now - h[0] < ttl and not curve:
                if h[1]: out[m] = h[1]
            else: need.append(m)
    for i in range(0, len(need), 30):
        DEX_RL.wait(); _count("dex_call")
        r = requests.get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(need[i:i+30]), timeout=15)
        if r.status_code == 429: _count("dex_429"); raise Http429()
        r.raise_for_status()
        got = {}
        by = {}
        for p in r.json() or []:
            if p.get("chainId", "solana") != "solana" or (p.get("dexId") not in SOL_DEXES and not (curve and p.get("dexId") == "pumpfun")): continue
            if get(p, "quoteToken.address") not in QUOTES: continue
            m = get(p, "baseToken.address")
            if not m: continue
            by.setdefault(m, []).append(p)
        for m, ps in by.items():
            pick = best_pair(ps, m)
            if pick:
                out[m] = pick; got[m] = pick
        if curve: continue  # don't pollute the scanner cache with bonding-curve pairs
        with _dex_l:
            for m in need[i:i+30]: _dex_cache[m] = (time.time(), got.get(m))
            if len(_dex_cache) > 5000: _dex_cache.clear()
    return out
def _count(k):
    try: import stats; stats.ev("rl:" + k)
    except Exception: pass
class Spacer:
    """Shared min spacing between calls (thread-safe); spacing set per call from settings."""
    def __init__(s): s.l = threading.Lock(); s.last = 0.0; s.pause_until = 0.0
    def wait(s, gap):
        with s.l:
            t = time.time(); w = max(s.last + gap, s.pause_until) - t
            if w > 0: time.sleep(w)
            s.last = time.time()
    def backoff(s, sec): s.pause_until = max(s.pause_until, time.time() + sec)
RPC_SP = Spacer(); RC_SP = Spacer(); _rc_cache = {}; _rc_l = threading.Lock()
def _cfg(k, d):
    try: import config; return config.load()[k]
    except Exception: return d
_rps = [8, 0.0]
def rpc(url, method, params, timeout=30):
    if time.time() - _rps[1] > 30: _rps[0] = _cfg("rpc_rps", 8); _rps[1] = time.time()
    for attempt in range(4):
        RPC_SP.wait(1.0 / _rps[0]); _count("rpc_call")
        try: r = requests.post(url, json={"jsonrpc":"2.0","id":1,"method":method,"params":params}, timeout=timeout)
        except Exception as e: raise RuntimeError(f"[rpc] {method} {type(e).__name__}") from None
        if r.status_code != 429: break
        _count("rpc_429"); RPC_SP.backoff(2 ** attempt)
    if r.status_code != 200: raise RuntimeError(f"[rpc] {method} HTTP {r.status_code}")
    j = r.json()
    if "error" in j: raise RuntimeError(f"rpc {method}: {j['error']}")
    return j["result"]
def rugcheck(mint, what="report", ttl=300):
    k = (mint, what)
    with _rc_l:
        h = _rc_cache.get(k)
        if ttl and h and time.time() - h[0] < ttl: _count("rugcheck_cached"); return h[1]
    for attempt in range(3):
        RC_SP.wait(60.0 / _cfg("rc_per_min", 50)); _count("rugcheck_call")
        r = requests.get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/{what}", timeout=20)
        if r.status_code != 429: break
        _count("rugcheck_429"); RC_SP.backoff(5 * (attempt + 1))
    r.raise_for_status(); j = r.json()
    with _rc_l:
        _rc_cache[k] = (time.time(), j)
        if len(_rc_cache) > 2000: _rc_cache.clear()
    return j
def retry(fn, n=3, wait=2):
    for i in range(n):
        try: return fn()
        except Exception as e:
            err = e
            if i < n-1: time.sleep(wait)
    raise err

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
_rpc_res_l = threading.Lock(); _rpc_res_cache = {}; _rpc_res_win = [0.0, 0]
def research_budget_ok():
    cap = int(_cfg("research_rpc_per_min", 30))
    with _rpc_res_l:
        t = time.time()
        if t - _rpc_res_win[0] >= 60: _rpc_res_win[0] = t; _rpc_res_win[1] = 0
        if _rpc_res_win[1] >= cap: return False
        _rpc_res_win[1] += 1; return True
def research_rpc(method, params, ttl=0, timeout=30):
    """Cached, budgeted JSON-RPC for read-only research. Falls back to public mainnet on 429. Never logs the URL."""
    key = (method, json.dumps(params, sort_keys=True, default=str))
    with _rpc_res_l:
        h = _rpc_res_cache.get(key)
        if ttl and h and time.time() - h[0] < ttl:
            _count("rpc_research_cached"); return h[1]
    if not research_budget_ok():
        _count("rpc_research_budget"); raise RuntimeError("research rpc budget exhausted")
    urls = []
    u = os.environ.get("DESK_RPC_URL")
    if u: urls.append(u)
    pub = PUBLIC_RPC
    if not u or u.rstrip("/") != pub: urls.append(pub)
    err = None
    for i, url in enumerate(urls):
        try:
            res = rpc(url, method, params, timeout=timeout)
            with _rpc_res_l:
                _rpc_res_cache[key] = (time.time(), res)
                if len(_rpc_res_cache) > 4000:
                    items = sorted(_rpc_res_cache.items(), key=lambda kv: kv[1][0])
                    for k, _ in items[:len(items)//2]: _rpc_res_cache.pop(k, None)
            return res
        except RuntimeError as e:
            err = e
            if "429" in str(e):
                _count("rpc_research_429"); RPC_SP.backoff(4 + 2 * i); continue
            raise
    raise err
