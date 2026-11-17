"""Agent 1 Scanner. PumpPortal migrations -> waiting room -> DexScreener every 60s -> kill/pass."""
import asyncio, json, time, re, threading, datetime, websockets, db, util, bus, config, fields, stats
from concurrent.futures import ThreadPoolExecutor
from agents import prefilter
log = util.logger("scanner")
WS = "wss://pumpportal.fun/api/data"
NC = {"Tokenkeg": fields.TOKEN_PROGRAM}
def norm(s): return re.sub(r"[^a-z]", "", (s or "").lower())
def today(): return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

def on_migration(msg):
    mint = msg.get(fields.PUMPPORTAL_MIGRATION["mint"]); stats.ev("seen_migration")
    if not mint: return
    row = db.q("select status from coins where mint=?", (mint,), True)
    if row and row["status"] == "curve":  # pre-filter flagged it promising: now it has a pool -> waiting room, full checks
        db.q("update coins set status='waiting',reason=null,migrated_at=?,found_at=?,signature=? where mint=?", (time.time(), time.time(), msg.get("signature"), mint))
        log.info(f"waiting {mint} (curve -> migrated)"); return
    if row: return
    creator = cbal = None
    try:
        rep = util.retry(lambda: util.rugcheck(mint), 2, 2)
        creator = rep.get(fields.RUGCHECK_REPORT["creator"]); cbal = rep.get(fields.RUGCHECK_REPORT["creatorBalance"])
    except Exception as e: log.info(f"{mint} rugcheck creator fail {e}")
    db.q("insert or ignore into coins(mint,creator,creator_balance,migrated_at,signature) values(?,?,?,?,?)",
         (mint, creator, cbal, time.time(), msg.get("signature")))
    log.info(f"waiting {mint} creator={creator}")
    try:
        from agents import winners
        winners.on_migration(mint, creator)
    except Exception as e: log.info(f"winners hook {type(e).__name__}")

async def ws_loop(stop):
    s = config.load(); back = 1
    while not stop.is_set():
        try:
            async with websockets.connect(WS, ping_interval=30) as w:
                await w.send(json.dumps({"method": "subscribeMigration"}))
                await w.send(json.dumps({"method": "subscribeNewToken"})); back = 1; log.info("ws connected")
                while not stop.is_set():
                    raw = await asyncio.wait_for(w.recv(), timeout=s["ws_silent_s"])
                    m = json.loads(raw)
                    if m.get("txType") == "migrate": asyncio.get_running_loop().run_in_executor(None, on_migration, m)
                    elif m.get("txType") == "create": prefilter.on_create(m)
        except Exception as e:
            log.info(f"ws reconnect in {back}s ({type(e).__name__}: {e})"); await asyncio.sleep(back); back = min(back*2, 60)

BURN = {"1nc1nerator11111111111111111111111111111111", "11111111111111111111111111111111"}
def top10_pct(rpc_url, mint, pool, excl_owners):
    sup = float(util.rpc(rpc_url, "getTokenSupply", [mint])["value"]["uiAmountString"])
    accs = util.rpc(rpc_url, "getTokenLargestAccounts", [mint])["value"]
    infos = util.rpc(rpc_url, "getMultipleAccounts", [[a["address"] for a in accs], {"encoding": "jsonParsed"}])["value"]
    owners = [util.get(inf or {}, "data.parsed.info.owner") for inf in infos]
    ou = [o for o in dict.fromkeys(owners) if o]
    oi = dict(zip(ou, util.rpc(rpc_url, "getMultipleAccounts", [ou, {"encoding": "base64"}])["value"])) if ou else {}
    tot = 0; n = 0
    for a, owner in zip(accs, owners):
        if owner in excl_owners or a["address"] in excl_owners or owner in BURN: continue
        oa = oi.get(owner)  # pool vaults / lockers / bonding curves are owned by PDAs: no system account or program-owned
        if oa is None or oa.get("owner") != "11111111111111111111111111111111": continue
        tot += float(a["uiAmountString"]); n += 1
        if n == 10: break
    return 100 * tot / sup if sup else 100

