"""Wallet-cluster tracker (PAPER, read-only). Never places orders or touches funds.

Every few minutes: find recent pump.fun-origin coins that exploded (>=10x or MC >= $500k
within ~24h) via DexScreener / GeckoTerminal, then on-chain (budgeted RPC) pull the dev,
early buyers, and the SOL funder of those wallets. Wallets sharing a funder become a cluster.
Scores separate repeatable winners from dumpers/ruggers. New PumpPortal tokens whose creator
or early buyers match a good cluster get a Telegram alert and enter the normal Scanner ->
Auditor -> Analyst -> Executor -> Risk PAPER path (ZŁY is still never bought).
"""
import json, time, threading, collections, requests, db, util, config, notifier, links
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

def _first_buyers(addr, launched, s):
    """Earliest unique buyers on a pool or bonding-curve account. Budget-capped."""
    if not addr: return []
    window = s["win_early_window_s"]; need = s["win_early_n"]
    sigs, before, pages = [], None, 0
    oldest_needed = (launched or 0) - 30
    try:
        db.beat("winners")
        batch = util.research_rpc("getSignaturesForAddress", [addr, {"limit": 200}], ttl=45) or []
        if not batch: return []
        last_t = batch[-1].get("blockTime") or 0
        # hot / old pools: newest 200 txs are already far past the 30s sniper window — don't burn the RPC budget walking back
        if launched and last_t and last_t > launched + max(window, 120):
            log.info(f"skip early-buyer walk {addr[:6]} (oldest sig in last 200 is {int(last_t-launched)}s after launch)")
            return []
        sigs.extend(batch)
        while pages < 2 and last_t and last_t > oldest_needed and len(batch) == 200:
            before = batch[-1]["signature"]; pages += 1
            db.beat("winners")
            batch = util.research_rpc("getSignaturesForAddress", [addr, {"limit": 200, "before": before}], ttl=45) or []
            if not batch: break
            sigs.extend(batch)
            last_t = batch[-1].get("blockTime") or 0
    except Exception as e:
        log.info(f"sigs {type(e).__name__}"); return []
    early = []
    for x in reversed(sigs):  # oldest first
        bt = x.get("blockTime") or 0
        if x.get("err"): continue
        if launched and bt and bt < launched - 15: continue
        if launched and bt and bt > launched + window and len(early) >= 3: break
        early.append(x)
        if len(early) >= need * 2: break
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
                if len(buyers) >= need: return buyers
    return buyers

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
        if target and time.time() - t0 < 25:
            buyers = _first_buyers(target, row.get("launched") or 0, s)
        elif target:
            log.info(f"{mint[:6]} skip early buyers, {time.time()-t0:.0f}s already spent")
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
        _maybe_holds(row.get("pool"), mint)
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

def on_new_token(m):
    mint = m.get("mint"); creator = m.get("traderPublicKey") or m.get("creator")
    if not mint: return
    s = config.load()
    if not s.get("win_enabled", True): return
    info = known_dev(creator)
    if info and (info.get("use_score") or 0) >= s["win_alert_min_score"] and (info.get("cwins") or info.get("wins") or 0) >= 2 and info.get("label") != "dumper":
        _alert("Winners", m.get("name"), mint, info["use_score"], info.get("cwins") or info.get("wins") or 0,
               f"known dev {creator[:4]}…{creator[-4:]} · past wins {info.get('cwins') or info.get('wins')} · label {info.get('label') or 'watch'}")
        _feed(mint, "winners", m.get("name"))
    with _pl:
        _pending.append((time.time() + 25, mint, m.get("bondingCurveKey"), creator, m.get("name")))
        if len(_pending) > 40: del _pending[:len(_pending) - 40]

