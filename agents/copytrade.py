"""Copy trading (PAPER). Top-N wallets by 7-day realized PnL from Helius enhanced SWAP history.
Detection: Helius websocket logsSubscribe(mentions=wallet) per top wallet; fallback poll of enhanced API every copy.poll_s.
Entry: next real DexScreener quote + simulated price impact (size vs pool quote reserve) + FOMO fee. Fast rug check only.
Exits handled in risk.py (tp / tp2 / stop / trail) + follow_sell here. LIVE mode: signal only, no position."""
import re, os, json, time, threading, asyncio, statistics, requests
import db, util, config, bus, fees, links, notifier
log = util.logger("copy")
SOL = "So11111111111111111111111111111111111111112"; USDC = "EPjFWdd5AufqSSqeM2qJxNZoBmBFNdjYddw4ZQwKYsjv"
H_SP = util.Spacer(); _seen = set(); _busy = set(); _sl = threading.Lock()
def _key():
    m = re.search(r"api-key=([\w-]+)", os.environ.get("DESK_RPC_URL", "")); return m.group(1) if m else None
def h_get(path, params=None, body=None):
    k = _key()
    if not k: raise RuntimeError("no helius key")
    H_SP.wait(0.25); util._count("rl:helius")
    url = "https://api.helius.xyz/v0/" + path
    try:
        r = requests.post(url, params={"api-key": k}, json=body, timeout=25) if body is not None else requests.get(url, params={"api-key": k, **(params or {})}, timeout=25)
    except Exception as e: raise RuntimeError(f"helius {type(e).__name__}") from None
    if r.status_code == 429: H_SP.backoff(5); raise util.Http429()
    if r.status_code != 200: raise RuntimeError(f"helius {r.status_code}")
    return r.json()
_solp = [0.0, 0]
def sol_usd():
    if time.time() - _solp[1] < 120 and _solp[0]: return _solp[0]
    try:
        d = requests.get(f"https://lite-api.jup.ag/price/v3?ids={SOL}", timeout=10).json(); v = float(d[SOL]["usdPrice"])
    except Exception:
        try:
            d = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{SOL}", timeout=10).json()
            v = float([p for p in d["pairs"] if p["quoteToken"]["symbol"] in ("USDC", "USDT")][0]["priceUsd"])
        except Exception: v = _solp[0] or 150.0
    _solp[:] = [v, time.time()]; return v
def parse(tx, w):
    """-> (quote_delta_sol_equiv, {mint: token_delta}) for wallet w; usdc counted at /sol price later via 'usdc' key."""
    sol = sum((a.get("nativeBalanceChange") or 0) for a in tx.get("accountData", []) if a.get("account") == w) / 1e9
    toks = {}; usdc = 0.0
    for t in tx.get("tokenTransfers", []):
        amt = float(t.get("tokenAmount") or 0); m = t.get("mint")
        d = amt if t.get("toUserAccount") == w else (-amt if t.get("fromUserAccount") == w else 0)
        if not d: continue
        if m == SOL: sol += d
        elif m == USDC: usdc += d
        else: toks[m] = toks.get(m, 0) + d
    return sol, usdc, {m: d for m, d in toks.items() if abs(d) > 1e-9}
_deadline = [0.0]
def history(w, days=7, pages=8):
    out = []; before = None; cut = time.time() - days*86400
    for _ in range(pages):
        if _deadline[0] and time.time() > _deadline[0]: raise RuntimeError("refresh deadline")
        p = {"type": "SWAP", "limit": 100}
        if before: p["before"] = before
        b = h_get(f"addresses/{w}/transactions", p)
        if not b: break
        out += [t for t in b if (t.get("timestamp") or 0) >= cut]
        if (b[-1].get("timestamp") or 0) < cut: return out, True
        before = b[-1]["signature"]
        if len(out) >= 200:  # early bot exit: rate already far above any human
            span = max((out[0]["timestamp"] - out[-1]["timestamp"]) / 86400, 0.01)
            if len(out)/span > 3*config.load()["copy_max_trades_day"]: return out, False
    return out, False
