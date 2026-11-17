"""Pre-filter for brand-new pump.fun coins (PumpPortal subscribeNewToken).
Cheap: uses the create message + ONE batched getMultipleAccounts per 100 bonding curves (curve progress).
FOMO does not document trading pump.fun bonding-curve coins, so nothing here is ever sent to the Auditor:
promising curve coins are only marked status='curve'; they enter the normal waiting room (full Scanner ->
Auditor -> Analyst) when they migrate / get a pool. Kills are aggregated, never one Activity row per coin."""
import time, base64, struct, threading, db, util, config, bus, stats
log = util.logger("prefilter")
INIT_REAL_TOKENS = 793_100_000 * 10**6
_l = threading.Lock(); W = {}  # mint -> dict
KILLS = {}
def on_create(m):
    mint = m.get("mint"); stats.ev("seen_new")
    if not mint or not m.get("bondingCurveKey"): return
    s = config.load(); dev = 100 * float(m.get("initialBuy") or 0) / 1e9
    if dev > s["pre_max_dev_buy_pct"]: return kill(mint, "dev buy")
    with _l: W[mint] = {"ts": time.time(), "curve": m["bondingCurveKey"], "dev": dev, "name": m.get("name"), "sym": m.get("symbol"),
                        "peak": 0, "prog": 0, "promoted": False}
    try:
        from agents import winners, gems
        winners.on_new_token(m); gems.on_new_token(m)
    except Exception as e: log.info(f"research hook {type(e).__name__}")
def kill(mint, why):
    stats.ev("pre_kill"); stats.ev("pre_kill:" + why); KILLS[why] = KILLS.get(why, 0) + 1
    with _l: v = W.pop(mint, None)
    if v and v.get("promoted"): db.q("update coins set status='drop',reason=? where mint=? and status='curve'", ("pre-filter: " + why, mint))
def curves(keys, rpc_url):
    out = {}
    for i in range(0, len(keys), 100):
        vals = util.rpc(rpc_url, "getMultipleAccounts", [keys[i:i+100], {"encoding": "base64"}])["value"]
        for k, v in zip(keys[i:i+100], vals):
            if not v: continue
            b = base64.b64decode(v["data"][0])
            if len(b) < 49: continue
            vt, vs, rt, rs, sup = struct.unpack_from("<5Q", b, 8)
            out[k] = (max(0.0, min(100.0, 100 * (1 - rt / INIT_REAL_TOKENS))), bool(b[48]))
    return out
def tick(s):
    now = time.time()
    with _l: items = [(k, dict(v)) for k, v in W.items() if now - v["ts"] >= 60]
    if not items: return
    got = curves([v["curve"] for _, v in items], s["rpc_url"]); stats.ev("pre_check", len(items))
    for mint, v in items:
        r = got.get(v["curve"])
        if not r: continue
        prog, done = r; age = (now - v["ts"]) / 60; peak = max(v["peak"], prog)
        with _l:
            if mint in W: W[mint].update(prog=prog, peak=peak)
        if done or prog >= 99.5:  # graduating: the migrate event will put it in the waiting room
            with _l: W.pop(mint, None)
            stats.ev("pre_graduated"); continue
        if peak - prog >= s["pre_dump_pts"]: kill(mint, "dumped on curve"); continue
        if age >= 3 and peak < 5: kill(mint, "dead curve"); continue
        if age >= 10 and peak < 25: kill(mint, "slow curve"); continue
        if age >= s["pre_watch_max_min"]: kill(mint, "stalled"); continue
        if prog >= s["pre_promote_progress"] and not v["promoted"]:
            with _l:
                if mint in W: W[mint]["promoted"] = True
            stats.ev("pre_pass")
            db.q("insert or ignore into coins(mint,name,symbol,migrated_at,source,found_at,status,reason) values(?,?,?,?,?,?,'curve',?)",
                 (mint, v["name"], v["sym"], None, "curve", now, f"curve {prog:.0f}%"))
def summary(stop):
    while not stop.wait(600):
        if True:
            w = stats.window(10); n = w.get("seen_new", 0)
            if n: bus.activity(f"Pre-filter · {n} new coins in 10 min", f"{w.get('pre_kill',0)} cut · {w.get('pre_pass',0)} promising · {w.get('pre_graduated',0)} graduated", "Scanner", "info")
def loop(stop):
    threading.Thread(target=summary, args=(stop,), daemon=True, name="prefilter.summary").start()
    while not stop.is_set():
        db.beat("prefilter"); s = config.load()
        if not s["pre_enabled"] or db.kv_get("paused:scanner"): stop.wait(10); continue
        try: tick(s)
        except Exception as e: log.info(f"tick err {e}")
        stop.wait(s["pre_poll_s"])
def size():
    with _l: return len(W)

def watched():
    with _l: return {k: dict(v) for k, v in W.items()}