def on_migration(mint, creator):
    s = config.load()
    if not s.get("win_enabled", True): return
    info = known_dev(creator)
    if info and (info.get("use_score") or 0) >= s["win_min_cluster_score"] and info.get("label") != "dumper":
        _alert("Winners", None, mint, info["use_score"], info.get("cwins") or info.get("wins") or 0,
               f"migrated · known dev {creator[:4]}…{creator[-4:] if creator else ''}")
        _feed(mint, "winners")

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
            buyers = _first_buyers(curve, time.time() - 60, s) if curve else []
        except Exception as e:
            log.info(f"early {mint} {type(e).__name__}"); continue
        hits = known_good_cluster([b["addr"] for b in buyers], s)
        if hits:
            h = max(hits, key=lambda x: x.get("use_score") or 0)
            _alert("Winners", name, mint, h["use_score"], h.get("cwins") or h.get("wins") or 0,
                   f"early buyer from cluster #{h.get('cid') or h.get('cluster_id')} · score {h['use_score']:.0f}")
            _feed(mint, "winners", name)
        # persist early roles even if not a known cluster (builds the map)
        for b in buyers:
            db.q("insert or ignore into winner_roles(mint,addr,role,buy_ts,sell_ts,hold_s,usd) values(?,?,?,?,null,null,null)",
                 (mint, b["addr"], "sniper", b["ts"]))

def text():
    cs = db.q("select * from winner_clusters where coalesce(label,'')!='cex' order by score desc, wins desc limit 10")
    n = db.q("select count(*) n from winner_coins", one=True)["n"]
    a = db.q("select count(*) n from winner_coins where analyzed=1", one=True)["n"]
    lines = [f"Winners · {n} exploded coins · {a} analyzed · clusters {len(cs)}"]
    if not cs: lines.append("No clusters yet (needs a few analyzed winners + shared funders).")
    for c in cs:
        f = c["funder"] or "?"
        lines.append(f"#{c['id']} {c['label'] or 'watch'} · score {c['score'] or 0:.0f} · {c['wins'] or 0} wins · {c['rugs'] or 0} rugs · {c['wallets_n'] or 0} wallets · funder {f[:4]}…{f[-4:] if len(f)>8 else f}")
    recent = db.q("select name,mint,max_mult,ath_mc,rug from winner_coins order by found_at desc limit 5")
    if recent:
        lines.append("Recent:")
        for r in recent:
            lines.append(f"  {(r['name'] or r['mint'][:6])} · {r['max_mult'] or 0:.0f}x · MC ${r['ath_mc'] or 0:,.0f}" + (" · rug" if r["rug"] else ""))
    return "\n".join(lines)

def loop(stop):
    # first cycle soon so we have findings after restart
    first = True
    while not stop.is_set():
        db.beat("winners")
        s = config.load()
        if not s.get("win_enabled", True) or not db.kv_get("desk_running", True):
            stop.wait(10); continue
        try:
            _drain_pending(s)
            db.beat("winners")
            log.info("discovering exploded pump coins")
            cands = discover(s)
            log.info(f"found {len(cands)} winner candidates")
            fresh = []
            for row in cands:
                ex = db.q("select analyzed,ath_mc from winner_coins where mint=?", (row["mint"],), True)
                _save_coin(row, s)
                if not ex or not ex["analyzed"]: fresh.append(row)
            budget = s["win_max_analyze"] if not first else min(s["win_max_analyze"], 2)
            for row in fresh[:budget]:
                db.beat("winners")
                try:
                    log.info(f"analyzing {row.get('name') or row['mint']}")
                    analyze(row, s)
                except Exception as e: log.info(f"analyze {row['mint']} {type(e).__name__}: {config.redact(e)}")
            log.info(f"cycle cands={len(cands)} new={len(fresh)} analyzed={min(len(fresh), budget)}")
        except util.Http429:
            log.info("cycle 429, backing off"); stop.wait(45)
        except Exception as e:
            log.info(f"cycle err {type(e).__name__}: {config.redact(e)}")
        first = False
        stop.wait(s.get("win_poll_s", 180))