def score_wallet(w, creators, solp):
    txs, full = history(w); txs.sort(key=lambda t: t.get("timestamp") or 0)
    r = {"addr": w, "trades": len(txs), "bot": None}
    if not txs: r["bot"] = "no swaps"; return r
    span_d = max((time.time() - txs[0]["timestamp"]) / 86400, 0.25) if not full else 7
    if not full: span_d = max((txs[-1]["timestamp"] - txs[0]["timestamp"]) / 86400, 0.05)
    r["tpd"] = round(len(txs) / span_d, 1)
    per = {}; sizes = []; arb = 0
    for t in txs:
        sol, usdc, toks = parse(t, w); q = sol + (usdc / solp if solp else 0)
        if len(toks) >= 2 or (not toks): arb += 1; continue
        (m, d), = toks.items(); x = per.setdefault(m, {"spent":0,"got":0,"bought":0,"sold":0,"first":t["timestamp"],"last":t["timestamp"]})
        x["last"] = t["timestamp"]; sizes.append(abs(q))
        if d > 0 and q < 0: x["spent"] += -q; x["bought"] += d
        elif d < 0 and q > 0: x["got"] += q; x["sold"] += -d
    closed = []; insider = 0; holds = []
    for m, x in per.items():
        if x["sold"] <= 0: continue
        if x["bought"] <= 0 or x["sold"] > x["bought"]*1.05: insider += 1; continue
        f = min(1, x["sold"]/x["bought"]); pnl = x["got"] - x["spent"]*f; closed.append(pnl); holds.append(x["last"]-x["first"])
    r.update(mints=len(per), closed=len(closed), insider=insider, arb=arb,
             pnl_sol=round(sum(closed), 3), pnl=round(sum(closed)*solp, 2),
             win_rate=round(sum(1 for p in closed if p > 0)/len(closed), 3) if closed else 0,
             med_sol=round(statistics.median(sizes), 3) if sizes else 0, med_hold_s=int(statistics.median(holds)) if holds else 0)
    s = config.load()
    if r["tpd"] > s["copy_max_trades_day"]: r["bot"] = f"{r['tpd']:.0f} trades/day"
    elif arb > 0.3*len(txs): r["bot"] = "arb/MEV pattern"
    elif r["med_sol"] < 0.05: r["bot"] = "dust size"
    elif w in creators: r["bot"] = "coin creator"
    elif insider >= 5 and insider > 0.6*(len(closed)+insider): r["bot"] = "insider (sells tokens it never bought)"
    elif holds and r["med_hold_s"] < 20: r["bot"] = "sub-20s holds (sniper bot)"
    return r
