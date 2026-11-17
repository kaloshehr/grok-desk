"""Smart-wallet tracker (PAPER, read-only).
Discovery: last ~300 trades of pumping Solana pools (GeckoTerminal, free) -> per-wallet realized PnL estimate
(USD sold - USD bought inside the window, only wallets that both bought and sold). Aggregated across coins:
coins traded, wins, win rate, PnL. Top wallets that meet min_coins/min_win_rate are auto-tracked.
Watching: tracked wallets' new signatures via the desk RPC ([rpc]); a token balance increase = a buy.
Bought coins go to the Scanner waiting room as source 'wallets' (still full Auditor) and give the Analyst a bonus."""
import json, time, collections, db, util, config, bus, gt
from agents import trend
log = util.logger("wallets")
DISCOVER_EVERY_S = 900
QUOTE = trend.SKIP
def _pools():
    out = []
    try:
        for d in gt.get("/networks/solana/trending_pools?page=1", 300).get("data") or []:
            a = d.get("attributes") or {}
            ch = float((a.get("price_change_percentage") or {}).get("h6") or 0)
            if ch > 20: out.append((a.get("address"), (a.get("name") or "")[:30]))
    except Exception as e: log.info(f"trending err {type(e).__name__}")
    for r in db.q("select pool,name from coins where pool is not null and found_at>? order by found_at desc limit 20", (time.time() - 86400,)):
        out.append((r["pool"], r["name"]))
    return list(dict.fromkeys(out))[:8]
def score_pool(pool):
    tr = gt.trades(pool); per = collections.defaultdict(lambda: [0.0, 0.0]); cnt = collections.Counter(t["wallet"] for t in tr)
    for t in tr:
        if t["wallet"]: per[t["wallet"]][0 if t["kind"] == "buy" else 1] += t["usd"]
    # >12 trades in ~300 = bot / arbitrage / market maker, not a trader to copy
    return {w: round(s - b, 2) for w, (b, s) in per.items() if b >= 20 and s > 0 and cnt[w] <= 12}
def discover():
    s = config.load(); seen = 0
    for pool, name in _pools():
        try: res = score_pool(pool)
        except util.Http429: log.info("gt 429"); break
        except Exception as e: log.info(f"pool err {type(e).__name__}"); continue
        for w, pnl in res.items():
            r = db.q("select json from wallets where addr=?", (w,), True)
            j = json.loads(r["json"]) if r else {}
            j[pool] = pnl  # same pool re-scanned -> overwrite, not double count
            vals = list(j.values()); wins = sum(1 for v in vals if v > 0)
            wr = wins / len(vals); tot = sum(vals)
            sc = round(wr * min(len(vals), 5) / 5 * 100 + min(tot, 5000) / 100, 1)
            if r: db.q("update wallets set coins=?,wins=?,pnl=?,win_rate=?,score=?,json=? where addr=?", (len(vals), wins, round(tot, 2), wr, sc, json.dumps(j), w))
            else: db.q("insert into wallets(addr,origin,tracked,coins,wins,pnl,win_rate,score,added_at,json) values(?,?,?,?,?,?,?,?,?,?)",
                       (w, "auto", 0, 1, wins, round(tot, 2), wr, sc, time.time(), json.dumps(j)))
            seen += 1
    # auto-track best qualifying wallets; manual ones are never auto-removed
    q = db.q("select addr from wallets where origin='auto' and coins>=? and win_rate>=? and pnl>0 order by score desc limit ?", (s["w_min_coins"], s["w_min_wr"], s["w_max"]))
    keep = {r["addr"] for r in q}
    db.q("update wallets set tracked=0 where origin='auto'")
    for a in keep: db.q("update wallets set tracked=1 where addr=?", (a,))
    db.q("delete from wallets where origin='auto' and tracked=0 and coins<2 and added_at<?", (time.time() - 3 * 86400,))
    log.info(f"discover scored={seen} tracked_auto={len(keep)}")
def buys_of(addr, s):
    r = db.q("select last_sig from wallets where addr=?", (addr,), True)
    sigs = util.rpc(s["rpc_url"], "getSignaturesForAddress", [addr, {"limit": 15}])
    if not sigs: return []
    newest = sigs[0]["signature"]; last = r["last_sig"] if r else None
    db.q("update wallets set last_sig=? where addr=?", (newest, addr))
    if not last: return []  # first look = baseline, don't replay history
    out = []
    for x in sigs:
        if x["signature"] == last: break
        if x.get("err"): continue
        try: tx = util.rpc(s["rpc_url"], "getTransaction", [x["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
        except Exception: continue
        m = (tx or {}).get("meta") or {}
        pre = {b["mint"]: float(b["uiTokenAmount"]["uiAmount"] or 0) for b in m.get("preTokenBalances") or [] if b.get("owner") == addr}
        for b in m.get("postTokenBalances") or []:
            if b.get("owner") != addr or b["mint"] in QUOTE: continue
            d = float(b["uiTokenAmount"]["uiAmount"] or 0) - pre.get(b["mint"], 0)
            if d > 0: out.append((b["mint"], d, x["signature"], x.get("blockTime") or time.time()))
    return out
def watch():
    s = config.load(); found = []
    for r in db.q("select addr from wallets where tracked=1 and coalesce(origin,'')<>'kol'"):
        db.beat("wallets")  # a full pass can take long under RPC 429 backoff
        try: found += [(r["addr"],) + b for b in buys_of(r["addr"], s)]
        except Exception as e: log.info(f"watch err {config.redact(e)}")
    if not found: return
    px = util.dex_tokens(list({f[1] for f in found}))
    for addr, mint, amt, sig, ts in found:
        p = px.get(mint) or {}; usd = round(amt * float(p.get("priceUsd") or 0), 2)
        if usd < 20: continue  # dust / arbitrage leg / no Solana DEX pair
        if time.time() - (p.get("pairCreatedAt") or 0) / 1000 > trend.MAX_AGE_H * 3600: continue  # majors (wBTC, JUP...) are not memecoin signals
        db.q("insert or ignore into wallet_buys(addr,mint,ts,usd,sig) values(?,?,?,?,?)", (addr, mint, ts, usd, sig))
        db.q("update wallets set last_buy_ts=?,last_buy_mint=? where addr=?", (ts, mint, addr))
        name = util.get(p, "baseToken.symbol") or mint[:6]
        bus.activity(f"Smart wallet bought {name}", f"Wallets · {addr[:4]}…{addr[-4:]} · ${usd:,.0f}", "Wallets", "scan", mint)
        try: n = trend.add([mint], "wallets")
        except Exception: n = 0
        log.info(f"buy {addr[:6]} {mint} ${usd} fed={n}")
def backers(mint, hours=6):
    return [r["addr"] for r in db.q("select distinct b.addr from wallet_buys b join wallets w on w.addr=b.addr where b.mint=? and b.ts>? and w.tracked=1", (mint, time.time() - hours * 3600))]
def _discover_safe():
    try: discover()
    except Exception as e: log.info(f"discover crash {type(e).__name__}: {e}")
def loop(stop):
    import threading
    last_d = 0; dt = None
    while not stop.is_set():
        db.beat("wallets")
        s = config.load()
        if s["w_enabled"] and db.kv_get("desk_running", True):
            if time.time() - last_d > DISCOVER_EVERY_S and (dt is None or not dt.is_alive()):
                dt = threading.Thread(target=_discover_safe, daemon=True, name="wallets_discover"); dt.start()
                last_d = time.time()
            try: watch()
            except Exception as e: log.info(f"watch crash {config.redact(e)}")
        stop.wait(s["w_poll_s"])
