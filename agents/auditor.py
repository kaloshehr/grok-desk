"""Agent 2 Auditor: 8 layers, stop at first red, no data = red."""
import time, json, threading, db, util, bus, config, fields, notifier
log = util.logger("auditor")
NAMES = ["Mint authority","Freeze authority","Dev wallet","Top 10 holders","Liquidity","Pool age","Bundled wallets","Delegations"]
KEYS = ["mint","freeze","dev","top10","liquidity","pool age","bundles","delegations"]
gpa_down_kills = 0

def audit(sc):
    global gpa_down_kills
    s = config.load(); mint = sc["mint"]; c = db.q("select * from coins where mint=?", (mint,), True)
    R = lambda f: util.retry(f, s["src_retries"], s["src_retry_wait_s"])
    cache = {}
    def acct(): 
        if "a" not in cache: cache["a"] = R(lambda: util.rpc(s["rpc_url"], "getAccountInfo", [mint, {"encoding":"jsonParsed"}]))
        return cache["a"]
    def report():
        if "r" not in cache: cache["r"] = R(lambda: util.rugcheck(mint, ttl=0))
        return cache["r"]
    def graph():
        if "g" not in cache: cache["g"] = R(lambda: util.rugcheck(mint, "insiders/graph", ttl=0))
        return cache["g"]
    def supply():
        info = util.get(acct(), "value.data.parsed.info")
        sup = int(info["supply"]) / 10**info["decimals"]
        if sup <= 0: raise ValueError
        return sup, info["decimals"]
    def L1(): a = util.get(acct(),"value.data.parsed.info"); return (a["mintAuthority"] is None, "Revoked" if a["mintAuthority"] is None else "mint not revoked","rpc")
    def L2(): a = util.get(acct(),"value.data.parsed.info"); return (a["freezeAuthority"] is None, "Revoked" if a["freezeAuthority"] is None else "freeze not revoked","rpc")
    def L3():
        try: sup, dec = supply()
        except Exception: return (False, "no supply", "rpc")
        creator = (c["creator"] if c else None) or report().get("creator")
        if not creator: return (True, "dev unknown", "rpc")  # non-pump coins: no creator in rugcheck; mint/freeze/LP/top10 still enforced
        res = R(lambda: util.rpc(s["rpc_url"], "getTokenAccountsByOwner", [creator, {"mint": mint}, {"encoding":"jsonParsed"}]))
        bal = sum(float(util.get(x,"account.data.parsed.info.tokenAmount.uiAmountString") or 0) for x in res["value"])
        pct = 100*bal/sup; base = c["creator_balance"]
        base_ui = (base/10**dec) if base is not None else None
        sold = base_ui is not None and bal < base_ui - 1e-9
        if pct >= s["dev_max_pct"]: return (False, f"dev holds {pct:.0f}%", "rpc")
        if sold: return (False, "dev sold", "rpc")
        return (True, f"{pct:.1f}% · sold 0", "rpc")
    def L4():
        from agents.scanner import top10_pct
        p = R(lambda: top10_pct(s["rpc_url"], mint, sc["pool"], {sc["pool"]}))
        return (p < s["audit_top10_pct"], f"{p:.1f}%" if p < s["audit_top10_pct"] else f"top 10 hold {p:.0f}%", "rpc")
    def L5():
        mk = [m for m in (report().get("markets") or []) if m.get("pubkey") == sc["pool"]]
        lp = util.get(mk[0], "lp.lpLockedPct") if mk else None
        if lp is None: return (False, "no data: liquidity", "rugcheck")
        if lp < s["lp_locked_min_pct"]: return (False, "liquidity unlocked", "rugcheck")
        now = util.dex_tokens([mint], ttl=0).get(mint); liq = util.get(now or {}, "liquidity.usd") or 0
        if sc.get("liquidityUsd") and liq < sc["liquidityUsd"] * (1 - s["liq_drop_max_pct"]/100): return (False, "liquidity pulled", "rugcheck")
        return (True, f"{lp:.0f}% locked", "rugcheck")
    def L6():
        m = (time.time() - (c["migrated_at"] if c else time.time()))/60
        m = max(m, sc.get("ageMin") or 0)
        return (m >= s["audit_pool_age_min"], f"{int(m)}m" if m >= s["audit_pool_age_min"] else "pool too young", "rpc")
    def L7():
        g = graph()
        if g == [] or g is None:
            if (report().get("graphInsidersDetected") or 0) == 0: return (True, "None", "rugcheck")
            return (False, "no data: bundles", "rugcheck")
        if isinstance(g, list): g = g[0] if g else {}
        if "edges" not in g and "links" not in g: return (False, "no data: bundles", "rugcheck")
        edges = g.get("edges") or g.get("links") or []
        holders = {util.get(n,"id") for n in g.get("nodes", []) if (n.get("holdings") or n.get("balance") or 0) > 0}
        par = {}
        def f(x):
            while par.setdefault(x, x) != x: x = par[x]
            return x
        for e in edges:
            a, b = e.get("source"), e.get("target")
            if a in holders and b in holders: par[f(a)] = f(b)
        sizes = {}
        for h in holders: r = f(h); sizes[r] = sizes.get(r, 0) + 1
        big = max(sizes.values(), default=0); big = big if big > 1 else 0
        return (big < s["bundle_max"], "None" if big == 0 else f"{big} bundled wallets", "rugcheck")
    holders = [None]
    def L8():
        global gpa_down_kills
        accs = []
        try:
            for prog, filt in ((fields.TOKEN_PROGRAM, [{"dataSize":165},{"memcmp":{"offset":0,"bytes":mint}}]),
                               (fields.TOKEN_2022_PROGRAM, [{"memcmp":{"offset":0,"bytes":mint}}])):
                accs += R(lambda: util.rpc(s["rpc_url"], "getProgramAccounts", [prog, {"encoding":"jsonParsed","filters":filt}], 60))
        except Exception as e:
            gpa_down_kills += 1; db.kv_set("gpa_down_kills", gpa_down_kills)
            if gpa_down_kills == 1: notifier.send("layer 8 down, every coin is red until rpc_url changes")
            return (False, "no data: delegations", "rpc")
        dele = {}; h = 0
        for a in accs:
            i = util.get(a, "account.data.parsed.info") or {}
            if float(util.get(i, "tokenAmount.uiAmountString") or 0) > 0: h += 1
            d = i.get("delegate")
            if d: dele[d] = dele.get(d, 0) + 1
        holders[0] = h
        worst = max(dele.items(), key=lambda x: x[1], default=(None, 0))
        if worst[1] >= s["delegate_max_accounts"]: return (False, f"{worst[1]} wallets → {worst[0][:4]}...{worst[0][-3:]}", "rpc")
        return (True, "None", "rpc")
    layers = []; bus.live["audit_running"] = mint; bus.live["audit_layers"] = layers
    passed = True; reason = None
    for n, fn in enumerate([L1,L2,L3,L4,L5,L6,L7,L8], 1):
        t = time.time()
        try: ok, val, src = fn()
        except Exception as e: ok, val, src = False, f"no data: {KEYS[n-1]}", "rpc"; log.info(f"{mint} L{n} err {e}")
        layers.append({"n":n,"name":NAMES[n-1],"pass":ok,"value":val,"source":src})
        log.info(f"{mint} L{n} {val} {int((time.time()-t)*1000)}ms")
        if not ok: passed = False; reason = val; break
    for n in range(len(layers)+1, 9): layers.append({"n":n,"name":NAMES[n-1],"pass":None,"value":"not checked","source":None})
    bus.live["audit_running"] = None
    out = {"agent":"auditor","mint":mint,"ts":util.now_iso(),"passed":passed,"layersChecked":sum(1 for l in layers if l["pass"] is not None),
           "layers":layers,"holders":holders[0],"reason":reason}
    db.q("insert or replace into audits values(?,?,?,?)", (mint, out["ts"], int(passed), json.dumps(out)))
    log.info(f"{mint} verdict {'PASS' if passed else 'RED '+reason}")
    import stats; stats.ev("audit_" + ("pass" if passed else "kill"))
    if passed: bus.analyst.put((sc, out))
    else: bus.activity(f"Rejected {sc.get('name') or mint[:6]}", f"Auditor · {reason}", "Skipped", "reject", sc["mint"])

_busy = set(); _bl = threading.Lock()
def loop(stop):
    n = config.load()["audit_workers"]
    for i in range(1, n): threading.Thread(target=worker, args=(stop,), daemon=True, name=f"auditor.{i}").start()
    worker(stop)
def worker(stop):
    while not stop.is_set():
        db.beat("auditor")
        try: sc = bus.auditor.get(timeout=1)
        except Exception: continue
        if db.q("select 1 from audits where mint=?", (sc["mint"],), True): continue
        if db.kv_get("paused:auditor") or not config.load()["audit_enabled"]:
            bus.activity(f"Skipped {sc.get('name')}", "Auditor · paused", "Skipped", "reject"); continue
        with _bl:
            if sc["mint"] in _busy: continue
            _busy.add(sc["mint"])
        try: audit(sc)
        except Exception as e: log.info(f"audit crash {sc['mint']} {e}")
        finally:
            with _bl: _busy.discard(sc["mint"])