def refresh(force=False):
    s = config.load()
    n = db.q("select count(*) n from copy_top", one=True)["n"]; every = s["copy_refresh_h"]*3600 if n >= s["copy_top_n"] else 1800
    if not force and time.time() - (db.kv_get("copy_top_at", 0) or 0) < every: return
    cands = [r["addr"] for r in db.q("select addr from wallets order by score desc, pnl desc limit ?", (s["copy_candidates"],))]
    cands += [r["addr"] for r in db.q("select addr from wallets order by added_at desc limit ?", (s["copy_candidates"]//2,)) if r["addr"] not in cands]
    cands += [r["addr"] for r in db.q("select addr from copy_top") if r["addr"] not in cands]
    creators = {r["creator"] for r in db.q("select creator from coins where creator is not null")}
    kols = {r["addr"] for r in db.q("select addr from wallets where origin='kol' and tracked=1")}
    cands = [a for a in kols if a not in cands] + cands
    solp = sol_usd(); res = []; t0 = time.time(); _deadline[0] = t0 + 600
    for w in cands:
        c = db.kv_get(f"copy_score:{w}")
        if c and (c.get("bot") or w in kols) and time.time() - c.get("_t", 0) < 24*3600: res.append(c); continue
        if time.time() - t0 > 600: break
        try: r = score_wallet(w, creators, solp); r["_t"] = time.time(); db.kv_set(f"copy_score:{w}", r); res.append(r)
        except util.Http429: time.sleep(5)
        except Exception as e: log.info(f"score {w[:6]} err {e}")
    ok = [r for r in res if not r["bot"] and r.get("closed", 0) >= s["copy_min_closed"] and r.get("win_rate", 0) >= s["copy_min_wr"] and r.get("pnl", 0) > 0]
    for r in ok: r["score"] = round(r["pnl"] * (0.5 + r["win_rate"]), 2)
    ok.sort(key=lambda r: -r["score"])
    # pinned KOL wallets (origin='kol'): exempt from trades/day + arb heuristics (KOLs trade a lot, multi-hop routes);
    # dropped only if the desk sees a sniper pattern or a clear loss (>=3 closed and pnl<0)
    byaddr = {r["addr"]: r for r in res}; pinned = []
    for k in db.q("select addr,json from wallets where origin='kol' and tracked=1 order by score desc"):
        r = byaddr.get(k["addr"]) or {}; _m = json.loads(k["json"] or "{}")
        if not _m.get("locked") and (str(r.get("bot") or "").startswith("sub-20s") or (r.get("closed", 0) >= 3 and r.get("pnl", 0) < 0)):
            log.info(f"kol {k['addr'][:6]} rejected: {r.get('bot') or 'losing'}"); continue
        meta = json.loads(k["json"] or "{}"); pr = {"addr": k["addr"], "pnl": meta.get("kolscan_pnl_usd") or r.get("pnl", 0), "win_rate": r.get("win_rate", 0),
              "closed": r.get("closed", 0), "trades": r.get("trades", 0), "tpd": r.get("tpd", 0), "score": meta.get("score", 0), "kol": meta}
        pinned.append(pr)
    pa = {r["addr"] for r in pinned}
    top = (pinned + [r for r in ok if r["addr"] not in pa])[:s["copy_top_n"]]
    _deadline[0] = 0.0
    # keep previously followed wallets that were not re-checked or still look clean (don't wipe the list on an empty/partial scan)
    bad = {r["addr"] for r in res if r.get("bot") or (r.get("closed") is not None and r.get("pnl", 0) <= 0)}
    if len(top) < s["copy_top_n"]:
        have = {r["addr"] for r in top}
        for row in db.q("select json from copy_top order by rank"):
            try: r = json.loads(row["json"])
            except Exception: continue
            if r.get("addr") and r["addr"] not in have and r["addr"] not in bad and len(top) < s["copy_top_n"]:
                top.append(r); have.add(r["addr"])
    db.q("delete from copy_top")
    for i, r in enumerate(top, 1):
        db.q("insert into copy_top values(?,?,?,?,?,?,?,?,?,?)", (r["addr"], i, r["pnl"], r["win_rate"], r["closed"], r["trades"], r["tpd"], r["score"], time.time(), json.dumps(r)))
    rej = {}
    for r in res:
        if r["bot"]: rej[r["bot"].split(" (")[0] if "trades/day" not in r["bot"] else "bot frequency"] = rej.get(r["bot"].split(" (")[0] if "trades/day" not in r["bot"] else "bot frequency", 0) + 1
    db.kv_set("copy_top_at", time.time())
    db.kv_set("copy_top_meta", {"scanned": len(res), "eligible": len(ok), "excluded": rej, "secs": round(time.time()-t0), "at": time.time()})
    log.info(f"top refresh scanned={len(res)} eligible={len(ok)} top={[r['addr'][:6] for r in top]} excl={rej}")
    bus.activity("Top 10 wallets refreshed", f"Copy · {len(res)} scanned, {len(ok)} eligible", f"{len(top)} copied", "info")
def top_addrs(): return [r["addr"] for r in db.q("select addr from copy_top order by rank")]
# ---------- detection ----------
_sig_sem = threading.BoundedSemaphore(12)
def spawn_sig(w, sig, src):
    """cap concurrent signature lookups; drop when saturated (poll_backup catches real swaps)."""
    if not _sig_sem.acquire(blocking=False): util._count("copy:sig_dropped"); return
    def run():
        try: handle_sig(w, sig, src)
        finally: _sig_sem.release()
    threading.Thread(target=run, daemon=True).start()
def handle_sig(w, sig, src="ws"):
    with _sl:
        if sig in _seen or sig in _busy: return
        _busy.add(sig)
    try:
        det = time.time(); tx = None
        for _ in range(8):
            try:
                b = h_get("transactions", body={"transactions": [sig]}); tx = b[0] if b else None
                if tx: break
            except Exception: pass
            time.sleep(1.5)
        if not tx: util._count("copy:not_indexed"); return
        with _sl:
            _seen.add(sig)
            if len(_seen) > 20000: _seen.clear()
        # terminal-routed swaps (Axiom/Photon etc.) are often typed TRANSFER/SYSTEM_PROGRAM by Helius; parse() still requires SOL out + exactly one token in
        if tx.get("transactionError") or tx.get("type") not in ("SWAP", "UNKNOWN", "TRANSFER") or tx.get("feePayer") not in (None, w): return
        sol, usdc, toks = parse(tx, w)
        if len(toks) != 1: return
        (mint, d), = toks.items(); bt = tx.get("timestamp") or det
        util._count("copy:swap_seen"); log.info(f"{src} {w[:6]} {('buy' if sol < -0.01 else 'recv (no SOL paid: airdrop/dust, ignored)') if d > 0 else 'sell'} {mint[:6]} sol={sol:.3f} lat={det-bt:.1f}s")
        if d > 0 and sol < -0.01: on_buy(w, mint, -sol, bt, det, src)
        elif d < 0: on_sell(w, mint, bt, det)
    finally:
        with _sl: _busy.discard(sig)
def ev(addr, mint, kind, bt, det, action, reason):
    db.q("insert into copy_events(ts,addr,mint,kind,block_ts,detect_ts,action,reason,latency) values(?,?,?,?,?,?,?,?,?)",
         (time.time(), addr, mint, kind, bt, det, action, reason, round(det-bt, 2)))
def fast_rug(mint, p, s):
    a = util.rpc(s["rpc_url"], "getAccountInfo", [mint, {"encoding": "jsonParsed"}]); info = util.get(a, "value.data.parsed.info") or {}
    if info.get("mintAuthority") is not None: return "mint not revoked"
    if info.get("freezeAuthority") is not None: return "freeze not revoked"
    pool = p.get("pairAddress")
    try:
        rep = util.rugcheck(mint, ttl=600); mk = [m for m in (rep.get("markets") or []) if m.get("pubkey") == pool]
        lp = util.get(mk[0], "lp.lpLockedPct") if mk else None
        if lp is not None and lp < s["lp_locked_min_pct"]: return "LP unlocked"
    except Exception: pass
    if s.get("copy_hard_rug_only"): return None
    from agents.scanner import top10_pct
    t = top10_pct(s["rpc_url"], mint, pool, {pool})
    if t >= s["max_top10_pct"]: return f"top10 {t:.0f}%"
    return None
def impact(size, liq): return min(0.25, size / max(liq/2, 1))
import threading as _th
_INS_LOCK = _th.Lock()
def on_buy(w, mint, sol_amt, bt, det, src, attempt=0):
    s = config.load(); rank = db.q("select rank from copy_top where addr=?", (w,), True); tag = f"#{rank['rank']} {w[:4]}…{w[-4:]}" if rank else w[:6]
    def no(why): ev(w, mint, "buy", bt, det, "skip", why); log.info(f"skip {mint} from {tag}: {why} lat={det-bt:.1f}s"); return None
    if not s["copy_enabled"]: return no("copy off")
    if not db.kv_get("desk_running", True): return no("desk stopped")
    if sol_amt < s["copy_min_buy_sol"]: return no(f"wallet buy {sol_amt:.3f} SOL < min")
    wm = json.loads((db.q("select json from wallets where addr=?", (w,), True) or {"json": "{}"})["json"] or "{}")
    if wm.get("paused"): return no(f"wallet paused: {wm.get('paused')}")
    # rolling buy sizes -> median; conviction tracker (any followed wallet buying this mint)
    hist = (db.kv_get(f"copy_buys:{w}") or [])[-60:]; med = sorted(hist)[len(hist)//2] if len(hist) >= 5 else None
    if not attempt: db.kv_set(f"copy_buys:{w}", hist + [round(sol_amt, 4)])  # retries don't re-count
    now = time.time(); win = s.get("copy_window_min", 30)*60
    cv = [x for x in (db.kv_get(f"copy_conv:{mint}") or []) if now - x[1] < win and x[0] != w] + [[w, now]]
    db.kv_set(f"copy_conv:{mint}", cv); n_w = len({x[0] for x in cv})
    big = sol_amt >= s.get("copy_big_buy_sol", 1.0) or (med is not None and sol_amt >= s.get("copy_med_mult", 2.0)*med)
    if not big: return no(f"small buy {sol_amt:.3f} SOL (median {med if med is not None else '?'}; need ≥{s.get('copy_big_buy_sol',1.0)} SOL or ≥{s.get('copy_med_mult',2.0)}x median)")
    if db.q("select 1 from positions where strategy='copy' and mint=? and opened_at>?", (mint, now - win), True): return no("coin already copied in last 30 min")
    day0 = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    nd = db.q("select count(*) n from positions where strategy='copy' and opened_at>=?", (day0,), True)["n"]
    if s.get("copy_daily_cap") and nd >= s["copy_daily_cap"]: return no(f"daily copy cap {s['copy_daily_cap']} reached")
    if s["copy_max_open"] and db.q("select count(*) n from positions where strategy='copy' and status in ('open','exiting')", one=True)["n"] >= s["copy_max_open"]: return no(f"max copy positions ({s['copy_max_open']} open) — not selling to make room")
    p = None
    for _ in range(3):  # brand-new curve coins can take a few seconds to show on DexScreener
        p = util.dex_tokens([mint], ttl=0, curve=True).get(mint)
        if p and float(p.get("priceUsd") or 0): break
        time.sleep(2)
    if not p or not float(p.get("priceUsd") or 0):
        delays = (25, 60)  # retry later (timer thread, re-runs every gate) before giving up
        if attempt < len(delays):
            import threading; t = threading.Timer(delays[attempt], on_buy, (w, mint, sol_amt, bt, det, src, attempt + 1)); t.daemon = True; t.start()
            log.info(f"no price yet {mint} from {tag}: retry #{attempt+1} in {delays[attempt]}s"); return None
        return no("no price on DexScreener yet (after 25s + 60s retries)")
    liq = util.get(p, "liquidity.usd") or 0.2*(p.get("marketCap") or p.get("fdv") or 0)
    try: bad = fast_rug(mint, p, s)
    except Exception as e: bad = None; log.info(f"rug check err {type(e).__name__}")
    if bad: return no("rug: " + bad)
    rr = db.q("select report,social from coin_extra where mint=?", (mint,), True)
    v = (json.loads(rr["report"] or "null") or {}).get("verdict") if rr else None
    if v == "ZŁY": return no("raport ZŁY")
    if s.get("mom_enabled"):
        from agents import momentum; mo = momentum.check(mint, p, s, "copy")
        if mo["hard"]: return no(f"momentum: {mo['hard']}")
        socials = bool(rr and (json.loads(rr["social"] or "null") or {}).get("listed")) or bool(util.get(p, "info.socials") or util.get(p, "info.websites"))
    from agents import executor
    st = executor.stack(s); free = st - executor.open_money()
    size = round(s.get("copy_ticket_usd") or st*s["copy_size_pct"]/100, 2)
    if n_w >= 2 and s.get("copy_conviction_usd"): size = float(s["copy_conviction_usd"])
    if s.get("tier_enabled"):
        size, tier, cap = executor.tier_size(s, st, util.get(p, "liquidity.usd") or liq, v, mo if s.get("mom_enabled") else None, n_w=n_w, big=sol_amt >= 2*s.get("copy_big_buy_sol", 1.0))
        log.info(f"copy size {mint} tier={tier} cap={cap} size={size} n_w={n_w} sol={sol_amt:.2f}")
        if size < s.get("min_size_usd", 0): return no(f"ticket ${size} < ${s['min_size_usd']} (pool too small)")
    if size + fees.fee(size) > free: return no(f"no cash: free ${free:.2f} < ticket ${size}")
    import guard; g = guard.block(s, size + fees.fee(size), free)
    if g: return no(g)
    name = util.get(p, "baseToken.symbol") or mint[:6]; link = links.coin_url(mint, "solana")
    time.sleep(1.0)  # simulated tap/route latency
    p2 = util.dex_tokens([mint], ttl=0, curve=True).get(mint) or p; q = float(p2.get("priceUsd") or 0) or float(p["priceUsd"])
    imp = impact(size, util.get(p2, "liquidity.usd") or liq); fill = q * (1 + imp + 0.005)
    if s["mode"] != "PAPER":
        ev(w, mint, "buy", bt, det, "signal", "LIVE signal only")
        notifier.send(f"Copy signal · {tag} bought {name} · ${size}", [[{"text":"Buy on FOMO","url":link}]]); return
    with _INS_LOCK:  # positions.mint is UNIQUE: never insert twice (races between wallets / retries)
        ex = db.q("select status from positions where mint=?", (mint,), True)
        if ex: return no(f"position for this coin already exists ({ex['status']}), not inserting twice")
        db.kv_set("paper_balance", db.kv_get("paper_balance", s["paper_balance"]) - size - fees.fee(size))
        _ins(w, mint, name, s, fill, size, sol_amt, med, n_w, q, imp, rank, bt, det, tag, link, p2, liq, src)
    return
def _ins(w, mint, name, s, fill, size, sol_amt, med, n_w, q, imp, rank, bt, det, tag, link, p2, liq, src):
    meta = {"copied": w, "rank": rank["rank"] if rank else None, "walletSol": round(sol_amt,3), "walletMedSol": med, "conviction": n_w, "quote": q, "impactPct": round(100*imp,2), "latencyS": round(time.time()-bt,1), "detectS": round(det-bt,1), "src": src, "link": link, "sizeUsd": size}
    db.q("insert into positions(mint,name,mode,entry,size,peak,stop_pct,opened_at,status,json,strategy,copied,orig_size) values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
         (mint, name, s["mode"], fill, size, fill, s["stop_pct"], time.time(), "open", json.dumps(meta), "copy", w, size))
    ev(w, mint, "buy", bt, det, "bought", f"fill +{100*(fill/q-1):.2f}% vs quote")
    log.info(f"COPY BUY {name} {mint} from {tag} size={size} quote={q} fill={fill} det={det-bt:.1f}s total={time.time()-bt:.1f}s")
    bus.activity(f"Bought {name}", f"Kopia · {tag}", f"${size}", "buy", mint)
    notifier.send(f"PAPER · Kopia: {tag} kupił {name} → kupuję ${size} · TP +{s['copy_tp_pct']}% · stop -{s['stop_pct']}%\nopóźnienie {time.time()-bt:.0f}s · poślizg {100*(fill/q-1):.1f}%", [[{"text":"Buy on FOMO","url":link}]])
def on_sell(w, mint, bt, det):
    s = config.load(); poss = db.q("select * from positions where mint=? and status='open' and strategy='copy' and copied=?", (mint, w))
    if not poss: return
    if not s["copy_follow_sell"]: ev(w, mint, "sell", bt, det, "ignored", "follow_sell off"); return
    p = util.dex_tokens([mint], ttl=0, curve=True).get(mint); q = float(p.get("priceUsd") or 0) if p else 0
    if not q: return
    from agents import risk
    liq = util.get(p, "liquidity.usd") or 0.2*(p.get("marketCap") or p.get("fdv") or 0)
    for pos in poss:
        px = q * (1 - impact(pos["size"], liq) - 0.005)
        ev(w, mint, "sell", bt, det, "sold", "follow"); risk.close(pos, px, "follow", s)
# ---------- watchers ----------
async def ws_watch(stop):
    import websockets
    url = os.environ.get("DESK_RPC_URL", "").replace("https://", "wss://")
    while not stop.is_set():
        addrs = top_addrs()
        if not addrs or not url.startswith("wss://"): await asyncio.sleep(10); continue
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, open_timeout=15, close_timeout=5, max_size=2**22) as c:
                ids = {}
                for i, a in enumerate(addrs):
                    await c.send(json.dumps({"jsonrpc":"2.0","id":i+1,"method":"logsSubscribe","params":[{"mentions":[a]},{"commitment":"confirmed"}]}))
                subs = {}; t0 = time.time(); db.kv_set("copy_ws", {"up": True, "n": len(addrs), "at": t0})
                ws_watch._back = 5; log.info(f"ws subscribed to {len(addrs)} wallets")
                last_rx = time.time()
                while not stop.is_set():
                    db.kv_set("copy_ws_alive", time.time())
                    if top_addrs() != addrs: break
                    if time.time() - last_rx > 600: log.info("ws idle 10 min, reconnecting"); break
                    try: m = json.loads(await asyncio.wait_for(c.recv(), 5)); last_rx = time.time()
                    except asyncio.TimeoutError: continue
                    if "id" in m and "result" in m: subs[m["result"]] = addrs[m["id"]-1]; continue
                    v = util.get(m, "params.result.value") or {}; w = subs.get(util.get(m, "params.subscription"))
                    if w and v.get("signature") and not v.get("err"):
                        db.kv_set("copy_last_ws", time.time())
                        spawn_sig(w, v["signature"], "ws")
        except Exception as e:
            err = type(e).__name__
            db.kv_set("copy_ws", {"up": False, "err": err, "at": time.time()}); log.info(f"ws err {err}")
            # InvalidStatus is usually HTTP 429/401 from Helius; back off hard so we stop burning quota
            if err == "InvalidStatus":
                back = min(int(getattr(ws_watch, "_back", 15)) * 2, 300)
                ws_watch._back = back
            else:
                ws_watch._back = 5; back = 5
            await asyncio.sleep(back)
def poll_backup(stop):
    """Safety net: every poll_s*4 check latest swap of each top wallet (catches ws gaps)."""
    while not stop.is_set():
        s = config.load()
        ws = db.kv_get("copy_ws") or {}
        gap = s["copy_poll_s"] if not ws.get("up") else s["copy_poll_s"]*6
        if not ws.get("up") and ws.get("err") == "InvalidStatus":
            gap = max(gap, 120)
        for w in top_addrs():
            try:
                b = [t for t in h_get(f"addresses/{w}/transactions", {"limit": 15}) if t.get("feePayer") == w][:5]
                last = db.kv_get(f"copy_seen:{w}")
                for t in b:
                    if last is None: break
                    if t["signature"] == last: break
                    if time.time() - (t.get("timestamp") or 0) < 300: spawn_sig(w, t["signature"], "poll")
                if b: db.kv_set(f"copy_seen:{w}", b[0]["signature"])
            except Exception: pass
        stop.wait(gap)
def _refresh_safe():
    try: refresh()
    except Exception as e: log.info(f"refresh err {e}")
    finally: _deadline[0] = 0.0
def _ws_runner(stop):
    while not stop.is_set():
        try: asyncio.run(ws_watch(stop))
        except Exception as e: log.info(f"ws runner err {type(e).__name__}"); time.sleep(5)
def loop(stop):
    """Heartbeat never blocks on the (slow, network-bound) top refresh; watchdog restarts dead watcher threads."""
    th = {}
    mk = {"copy_poll": lambda: threading.Thread(target=poll_backup, args=(stop,), daemon=True, name="copy_poll"),
          "copy_ws": lambda: threading.Thread(target=_ws_runner, args=(stop,), daemon=True, name="copy_ws")}
    rt = None
    while not stop.is_set():
        db.beat("copy")
        for k, f in mk.items():
            if k not in th or not th[k].is_alive():
                if k in th: log.info(f"watchdog: restarting {k}")
                th[k] = f(); th[k].start()
        if rt is None or not rt.is_alive():
            rt = threading.Thread(target=_refresh_safe, daemon=True, name="copy_refresh"); rt.start()
        stop.wait(30)
