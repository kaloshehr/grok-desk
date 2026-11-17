"""Wallet-cluster tracker (PAPER, read-only). Never places orders or touches funds.

Every ~20 minutes take the top 10 exploded pump.fun-origin coins (by multiplier / ATH MC
in ~24h) and fully map linked wallets: dev, snipers/early buyers, funders, and early
holders who profited. RPC budget is spent mainly on those 10 (deeper, cached walks).
New PumpPortal tokens and new pairs are watched; if a mapped address appears as creator,
early buyer, or funder, a Telegram alert names the address and the exploded coin it came from.
"""
import json, time, threading, collections, requests, asyncio, db, util, config, notifier, links
log = util.logger("winners")

PUMP_API = ("https://frontend-api-v3.pump.fun/coins/", "https://frontend-api.pump.fun/coins/")
SYSTEM = {
    "11111111111111111111111111111111",
    "So11111111111111111111111111111111111111112",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # pump.fun program
    "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf",  # pump.fun global
    "ComputeBudget111111111111111111111111111111",
    "SysvarRent111111111111111111111111111111111",
    "11111111111111111111111111111112",
}
H = {"Accept": "application/json", "User-Agent": "desk/1.0"}
_pending = []  # delayed early-buyer inspections
_pl = threading.Lock()
_alerted = {}

def is_pump(mint, p=None, name=""):
    if (mint or "").endswith("pump"): return True
    dex = (p or {}).get("dexId") or ""
    if dex.lower() in ("pumpswap", "pumpfun", "pump-fun", "pump"): return True
    if "pump" in (name or "").lower(): return True
    return False

def is_winner(mc, change_pct, age_h, s, launch_mc=None):
    if age_h is None or age_h > s["win_max_age_h"] or age_h < 0: return False, 0.0
    launch = launch_mc or s.get("win_launch_mc_usd", 5500) or 5500
    mult = (mc / launch) if mc and launch else 0
    if change_pct is not None: mult = max(mult, 1 + (change_pct or 0) / 100)
    ok = (mult >= s["win_min_mult"]) or (mc and mc >= s["win_min_mc_usd"])
    return ok, round(mult, 2)

def cluster_score(wins, rugs, launches, snipes, avg_hold_s=None):
    n = max(wins + rugs, 1)
    wr = wins / n
    rug_rate = rugs / n
    sc = 20 * min(wins, 5) + 30 * wr + 8 * min(max(snipes, launches), 5)
    if avg_hold_s and avg_hold_s >= 300: sc += 10
    if avg_hold_s is not None and avg_hold_s < 30 and (wins + rugs) >= 2: sc -= 20
    sc -= 40 * rug_rate
    if wins < 2: sc -= 15  # not yet repeatable
    return max(0, min(100, round(sc, 1)))