def decide(c, p, s):
    mint = c["mint"]; name = util.get(p, "baseToken.name"); sym = util.get(p, "baseToken.symbol")
    pool = p.get("pairAddress"); liq = util.get(p, "liquidity.usd") or 0; h1 = util.get(p, "priceChange.h1") or 0
    age = (time.time()*1000 - (p.get("pairCreatedAt") or time.time()*1000)) / 60000
    db.q("update coins set name=?,symbol=?,pool=? where mint=?", (name, sym, pool, mint))
    if (util.get(p, "priceChange.h24") or 0) >= s["runner_h24_pct"]:
        for k in {norm(name), norm(sym)} - {""}: db.q("insert or ignore into runners values(?,?)", (today(), k))
    reason = None; top10 = None; stats.ev("scan_check")
    if liq < s["min_liq_usd"]: reason = f"liq ${liq/1000:.1f}K"
    else:
        try:
            excl = {pool, util.get(p, "pairAddress")}
            top10 = top10_pct(s["rpc_url"], mint, pool, excl)
        except Exception as e: log.info(f"{mint} top10 err {e}"); return  # retry next round
        if top10 > s["max_top10_pct"]: reason = f"top 10 hold {top10:.0f}%"
        elif h1 > s["max_h1_pct"]: reason = f"+{h1:.0f}% in 1h"
        else:
            me = {norm(name), norm(sym)} - {""}
            for (k,) in [tuple(r) for r in db.q("select key from runners where day=?", (today(),))]:
                if (util.get(p, "priceChange.h24") or 0) >= s["runner_h24_pct"]: break  # it's the runner itself
                if any(k == m or (len(k) > 2 and len(m) > 2 and (k in m or m in k)) for m in me):
                    reason = f"copycat of {k}"; break
    if reason is None and age < s["min_pool_age_min"]: return  # keep waiting
    if reason and not db.kv_get(f"rc:{mint}") and (reason.startswith("+") or (top10 is not None and top10 <= s["max_top10_pct"] + 15) or (reason.startswith("liq") and liq >= 0.7 * s["min_liq_usd"])):
        db.kv_set(f"rc:{mint}", time.time() + 300); stats.ev("scan_recheck"); log.info(f"{mint} near miss ({reason}), recheck in 5m"); return
    verdict = "kill" if reason else "pass"; stats.ev("scan_" + verdict)
    out = {"agent":"scanner","mint":mint,"name":name,"symbol":sym,"pool":pool,"liquidityUsd":liq,
           "top10Pct":round(top10,1) if top10 is not None else None,"h1ChangePct":h1,"ageMin":int(age),
           "verdict":verdict,"reason":reason,"ts":util.now_iso()}
    db.q("update coins set status=?,reason=?,scan_liq=?,passed_at=? where mint=?", (verdict, reason, liq, time.time(), mint))
    db.q("insert into scans(mint,ts,verdict,reason,json) values(?,?,?,?,?)", (mint, out["ts"], verdict, reason, json.dumps(out)))
    log.info(f"{out['ts']} {mint} {verdict} {reason} liq={liq} top10={top10} h1={h1} age={int(age)}")
    if verdict == "pass": bus.auditor.put(out)

def poll_loop(stop):
    while not stop.is_set():
        db.beat("scanner")
        s = config.load()
        if db.kv_get("paused:scanner"): stop.wait(5); continue
        try:
            room = db.q("select * from coins where status='waiting'")
            for c in room:
                if time.time() - (c["found_at"] or c["migrated_at"]) > s["waiting_max_min"]*60:
                    db.q("update coins set status='drop',reason='never qualified' where mint=?", (c["mint"],))
                    out = {"agent":"scanner","mint":c["mint"],"name":c["name"],"verdict":"kill","reason":"never qualified","ts":util.now_iso()}
                    db.q("insert into scans(mint,ts,verdict,reason,json) values(?,?,?,?,?)", (c["mint"], out["ts"], "kill", "never qualified", json.dumps(out)))
            room = db.q("select * from coins where status='waiting'")
            if room:
                pairs = util.dex_tokens([c["mint"] for c in room], ttl=0)
                jobs = []
                for c in room:
                    p = pairs.get(c["mint"])
                    rc = db.kv_get(f"rc:{c['mint']}")
                    if rc and time.time() < rc: continue
                    if not p: continue  # DexScreener doesn't know it yet
                    db.q("insert into prices values(?,?,?,?)", (c["mint"], time.time(), float(p.get("priceUsd") or 0), util.get(p,"volume.m5")))
                    jobs.append((c, p))
                with ThreadPoolExecutor(max_workers=s["scan_workers"]) as ex:  # bounded pool; util.rpc is rate-limited
                    for f in [ex.submit(decide, c, p, s) for c, p in jobs]:
                        try: f.result()
                        except Exception as e: log.info(f"decide err {e}")
        except util.Http429: log.info("dex 429, wait"); stop.wait(s["dex_429_wait_s"]); continue
        except Exception as e: log.info(f"poll err {e}")
        end = time.time() + s["scan_poll_s"]
        while time.time() < end and not stop.is_set(): db.beat("scanner"); stop.wait(min(15, end - time.time()))
