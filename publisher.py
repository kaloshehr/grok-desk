"""Plan B: GitHub Pages. Every publish_every_s, one orphan commit force-pushed to gh-pages
(web app + /d/{DATA_PATH}/*.json). History stays 1 commit. Token only via GIT credential helper from env."""
import os, json, time, shutil, subprocess, db, util, config, bus, links
from agents import auditor
log = util.logger("publisher")
SITE = os.path.join(config.BASE, "site"); WEB = os.path.join(config.BASE, "web")
REPO = "https://github.com/kaloshehr/grok-desk.git"
HELPER = '!f(){ echo username=x-access-token; echo "password=$GH_DESK_TOKEN"; }; f'
def web_version():
    import hashlib; return hashlib.md5(open(os.path.join(WEB, "index.html"), "rb").read()).hexdigest()[:10]
def git(*a): return subprocess.run(["git", "-C", SITE, "-c", "credential.helper=", "-c", f"credential.helper={HELPER}", *a], capture_output=True, text=True, timeout=120)
def candles(mint, n=240):
    rows = db.q("select ts,price from prices where mint=? and price>0 and ts>? order by ts", (mint, time.time()-n*60))
    out = {}
    for r in rows:
        m = int(r["ts"]//60)*60; c = out.get(m)
        if not c: out[m] = [m, r["price"], r["price"], r["price"], r["price"]]
        else: c[2] = max(c[2], r["price"]); c[3] = min(c[3], r["price"]); c[4] = r["price"]
    return list(out.values())  # [t,o,h,l,c]
def _with_links(d):
    import re
    ms = set(re.findall(r'"mint": "([1-9A-HJ-NP-Za-km-z]{32,44})"', json.dumps(d)))
    d["links"] = {m: links.coin_url(m) for m in ms}; d["fomoHome"] = links.FOMO_HOME; return d
def market(mints):
    try: P = util.dex_tokens(list(mints)) if mints else {}
    except Exception: return {}
    return {m: {"symbol": util.get(p, "baseToken.symbol"), "name": util.get(p, "baseToken.name"), "priceUsd": float(p.get("priceUsd") or 0),
                "marketCap": p.get("marketCap") or p.get("fdv"), "volH1": util.get(p, "volume.h1"), "pair": p.get("pairAddress"), "createdAt": p.get("pairCreatedAt"), "image": util.get(p, "info.imageUrl")} for m, p in P.items()}
def _funnel():
    import stats
    try: from agents import prefilter; watch = prefilter.size()
    except Exception: watch = 0
    w = stats.window(15); seen = w.get("seen_new", 0) + w.get("seen_trend", 0) + w.get("seen_migration", 0)
    an = db.q("select count(*) n, sum(passed) p from analyses where ts>?", (util.now_iso_ago(900),), True)
    return {"windowMin": 15, "perMin": round(seen / 15, 1), "seen": seen, "new": w.get("seen_new", 0), "trend": w.get("seen_trend", 0),
            "migrations": w.get("seen_migration", 0), "repeatsPerMin": round(w.get("seen_repeat", 0) / 15, 1), "watching": watch, "preKill": w.get("pre_kill", 0), "prePass": w.get("pre_pass", 0),
            "scanCheck": w.get("scan_check", 0), "scanPass": w.get("scan_pass", 0), "auditPass": w.get("audit_pass", 0), "auditKill": w.get("audit_kill", 0),
            "analystCheck": an["n"] or 0, "analystPass": an["p"] or 0,
            "rateLimited": sum(v for k, v in w.items() if k.endswith("_429")),
            "preKillWhy": {k[9:]: v for k, v in w.items() if k.startswith("pre_kill:")}}
def snapshot(): return _with_links(_snapshot())
def _snapshot():
    s = config.load(); from agents.executor import stack, ticket_check
    rows = lambda sql, a=(): [dict(r) for r in db.q(sql, a)]
    st = stack(s)
    pos = rows("select * from positions order by id desc limit 50")
    for p in pos:
        tp = db.kv_get(f"tp:{p['id']}")
        if tp: p["tp_at"], p["tp_px"] = tp
    mints = {p["mint"] for p in pos[:10]} | {json.loads(r["json"])["mint"] for r in db.q("select json from audits order by ts desc limit 10")}
    xm = {r["mint"] for r in db.q("select mint from coin_extra order by ts desc limit 8")} | {r["mint"] for r in db.q("select distinct mint from wallet_buys order by ts desc limit 8")}
    J = lambda sql, n=20: [json.loads(r["json"]) for r in db.q(sql + f" limit {n}")]
    return {"ts": util.now_iso(), "mode": s["mode"], "stack": tick()["e"], "stackCost": round(st, 2), "startStack": s["paper_balance"] if s["mode"] == "PAPER" else s["stack"],
            "paperBalance": round(db.kv_get("paper_balance", s["paper_balance"]), 2), "fomoHandle": s["fomo_handle"],
            "profileUrl": links.profile_url(s["fomo_handle"]), "settings": s["_n"], "ticketWarning": ticket_check(push=False),
            "webVersion": web_version(),
            "deskRunning": bool(db.kv_get("desk_running", True)),
            "paused": [a for a in ("scanner","auditor","analyst","executor","risk") if db.kv_get(f"paused:{a}")],
            "funnel": _funnel(), "momentum": __import__("agents.momentum", fromlist=["x"]).recent(10), "momentumBeat": (db.q("select ts from heartbeats where agent='momentum'", one=True) or {"ts": None})["ts"],
            "counts": {"sources": {r["source"] or "pumpportal": r["n"] for r in db.q("select source,count(*) n from coins group by 1")}, "pools": db.q("select count(*) n from coins",one=True)["n"],
                       "waiting": db.q("select count(*) n from coins where status='waiting'",one=True)["n"],
                       "audited": db.q("select count(*) n from audits",one=True)["n"],
                       "rejected": db.q("select count(*) n from audits where passed=0",one=True)["n"],
                       "scored": db.q("select count(*) n from analyses",one=True)["n"],
                       "trades": db.q("select count(*) n from positions",one=True)["n"]},
            "gpaDownKills": db.kv_get("gpa_down_kills", 0), "lastExit": db.kv_get("last_exit"), "live": bus.live,
            "equity": [[int(r["ts"]), round(r["price"], 2)] for r in db.q("select ts,price from prices where mint='__equity__' order by ts desc limit 1500")][::-1],
            "scans": J("select json from scans order by id desc"), "audits": J("select json from audits order by ts desc"),
            "analyses": J("select json from analyses order by ts desc"),
            "executor": rows("select mint,ts,reason from skips order by ts desc limit 20"),
            "positions": pos, "activity": rows("select * from activity order by id desc limit 60"),
            "changeLog": rows("select * from change_log order by id desc limit 20"),
            "names": {r["mint"]: r["name"] for r in db.q("select mint,name from coins where name is not null order by migrated_at desc limit 300")},
            "wallets": [dict(r, url=links.wallet_url(r["addr"])) for r in db.q("select addr,origin,tracked,coins,wins,pnl,win_rate,score,last_buy_ts,last_buy_mint,added_at from wallets where tracked=1 or origin='manual' order by tracked desc, score desc limit 40")],
            "copy": {"enabled": bool(s["copy_enabled"]), "tp": s["copy_tp_pct"], "twoStep": bool(s["copy_two_step"]), "tp2": s["copy_tp2_pct"], "followSell": bool(s["copy_follow_sell"]), "maxOpen": s["copy_max_open"], "sizePct": s["copy_size_pct"],
                     "top": [dict(r, url=links.wallet_url(r["addr"]), extra={k: v for k, v in json.loads(r["json"] or "{}").items() if k in ("mints","med_sol","med_hold_s","tpd")}) for r in db.q("select addr,rank,pnl,win_rate,closed,trades,tpd,score,updated,json from copy_top order by rank")],
                     "meta": db.kv_get("copy_top_meta"), "ws": db.kv_get("copy_ws"),
                     "events": rows("select ts,addr,mint,kind,action,reason,latency from copy_events order by id desc limit 30")},
            "walletCandidates": db.q("select count(*) n from wallets", one=True)["n"],
            "walletBuys": rows("select addr,mint,ts,usd from wallet_buys order by ts desc limit 40"),
            "extra": {r["mint"]: {"vq": r["vq"], "volume": json.loads(r["vq_json"] or "{}"), "social": json.loads(r["social"] or "null"), "report": json.loads(r["report"] or "null"), "wallets": json.loads(r["wallets"] or "[]")} for r in db.q("select * from coin_extra order by ts desc limit 40")},
            "learning": {"enabled": bool(s["learn_enabled"]), "every": s["learn_every"], "last": db.kv_get("learn_last"), "bounds": {f"{a}.{b}": v for (a, b), v in __import__("learning").SAFE.items()}},
            "candles": {m: candles(m) for m in mints}, "market": market(list(dict.fromkeys([p["mint"] for p in pos[:20]] + sorted(mints | xm)))[:40]), "lastPublish": db.kv_get("last_publish")}
def publish():
    dp = config.secret("DATA_PATH")
    if os.path.exists(SITE): shutil.rmtree(SITE)
    shutil.copytree(WEB, SITE, ignore=shutil.ignore_patterns("vapid*"))
    ix = os.path.join(SITE, "index.html"); h = open(ix).read(); open(ix, "w").write(h.replace("__VER__", web_version()))
    os.makedirs(os.path.join(SITE, "d", dp))
    json.dump(snapshot(), open(os.path.join(SITE, "d", dp, "state.json"), "w"))
    open(os.path.join(SITE, ".nojekyll"), "w").close()
    for c in (["init", "-q", "-b", "gh-pages"], ["add", "-A"],
              ["-c", "user.name=desk", "-c", "user.email=desk@localhost", "commit", "-qm", "publish"]):
        r = git(*c)
        if r.returncode: log.info(f"git {c[0]} fail"); return False
    r = git("push", "-qf", REPO, "gh-pages")
    ok = r.returncode == 0
    log.info("published" if ok else f"push fail rc={r.returncode} {r.stderr[-200:].replace(os.environ.get('GH_DESK_TOKEN','~~'),'***')}")
    if ok: db.kv_set("last_publish", util.now_iso())
    return ok
# ---------- near-live channel: small JSON pushed to ntfy.sh (free, CORS, SSE) ----------
# Topic = sha256("desk-live:" + DATA_PATH)[:32]: same secrecy as the state.json path, no new secret, nothing in the repo.
NTFY = "https://ntfy.sh/"
def live_topic():
    import hashlib; return "dk" + hashlib.sha256(("desk-live:" + config.secret("DATA_PATH")).encode()).hexdigest()[:30]
def live_payload():
    s = config.load(); from agents.executor import stack
    pos = [dict(r) for r in db.q("select id,mint,name,mode,entry,size,peak,stop_pct,trail_active,opened_at,status,exit_price,exit_reason,closed_at,pnl,fees,strategy,copied,orig_size from positions where status in ('open','exiting') or closed_at>? order by id desc limit 12", (time.time()-900,))]
    px = {}
    for p in pos:
        r = db.q("select price from prices where mint=? order by ts desc limit 1", (p["mint"],), True)
        if r: px[p["mint"]] = r["price"]
    act = [dict(r) for r in db.q("select * from activity order by id desc limit 10")]
    d = {"v": 1, "ts": util.now_iso(), "t": time.time(), "mode": s["mode"], "stack": round(stack(s), 2), "paperBalance": round(db.kv_get("paper_balance", s["paper_balance"]), 2),
         "deskRunning": bool(db.kv_get("desk_running", True)), "positions": pos, "prices": px, "activity": act, "copyWs": db.kv_get("copy_ws")}
    for k in ("activity", "positions"):
        while len(json.dumps(d, separators=(",", ":"))) > 3900 and d[k]: d[k].pop()
    return d
def tick():
    """compact mark-to-market: cash, equity (cash + open positions at latest risk price), [id, price, value, pnl] per open position."""
    s = config.load(); cash = db.kv_get("paper_balance", s["paper_balance"]) if s["mode"] == "PAPER" else None
    rows = []; mtm = 0.0
    for p in db.q("select id,mint,entry,size from positions where status in ('open','exiting') order by id desc limit 30"):
        r = db.q("select price,ts from prices where mint=? order by ts desc limit 1", (p["mint"],), True)
        px = r["price"] if r and r["price"] else p["entry"]; val = p["size"] * px / p["entry"] if p["entry"] else p["size"]
        mtm += val; rows.append([p["id"], p["mint"], float(f"{px:.6g}"), round(val, 2), round(val - p["size"], 2), int(r["ts"]) if r else 0])
    eq = round((cash or 0) + mtm, 2) if cash is not None else round(s["stack"], 2)
    return {"t": round(time.time(), 1), "c": round(cash, 2) if cash is not None else None, "e": eq, "p": rows}
EQ_LAST = [0.0]
LIVE_MAX = 3800; HEAVY_EVERY_S = 30; BUCKET = [10.0, 0.0]  # ntfy.sh free: ~1 msg/5s sustained, burst 60 -> we use <=10/min
def _pack(obj):
    raw = json.dumps(obj, separators=(",", ":"))
    if len(raw) <= LIVE_MAX: return {"s": obj}, len(raw)
    import gzip, base64
    z = base64.b64encode(gzip.compress(raw.encode(), 9)).decode()
    return ({"z": z}, len(z)) if len(z) <= LIVE_MAX - 40 else (None, len(z))
def live_loop(stop):
    """v2: every app section; only changed sections are sent; packed into <=3.8KB messages (gzip+base64 when needed)."""
    import requests, hashlib
    topic = None; sent_h = {}; heavy_at = {}; last_any = 0
    while not stop.is_set():
        s = config.load()
        try:
            topic = topic or live_topic()
            snap = snapshot(); now = time.time()
            BUCKET[0] = min(10.0, BUCKET[0] + (now - (BUCKET[1] or now)) / 5.0); BUCKET[1] = now
            ch = []
            for k, v in snap.items():
                if k in ("ts", "lastPublish", "equity", "candles", "stack"): continue  # equity/candles/stack come live via the q tick + 30s state.json
                h = hashlib.md5(json.dumps(v, sort_keys=True, default=str).encode()).hexdigest()
                if sent_h.get(k) != h: ch.append((k, v, h))
            prio = ["positions", "paperBalance", "stack", "deskRunning", "paused", "lastExit", "live", "activity", "learning", "settings", "funnel", "momentum", "momentumBeat", "counts",
                    "ticketWarning", "executor", "copy", "wallets", "walletBuys", "webVersion", "changeLog"]
            ch.sort(key=lambda x: prio.index(x[0]) if x[0] in prio else 99)
            msgs = []; cur = {}; chunks = []
            for k, v, h in ch:
                if len(json.dumps(v, separators=(",", ":"), default=str)) > LIVE_MAX and now - heavy_at.get(k, 0) < HEAVY_EVERY_S: continue
                trial = {**cur, k: v}; pk, n = _pack(trial)
                if pk: cur = trial; cur_h = None; continue
                if cur: msgs.append(cur)
                pk, n = _pack({k: v})
                cur = {k: v} if pk else {}
                if not pk: chunks.append((k, v, h))
            if cur: msgs.append(cur)
            for k, v, h in chunks:  # gzip then split into <=3 parts; bigger ones are left to the gh-pages snapshot
                import gzip, base64
                z = base64.b64encode(gzip.compress(json.dumps(v, separators=(",", ":"), default=str).encode(), 9)).decode(); n = -(-len(z) // 3600)
                if n > 3 or BUCKET[0] < n + 1: 
                    if n > 3: sent_h[k] = h
                    continue
                ok = True
                for i in range(n):
                    body = json.dumps({"v": 2, "t": now, "ts": snap.get("ts"), "c": {"k": k, "id": h[:8], "i": i, "n": n}, "p": z[i*3600:(i+1)*3600]}, separators=(",", ":"))
                    r = requests.post(NTFY + topic, data=body.encode(), headers={"Firebase": "no"}, timeout=10); BUCKET[0] -= 1
                    if r.status_code != 200: ok = False; break
                if ok: sent_h[k] = h; heavy_at[k] = now; last_any = now
            tk = tick()
            if now - EQ_LAST[0] >= 60:  # mark-to-market equity history, 1 sample/min
                EQ_LAST[0] = now; db.q("insert into prices values(?,?,?,?)", ("__equity__", now, tk["e"], None))
            if msgs and len(json.dumps({**msgs[0], "q": tk}, separators=(",", ":"), default=str)) <= LIVE_MAX: msgs[0] = {**msgs[0], "q": tk}
            else: msgs = [{"q": tk}] + msgs
            for m in msgs:
                if BUCKET[0] < 1: break
                pk, _ = _pack(m); body = json.dumps({"v": 2, "t": now, "ts": snap.get("ts"), **pk}, separators=(",", ":"))
                r = requests.post(NTFY + topic, data=body.encode(), headers={"Firebase": "no"}, timeout=10)
                if r.status_code == 429: log.info("live 429, backing off"); BUCKET[0] = 0; break
                if r.status_code != 200: log.info(f"live push http {r.status_code}"); break
                BUCKET[0] -= 1; last_any = now; db.kv_set("live_last", now)
                for k in m:
                    if k == "q": continue
                    sent_h[k] = hashlib.md5(json.dumps(snap[k], sort_keys=True, default=str).encode()).hexdigest()
                    if len(json.dumps(snap[k], separators=(",", ":"), default=str)) > LIVE_MAX: heavy_at[k] = now
        except Exception as e: log.info(f"live err {type(e).__name__}")
        stop.wait(max(4, s["live_every_s"]))
def loop(stop):
    import threading
    threading.Thread(target=live_loop, args=(stop,), daemon=True, name="live").start()
    while not stop.is_set():
        try: publish()
        except Exception as e: log.info(f"err {type(e).__name__} {e}")
        stop.wait(config.load()["publish_every_s"])