def _gt_pools():
    import gt
    out = []
    pages = [("/networks/solana/trending_pools?page=1", 90),
             ("/networks/solana/trending_pools?page=2", 130),
             ("/networks/solana/trending_pools?page=3", 180),
             ("/networks/solana/pools?page=1&sort=h24_volume_usd_desc", 170),
             ("/networks/solana/new_pools?page=1", 80)]
    for path, ttl in pages:
        try: data = gt.get(path, ttl).get("data") or []
        except util.Http429: log.info("gt 429"); break
        except Exception as e: log.info(f"gt {type(e).__name__}"); continue
        for d in data:
            a = d.get("attributes") or {}
            b = (((d.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id") or ""
            mint = b[7:] if b.startswith("solana_") else None
            if not mint: continue
            created = a.get("pool_created_at") or ""
            try: launched = time.mktime(time.strptime(created[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
            except Exception: launched = 0
            ch = a.get("price_change_percentage") or {}
            change = None
            for k in ("h24", "h6", "h1"):
                if ch.get(k) is not None:
                    try: change = float(ch[k]); break
                    except Exception: pass
            mc = 0.0
            for k in ("fdv_usd", "market_cap_usd"):
                try:
                    if a.get(k): mc = float(a[k]); break
                except Exception: pass
            out.append({"mint": mint, "name": a.get("name") or "", "pool": a.get("address"),
                        "mc": mc, "change": change, "launched": launched, "dex": None, "src": "gt"})
    return out

def _dex_pairs():
    out = []
    for q in ("pumpswap", "pump"):
        try:
            util.DEX_RL.wait()
            r = requests.get("https://api.dexscreener.com/latest/dex/search", params={"q": q}, headers=H, timeout=15)
            if r.status_code == 429: raise util.Http429()
            r.raise_for_status()
            for p in r.json().get("pairs") or []:
                if p.get("chainId") != "solana": continue
                mint = util.get(p, "baseToken.address")
                if not mint: continue
                created = (p.get("pairCreatedAt") or 0) / 1000
                mc = float(p.get("marketCap") or p.get("fdv") or 0)
                ch = util.get(p, "priceChange.h24")
                if ch is None: ch = util.get(p, "priceChange.h6")
                out.append({"mint": mint, "name": util.get(p, "baseToken.name") or "", "symbol": util.get(p, "baseToken.symbol"),
                            "pool": p.get("pairAddress"), "mc": mc, "change": ch, "launched": created, "dex": p.get("dexId"),
                            "pair": p, "src": "dex"})
        except util.Http429: log.info("dex 429"); break
        except Exception as e: log.info(f"dex {type(e).__name__}")
    return out

def discover(s):
    cands, seen = [], set()
    for row in _gt_pools() + _dex_pairs():
        mint = row["mint"]
        if mint in seen: continue
        seen.add(mint)
        if not is_pump(mint, row.get("pair"), row.get("name")): continue
        launched = row.get("launched") or 0
        age_h = (time.time() - launched) / 3600 if launched else None
        ok, mult = is_winner(row.get("mc") or 0, row.get("change"), age_h, s)
        if not ok: continue
        row["mult"] = mult; row["age_h"] = age_h; cands.append(row)
    return cands

def pump_info(mint):
    k = f"pumpapi:{mint}"; hit = db.kv_get(k)
    if hit and time.time() - hit.get("t", 0) < 3600:
        return {} if hit.get("fail") else hit
    for base in PUMP_API:
        try:
            r = requests.get(base + mint, headers=H, timeout=10)
            if r.status_code == 429: time.sleep(2); continue
            if r.status_code != 200:
                db.kv_set(k, {"t": time.time(), "fail": r.status_code}); continue
            j = r.json(); j["t"] = time.time(); db.kv_set(k, j); return j
        except Exception:
            continue
    return {}

def _keys(tx):
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    acc = list(msg.get("accountKeys") or [])
    out = []
    for a in acc:
        if isinstance(a, dict): out.append(a.get("pubkey") or "")
        else: out.append(a)
    meta = (tx or {}).get("meta") or {}
    for a in meta.get("loadedAddresses", {}).get("writable") or []: out.append(a)
    for a in meta.get("loadedAddresses", {}).get("readonly") or []: out.append(a)
    return out

def _first_buyers(addr, launched, s, deep=False, cursor=None):
    """Earliest unique buyers on a pool or bonding-curve account.
    Shallow: skip hot pools. Deep (top-10): page back toward launch, resumable via cursor.
    Returns (buyers, state) where state has before/done/reached."""
    empty = ([], {"before": cursor, "done": True, "reached": False})
    if not addr: return empty
    window = s["win_early_window_s"]; need = s["win_early_n"]
    max_pages = int(s.get("win_deep_pages", 15) if deep else 2)
    sigs, before, pages = [], cursor, 0
    oldest_needed = (launched or 0) - 30
    reached = False
    try:
        db.beat("winners")
        args = {"limit": 200}
        if before: args["before"] = before
        batch = util.research_rpc("getSignaturesForAddress", [addr, args], ttl=3600 if before else 60) or []
        if not batch:
            return [], {"before": before, "done": True, "reached": False}
        last_t = batch[-1].get("blockTime") or 0
        if not deep and launched and last_t and last_t > launched + max(window, 120) and not before:
            log.info(f"skip early-buyer walk {addr[:6]} (oldest sig in last 200 is {int(last_t-launched)}s after launch)")
            return [], {"before": None, "done": True, "reached": False}
        sigs.extend(batch)
        while pages < max_pages and last_t and last_t > oldest_needed and len(batch) == 200:
            before = batch[-1]["signature"]; pages += 1
            db.beat("winners")
            batch = util.research_rpc("getSignaturesForAddress", [addr, {"limit": 200, "before": before}], ttl=6*3600) or []
            if not batch: break
            sigs.extend(batch)
            last_t = batch[-1].get("blockTime") or 0
        if last_t and launched and last_t <= oldest_needed + window:
            reached = True
        if len(batch) < 200:
            reached = True
        done = reached or (not deep)
        before = batch[-1]["signature"] if batch and not reached else None
    except Exception as e:
        log.info(f"sigs {type(e).__name__}"); return [], {"before": cursor, "done": False, "reached": False}
    early = []
    for x in reversed(sigs):  # oldest first
        bt = x.get("blockTime") or 0
        if x.get("err"): continue
        if launched and bt and bt < launched - 15: continue
        if launched and bt and bt > launched + window and len(early) >= (need if deep else 3):
            if reached or not deep: break
            # deep walk not yet at launch: don't treat mid-history as snipers
            early = []; break
        if launched and bt and bt > launched + window and not reached and deep:
            continue
        early.append(x)
        if len(early) >= need * (3 if deep else 2): break
    buyers, seen = [], set()
    for x in early:
        try:
            db.beat("winners")
            tx = util.research_rpc("getTransaction", [x["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}], ttl=7 * 86400)
        except Exception:
            continue
        if not tx: continue
        bt = x.get("blockTime") or (tx.get("blockTime") or 0)
        meta = tx.get("meta") or {}
        for b in meta.get("postTokenBalances") or []:
            owner = b.get("owner")
            if not owner or owner in SYSTEM or owner in seen: continue
            pre = {pb.get("owner"): float((pb.get("uiTokenAmount") or {}).get("uiAmount") or 0)
                   for pb in meta.get("preTokenBalances") or [] if pb.get("mint") == b.get("mint")}
            post = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            if post > pre.get(owner, 0):
                seen.add(owner); buyers.append({"addr": owner, "ts": bt, "mint": b.get("mint")})
                if len(buyers) >= need: 
                    return buyers, {"before": before, "done": done, "reached": reached}
    return buyers, {"before": before, "done": done, "reached": reached}

def _funder(addr):
    cached = db.q("select json from winner_wallets where addr=?", (addr,), True)
    if cached and cached["json"]:
        j = json.loads(cached["json"])
        if "funder" in j: return j["funder"]
    try:
        sigs = util.research_rpc("getSignaturesForAddress", [addr, {"limit": 80}], ttl=3600) or []
    except Exception:
        return None
    if not sigs: return None
    # walk to the oldest page once if it looks like we didn't reach the start
    oldest = sigs[-1]
    if len(sigs) >= 80:
        try:
            more = util.research_rpc("getSignaturesForAddress", [addr, {"limit": 80, "before": oldest["signature"]}], ttl=3600) or []
            if more: oldest = more[-1]
        except Exception:
            pass
    try:
        tx = util.research_rpc("getTransaction", [oldest["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}], ttl=7 * 86400)
    except Exception:
        return None
    if not tx: return None
    keys = _keys(tx)
    meta = tx.get("meta") or {}
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    if addr not in keys: return None
    i = keys.index(addr)
    if i >= len(pre) or i >= len(post): return None
    if post[i] <= pre[i]:  # not an incoming first tx; search this page for first incoming
        return None
    # the sender is the account whose SOL dropped the most
    best, drop = None, 0
    for j, k in enumerate(keys):
        if j >= len(pre) or j >= len(post) or k == addr or k in SYSTEM: continue
        d = pre[j] - post[j]
        if d > drop: drop, best = d, k
    return best

def _save_coin(row, s, creator=None, rug=0):
    mint = row["mint"]
    ex = db.q("select ath_mc,max_mult from winner_coins where mint=?", (mint,), True)
    ath = max(row.get("mc") or 0, (ex["ath_mc"] if ex else 0) or 0)
    mult = max(row.get("mult") or 0, (ex["max_mult"] if ex else 0) or 0)
    db.q("insert into winner_coins(mint,name,symbol,pool,creator,found_at,launched_at,ath_mc,max_mult,first_mc,status,rug,analyzed,json) values(?,?,?,?,?,?,?,?,?,?,?,?,0,?) on conflict(mint) do update set name=excluded.name,symbol=excluded.symbol,pool=excluded.pool,creator=coalesce(excluded.creator,winner_coins.creator),ath_mc=max(winner_coins.ath_mc,excluded.ath_mc),max_mult=max(winner_coins.max_mult,excluded.max_mult),rug=max(winner_coins.rug,excluded.rug),json=excluded.json",
         (mint, row.get("name"), row.get("symbol"), row.get("pool"), creator, time.time(), row.get("launched"), ath, mult, row.get("mc"), "winner", rug, json.dumps({"src": row.get("src"), "change": row.get("change")})))

def _is_rug(pair, ath_mc):
    if not pair: return False
    liq = util.get(pair, "liquidity.usd") or 0
    mc = float(pair.get("marketCap") or pair.get("fdv") or 0)
    if ath_mc and mc and mc < 0.1 * ath_mc and liq < 1000: return True
    if liq <= 0 and (pair.get("dexId") != "pumpfun"): return True
    return False

def mark_rug(mint, creator=None, name=None, why=""):
    """Record a rug outcome on the coin and penalize the creator/cluster. Safe to call twice."""
    row = db.q("select rug,creator from winner_coins where mint=?", (mint,), True)
    if row:
        db.q("update winner_coins set rug=1,status='rug' where mint=?", (mint,))
        creator = creator or row["creator"]
    else:
        db.q("insert or ignore into winner_coins(mint,name,creator,found_at,status,rug,analyzed,json) values(?,?,?,?, 'rug',1,0,?)",
             (mint, name, creator, time.time(), json.dumps({"why": why})))
        if creator:
            db.q("update winner_coins set creator=coalesce(creator,?) where mint=?", (creator, mint))
    if creator:
        db.q("insert or ignore into winner_wallets(addr,role,first_seen,json) values(?,?,?,?)",
             (creator, "dev", time.time(), json.dumps({})))
        db.q("insert or ignore into winner_roles(mint,addr,role,buy_ts,sell_ts,hold_s,usd) values(?,?,?,?,null,null,null)",
             (mint, creator, "dev", time.time()))
        try: rebuild_clusters()
        except Exception as e: log.info(f"mark_rug cluster {type(e).__name__}")
    log.info(f"RUG {name or mint[:6]} {mint} {why}")

def analyze(row, s):
    mint = row["mint"]
    t0 = time.time()
    info = pump_info(mint) or {}
    creator = info.get("creator") or info.get("creatorId") or row.get("creator")
    curve = info.get("bonding_curve") or info.get("bondingCurve")
    if not creator:
        prev = db.q("select creator from winner_coins where mint=?", (mint,), True) or db.q("select creator from coins where mint=?", (mint,), True)
        creator = prev["creator"] if prev else None
    # rugcheck is shared and can block the desk; only try if we still have no creator and time left
    if not creator and time.time() - t0 < 8:
        try:
            creator = (util.rugcheck(mint) or {}).get("creator")
        except Exception:
            pass
    buyers, rug = [], 0
    try:
        if not row.get("launched") and info.get("created_timestamp"):
            try: row["launched"] = int(info["created_timestamp"]) / (1000 if info["created_timestamp"] > 1e12 else 1)
            except Exception: pass
        pair = row.get("pair")
        if not pair:
            try: pair = util.dex_tokens([mint], ttl=30).get(mint)
            except Exception: pair = None
        if pair and not row.get("pool"): row["pool"] = pair.get("pairAddress")
        rug = int(_is_rug(pair, row.get("mc") or 0))
        _save_coin(row, s, creator, rug)
        target = curve or row.get("pool")
        deep = bool(row.get("deep") or s.get("win_deep_pages"))
        cursor = row.get("walk_before")
        budget = 180 if deep else 25
        walk = {"before": cursor, "done": False, "reached": False}
        if target and time.time() - t0 < budget:
            buyers, walk = _first_buyers(target, row.get("launched") or 0, s, deep=deep, cursor=cursor)
        elif target:
            log.info(f"{mint[:6]} skip early buyers, {time.time()-t0:.0f}s already spent")
        try:
            db.q("update winner_coins set walk_before=?,walk_done=?,mapped_at=? where mint=?",
                 (walk.get("before"), int(bool(walk.get("done"))), time.time(), mint))
        except Exception:
            pass
        addrs = []
        if creator: addrs.append(("dev", creator, row.get("launched") or 0))
        for b in buyers:
            role = "sniper" if (row.get("launched") and b["ts"] and b["ts"] <= (row["launched"] + s["win_early_window_s"])) else "early"
            addrs.append((role, b["addr"], b["ts"]))
        for role, addr, ts in addrs:
            if addr in SYSTEM: continue
            f = None
            if role != "dev" and time.time() - t0 < 40:
                try: f = _funder(addr)
                except Exception as e: log.info(f"funder {type(e).__name__}")
            prev = db.q("select json from winner_wallets where addr=?", (addr,), True)
            j = json.loads(prev["json"]) if prev and prev["json"] else {}
            if f: j["funder"] = f
            if not prev:
                db.q("insert into winner_wallets(addr,role,first_seen,json) values(?,?,?,?)", (addr, role, time.time(), json.dumps(j)))
            else:
                db.q("update winner_wallets set json=? where addr=?", (json.dumps(j), addr))
            db.q("insert or replace into winner_roles(mint,addr,role,buy_ts,sell_ts,hold_s,usd) values(?,?,?,?,null,null,null)",
                 (mint, addr, role, ts))
            if f and f not in SYSTEM:
                db.q("insert or ignore into winner_roles(mint,addr,role,buy_ts,sell_ts,hold_s,usd) values(?,?,?,?,null,null,null)",
                     (mint, f, "funder", ts))
        _maybe_holds(row.get("pool"), mint)
        _map_profits(row.get("pool"), mint)
        rebuild_clusters()
    finally:
        db.q("update winner_coins set analyzed=1,creator=coalesce(?,creator),rug=? where mint=?", (creator, rug, mint))
        log.info(f"analyzed {mint} buyers={len(buyers)} creator={(creator or '')[:6]} rug={rug} in {time.time()-t0:.0f}s")
    return creator, buyers

def _maybe_holds(pool, mint):
    if not pool: return
    try:
        import gt
        trades = gt.trades(pool)
    except Exception:
        return
    buys, sells = collections.defaultdict(list), collections.defaultdict(list)
    for t in trades:
        w = t.get("wallet")
        if not w: continue
        (buys if t.get("kind") == "buy" else sells)[w].append(t.get("ts") or 0)
    for r in db.q("select addr,buy_ts from winner_roles where mint=?", (mint,)):
        if r["addr"] not in sells: continue
        st = min(t for t in sells[r["addr"]] if t)
        if r["buy_ts"] and st and st >= r["buy_ts"]:
            db.q("update winner_roles set sell_ts=?,hold_s=? where mint=? and addr=?", (st, st - r["buy_ts"], mint, r["addr"]))

def rebuild_clusters():
    rows = db.q("select addr, json from winner_wallets")
    fund_of = {}
    for r in rows:
        j = json.loads(r["json"] or "{}"); f = j.get("funder")
        if f and f not in SYSTEM: fund_of[r["addr"]] = f
    # funder -> wallets; skip exchange-like funders (too many children)
    groups = collections.defaultdict(set)
    for a, f in fund_of.items(): groups[f].add(a)
    cex = {f for f, ws in groups.items() if len(ws) >= 15}
    # each funded group is a cluster; devs with no funder form a 1-wallet cluster by their own addr
    # merge already-known cluster ids when possible: match by funder
    existing = {r["funder"]: r["id"] for r in db.q("select id,funder from winner_clusters") if r["funder"]}
    touched = set()
    for f, ws in groups.items():
        if f in cex or len(ws) < 2: continue
        cid = existing.get(f)
        if not cid:
            db.q("insert into winner_clusters(funder,updated) values(?,?)", (f, time.time()))
            cid = db.q("select id from winner_clusters where funder=?", (f,), True)["id"]
        touched.add(cid)
        for a in ws:
            db.q("update winner_wallets set cluster_id=? where addr=?", (cid, a))
        _score_cluster(cid, f, ws, cex=False)
    # solo devs: still track as a cluster of 1 if they launched 2+ coins
    devs = db.q("select addr from winner_wallets where role='dev'")
    for d in devs:
        addr = d["addr"]
        if db.q("select cluster_id from winner_wallets where addr=?", (addr,), True)["cluster_id"]: continue
        n = db.q("select count(distinct mint) n from winner_roles where addr=? and role='dev'", (addr,), True)["n"]
        if n < 2: continue
        db.q("insert into winner_clusters(funder,updated,label) values(?,?,?)", (addr, time.time(), "dev"))
        cid = db.q("select id from winner_clusters where funder=?", (addr,), True)["id"]
        db.q("update winner_wallets set cluster_id=? where addr=?", (cid, addr))
        _score_cluster(cid, addr, {addr}, cex=False)
    # mark cex-like
    for f in cex:
        cid = existing.get(f)
        if not cid: continue
        db.q("update winner_clusters set label='cex',score=0,wallets_n=?,updated=? where id=?", (len(groups[f]), time.time(), cid))

def _score_cluster(cid, funder, wallets, cex=False):
    addrs = list(wallets)
    qmarks = ",".join("?" * len(addrs))
    roles = db.q(f"select * from winner_roles where addr in ({qmarks})", addrs) if addrs else []
    mints = {r["mint"] for r in roles}
    wins = rugs = launches = snipes = 0
    holds = []
    for mint in mints:
        c = db.q("select * from winner_coins where mint=?", (mint,), True)
        if not c: continue
        is_win = (c["max_mult"] or 0) >= 10 or (c["ath_mc"] or 0) >= 500000
        if c["rug"]: rugs += 1
        elif is_win: wins += 1
        if any(r["role"] == "dev" and r["mint"] == mint for r in roles): launches += 1
        if any(r["role"] in ("sniper", "early") and r["mint"] == mint for r in roles): snipes += 1
        holds += [r["hold_s"] for r in roles if r["mint"] == mint and r["hold_s"] is not None]
    avg_hold = (sum(holds) / len(holds)) if holds else None
    sc = 0 if cex else cluster_score(wins, rugs, launches, snipes, avg_hold)
    label = "cex" if cex else ("dumper" if (rugs and rugs >= wins and sc < 40) else ("good" if sc >= 70 else "watch"))
    db.q("update winner_clusters set wallets_n=?,wins=?,launches=?,snipes=?,rugs=?,avg_dump_s=?,score=?,label=?,updated=? where id=?",
         (len(addrs), wins, launches, snipes, rugs, avg_hold, sc, label, time.time(), cid))
    # per-wallet rollup
    for a in addrs:
        my = [r for r in roles if r["addr"] == a]
        w_mints = {r["mint"] for r in my}
        w_wins = w_rugs = w_launch = w_snipe = 0
        hs = [r["hold_s"] for r in my if r["hold_s"] is not None]
        for mint in w_mints:
            c = db.q("select * from winner_coins where mint=?", (mint,), True)
            if not c: continue
            is_win = (c["max_mult"] or 0) >= 10 or (c["ath_mc"] or 0) >= 500000
            if c["rug"]: w_rugs += 1
            elif is_win: w_wins += 1
            if any(r["role"] == "dev" and r["mint"] == mint for r in my): w_launch += 1
            if any(r["role"] in ("sniper", "early") and r["mint"] == mint for r in my): w_snipe += 1
        wsc = cluster_score(w_wins, w_rugs, w_launch, w_snipe, (sum(hs)/len(hs)) if hs else None)
        dump = (sum(hs)/len(hs)) if hs else None
        db.q("update winner_wallets set wins=?,launches=?,snipes=?,rugs=?,avg_hold_s=?,dump_speed=?,score=?,cluster_id=? where addr=?",
             (w_wins, w_launch, w_snipe, w_rugs, dump, dump, wsc, cid, a))

def known_dev(addr):
    if not addr: return None
    r = db.q("select w.*, c.score as cscore, c.wins as cwins, c.label, c.id as cid from winner_wallets w left join winner_clusters c on c.id=w.cluster_id where w.addr=?", (addr,), True)
    if not r: return None
    sc = r["cscore"] if r["cscore"] is not None else r["score"]
    if sc is None: return None
    return dict(r) | {"use_score": sc}

def known_good_cluster(addrs, s):
    hits = []
    for a in addrs:
        r = known_dev(a)
        if not r: continue
        sc = r.get("use_score") or 0
        if sc >= s["win_min_cluster_score"] and (r.get("cwins") or r.get("wins") or 0) >= 2 and r.get("label") != "cex":
            hits.append(r)
    return hits

def _feed(mint, src, name=None):
    """Enter the normal waiting-room path. Curve coins wait for migration; pooled coins go to Scanner."""
    row = db.q("select status,pool from coins where mint=?", (mint,), True)
    if row and row["status"] in ("waiting", "passed", "open"): return "already-in"
    try:
        p = util.dex_tokens([mint], ttl=15, curve=True).get(mint)
    except Exception:
        p = None
    if p and p.get("dexId") != "pumpfun" and p.get("pairAddress"):
        from agents import trend
        n = trend.add([mint], src)
        return "waiting" if n else "rejected"
    # still on the curve: mark promising so migration puts it in the waiting room
    if not row:
        db.q("insert or ignore into coins(mint,name,source,found_at,status,reason) values(?,?,?,?, 'curve',?)",
             (mint, name, src, time.time(), "winners/gems watch"))
        return "curve"
    return row["status"] if row else "none"

def _alert(kind, name, mint, score, wins, why):
    key = f"{kind}:{mint}"
    if time.time() - _alerted.get(key, 0) < 6 * 3600: return
    _alerted[key] = time.time()
    notifier.send(f"PAPER · {kind}: {name or mint[:6]} · cluster {score:.0f} · {wins} wins\n{why}\n{links.coin_url(mint)}",
                  notifier.fomo_btn(mint))
    try:
        import bus; bus.activity(f"{kind}: {name or mint[:6]}", why, f"score {score:.0f}", "info", mint)
    except Exception:
        pass


def _map_profits(pool, mint):
    """Mark early wallets that later sold for more than they bought (GeckoTerminal trades)."""
    if not pool: return
    try:
        import gt
        trades = gt.trades(pool)
    except Exception:
        return
    bought, sold = collections.defaultdict(float), collections.defaultdict(float)
    first_buy, last_sell = {}, {}
    for tr in trades:
        w = tr.get("wallet")
        if not w: continue
        usd = float(tr.get("usd") or 0)
        if tr.get("kind") == "buy":
            bought[w] += usd
            first_buy[w] = min(first_buy.get(w, tr.get("ts") or 0) or 0, tr.get("ts") or 0) or (tr.get("ts") or 0)
        else:
            sold[w] += usd
            last_sell[w] = max(last_sell.get(w, 0), tr.get("ts") or 0)
    for w, s_usd in sold.items():
        if s_usd >= 20 and s_usd > bought.get(w, 0) * 1.15:
            hold = None
            if w in first_buy and last_sell.get(w) and last_sell[w] >= first_buy[w]:
                hold = last_sell[w] - first_buy[w]
            db.q("insert or ignore into winner_roles(mint,addr,role,buy_ts,sell_ts,hold_s,usd) values(?,?,?,?,?,?,?)",
                 (mint, w, "profited", first_buy.get(w), last_sell.get(w), hold, round(s_usd - bought.get(w, 0), 2)))

def rank_key(row):
    return (float(row.get("max_mult") or row.get("mult") or 0), float(row.get("ath_mc") or row.get("mc") or 0))

def pick_top10(s, cands):
    """Merge fresh candidates with known 24h winners; keep the top N by multiplier, then ATH MC."""
    n = int(s.get("win_top_n", 10))
    by = {}
    cutoff = time.time() - s.get("win_max_age_h", 24) * 3600
    for r in db.q("select * from winner_coins"):
        if r["rug"] or r["status"] == "rug": continue
        if not (r["max_mult"] or r["ath_mc"]): continue
        launched = r["launched_at"] or r["found_at"] or 0
        if launched and launched < cutoff: continue
        by[r["mint"]] = dict(r)
    for row in cands:
        if row.get("rug") or row.get("status") == "rug": continue
        cur = by.get(row["mint"], {})
        if cur.get("rug") or cur.get("status") == "rug": continue
        cur.update(row)
        cur["max_mult"] = max(cur.get("max_mult") or 0, row.get("mult") or 0)
        cur["ath_mc"] = max(cur.get("ath_mc") or 0, row.get("mc") or 0)
        by[row["mint"]] = cur
    ranked = sorted(by.values(), key=rank_key, reverse=True)[:n]
    db.q("update winner_coins set rank=null")
    for i, row in enumerate(ranked, 1):
        db.q("update winner_coins set rank=? where mint=?", (i, row["mint"]))
        row["rank"] = i
    return ranked

_idx = [0.0, {}]
def linked_index():
    """addr -> list of {mint,name,role,max_mult,rank} from mapped top-10 / analyzed winners."""
    if time.time() - _idx[0] < 20 and _idx[1]: return _idx[1]
    out = collections.defaultdict(list)
    rows = db.q("""select r.addr, r.role, r.mint, c.name, c.max_mult, c.ath_mc, c.rank
                   from winner_roles r join winner_coins c on c.mint=r.mint
                   where coalesce(c.rank,0)>0 or c.analyzed=1""")
    for r in rows:
        if not r["addr"] or r["addr"] in SYSTEM: continue
        out[r["addr"]].append({"mint": r["mint"], "name": r["name"], "role": r["role"],
                               "max_mult": r["max_mult"], "rank": r["rank"]})
    _idx[0], _idx[1] = time.time(), out
    return out

def _fmt_hit(addr, recs):
    recs = recs or []
    src = recs[0] if recs else {}
    name = src.get("name") or (src.get("mint") or "")[:6]
    roles = ",".join(sorted({r.get("role") or "?" for r in recs}))
    mult = src.get("max_mult") or 0
    a = f"{addr[:4]}…{addr[-4:]}" if addr and len(addr) > 8 else (addr or "?")
    return f"{a} · {roles} of {name} ({mult:.0f}x)"

def match_addr(addr, idx=None):
    if not addr: return []
    return (idx or linked_index()).get(addr) or []

def _watch_alert(name, mint, hits):
    """hits: list of (addr, role_on_new_coin, recs). One alert per mint per 6h."""
    key = f"watch:{mint}"
    if time.time() - _alerted.get(key, 0) < 6 * 3600: return
    _alerted[key] = time.time()
    lines = [f"PAPER · Winners watch: {name or mint[:6]}"]
    for addr, role, recs in hits[:8]:
        lines.append("• " + (role + " · " if role else "") + _fmt_hit(addr, recs))
    lines.append(links.coin_url(mint))
    notifier.send("\n".join(lines), notifier.fomo_btn(mint))
    try:
        import bus; bus.activity(f"Winners watch: {name or mint[:6]}", lines[1] if len(lines)>1 else "match", "watch", "info", mint)
    except Exception:
        pass
    log.info(f"watch hit {name or mint} n={len(hits)}")

def watch_coin(mint, creator=None, curve=None, name=None, s=None, src=""):
    """Flag a new coin if a mapped top-10 address is the creator, early buyer, or their funder."""
    s = s or config.load()
    idx = linked_index()
    if not idx: return
    hits = []
    if creator:
        recs = match_addr(creator, idx)
        if recs: hits.append((creator, "creator", recs))
        else:
            try:
                f = _funder(creator)
            except Exception:
                f = None
            recs = match_addr(f, idx)
            if recs: hits.append((f, "funder of creator", recs))
    if hits:
        _watch_alert(name, mint, hits)
    # always queue a delayed early-buyer pass (new coins are still young — shallow walk is enough)
    with _pl:
        _pending.append((time.time() + 25, mint, curve, creator, name))
        if len(_pending) > 40: del _pending[:len(_pending) - 40]

def _watch_new_pairs(s):
    mints = []
    try:
        from agents import trend
        mints += list(trend.gecko_new() or [])
    except Exception as e:
        log.info(f"new pairs {type(e).__name__}")
    seen = set()
    for m in mints:
        if not m or m in seen: continue
        seen.add(m)
        if db.q("select 1 from winner_coins where mint=?", (m,), True): continue
        watch_coin(m, s=s, src="new_pair")
        if len(seen) >= 25: break

async def ws_loop(stop):
    """PumpPortal new-token + migration stream (used when the old scanner is not running)."""
    import websockets
    WS = "wss://pumpportal.fun/api/data"
    back = 1
    while not stop.is_set():
        try:
            async with websockets.connect(WS, ping_interval=30) as w:
                await w.send(json.dumps({"method": "subscribeMigration"}))
                await w.send(json.dumps({"method": "subscribeNewToken"}))
                back = 1; log.info("ws connected")
                while not stop.is_set():
                    raw = await asyncio.wait_for(w.recv(), timeout=120)
                    m = json.loads(raw)
                    typ = m.get("txType")
                    if typ == "create":
                        asyncio.get_running_loop().run_in_executor(None, on_new_token, m)
                    elif typ == "migrate":
                        cr = m.get("traderPublicKey") or m.get("creator")
                        asyncio.get_running_loop().run_in_executor(None, on_migration, m.get("mint"), cr)
        except Exception as e:
            log.info(f"ws reconnect in {back}s ({type(e).__name__}: {e})")
            await asyncio.sleep(back); back = min(back * 2, 60)

def on_new_token(m):
    mint = m.get("mint"); creator = m.get("traderPublicKey") or m.get("creator")
    if not mint: return
    s = config.load()
    if not s.get("win_enabled", True): return
    watch_coin(mint, creator=creator, curve=m.get("bondingCurveKey"), name=m.get("name"), s=s, src="create")

def on_migration(mint, creator):
    s = config.load()
    if not s.get("win_enabled", True) or not mint: return
    watch_coin(mint, creator=creator, name=None, s=s, src="migrate")

def _drain_pending(s):
    now = time.time(); due = []
    with _pl:
        keep = []
        for item in _pending:
            (due if item[0] <= now else keep).append(item)
        _pending[:] = keep[-40:]
    for _, mint, curve, creator, name in due[:5]:
        db.beat("winners")
        try:
            buyers, _st = _first_buyers(curve, time.time() - 60, s) if curve else ([], {})
        except Exception as e:
            log.info(f"early {mint} {type(e).__name__}"); continue
        idx = linked_index()
        hits = []
        for b in buyers:
            recs = match_addr(b["addr"], idx)
            if recs: hits.append((b["addr"], "early buyer", recs))
            else:
                try: f = _funder(b["addr"])
                except Exception: f = None
                recs = match_addr(f, idx)
                if recs: hits.append((f, "funder of early buyer", recs))
        if hits:
            _watch_alert(name, mint, hits)
        for b in buyers:
            db.q("insert or ignore into winner_roles(mint,addr,role,buy_ts,sell_ts,hold_s,usd) values(?,?,?,?,null,null,null)",
                 (mint, b["addr"], "sniper", b["ts"]))

def text():
    top = db.q("select * from winner_coins where coalesce(rank,0)>0 and coalesce(rug,0)=0 order by rank")
    uniq = db.q("select count(distinct addr) n from winner_roles", one=True)["n"]
    lines = [f"Winners · top {len(top)} exploded (24h) · {uniq} linked wallets"]
    if not top:
        lines.append("No top-10 list yet — waiting for the first mapping cycle.")
    for c in top:
        counts = db.q("select role, count(*) n from winner_roles where mint=? group by role", (c["mint"],))
        parts = ", ".join(f"{r['n']} {r['role']}" for r in counts) or "not mapped yet"
        mapped = "mapped" if c["walk_done"] or c["analyzed"] else "mapping…"
        lines.append(f"#{c['rank']} {c['name'] or c['mint'][:6]} · {c['max_mult'] or 0:.0f}x · MC ${c['ath_mc'] or 0:,.0f} · {mapped} · {parts}" + (" · rug" if c["rug"] else ""))
    return "\n".join(lines)

def loop(stop):
    first = True
    while not stop.is_set():
        db.beat("winners")
        s = config.load()
        if not s.get("win_enabled", True) or not db.kv_get("desk_running", True):
            stop.wait(10); continue
        t0 = time.time()
        try:
            _drain_pending(s)
            db.beat("winners")
            log.info("discovering exploded pump coins")
            cands = discover(s)
            for row in cands: _save_coin(row, s)
            top = pick_top10(s, cands)
            log.info(f"found {len(cands)} candidates, mapping top {len(top)}")
            for row in top:
                db.beat("winners")
                if time.time() - t0 > 15 * 60:  # leave budget in the 20-min cycle for watching
                    log.info("time box: remaining top-10 coins resume next cycle")
                    break
                coin = db.q("select * from winner_coins where mint=?", (row["mint"],), True)
                if coin and coin["walk_done"] and coin["analyzed"] and not first:
                    continue
                try:
                    log.info(f"mapping #{row.get('rank')} {row.get('name') or row['mint']}")
                    row = {**dict(coin or {}), **row, "deep": True, "walk_before": (coin["walk_before"] if coin else None)}
                    analyze(row, s)
                except Exception as e:
                    log.info(f"analyze {row['mint']} {type(e).__name__}: {config.redact(e)}")
            try: _watch_new_pairs(s)
            except Exception as e: log.info(f"watch pairs {type(e).__name__}")
            _idx[0] = 0  # refresh linked-wallet cache
            log.info(f"cycle top={len(top)} linked={len(linked_index())}")
        except util.Http429:
            log.info("cycle 429, backing off"); stop.wait(45)
        except Exception as e:
            log.info(f"cycle err {type(e).__name__}: {config.redact(e)}")
        first = False
        wait = max(30, s.get("win_poll_s", 1200) - (time.time() - t0))
        stop.wait(wait)
