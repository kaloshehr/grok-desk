"""Agent 3 Analyst. rules mode (no Jev key) or jev mode."""
import json, time, hashlib, requests, db, util, bus, config
log = util.logger("analyst")
cl = lambda x: max(0.0, min(1.0, x))
def lay(a, n): return next((l["value"] for l in a["layers"] if l["n"] == n), "?")
def state(sc, a, p, s):
    g = lambda k: util.get(p, k) or 0
    t = (f"Coin {sc['name']} on Solana, graduated {sc.get('ageMin')} min ago. Liquidity ${g('liquidity.usd'):.0f}. Market cap ${g('marketCap'):.0f}.\n"
         f"Last 5 min: {g('txns.m5.buys')} buys, {g('txns.m5.sells')} sells, volume ${g('volume.m5'):.0f}, price {g('priceChange.m5')}%.\n"
         f"Last 1h: {g('txns.h1.buys')} buys, {g('txns.h1.sells')} sells, volume ${g('volume.h1'):.0f}, price {g('priceChange.h1')}%.\n"
         f"Top 10 hold {lay(a,4)}. Dev holds {lay(a,3)}. LP {lay(a,5)}. Bundled {lay(a,7)}. Delegations {lay(a,8)}.")
    return t[:s["state_max_chars"]]
def rules(p, a, s):
    g = lambda k: util.get(p, k) or 0
    bund = lay(a, 7); nb = int(bund.split()[0]) if bund[:1].isdigit() else 0
    org = cl(g("txns.h1.buys")/s["rules_buys_h1"]) * (s["rules_bundle_penalty"] if nb >= s["rules_bundle_min"] else 1)
    m5 = g("priceChange.m5")
    flush = 1.0 if (m5 <= -s["rules_flush_pct"] and g("txns.m5.buys") >= g("txns.m5.sells")) else (0.5 if m5 < 0 else 0.0)
    return {"alive_6h": cl(g("volume.h1")/s["rules_vol_h1"]), "organic": org, "flush": flush, "chase": cl(g("priceChange.h1")/s["rules_chase_h1"])}
Q = {"alive_6h":"This coin is still actively traded 6 hours from now","organic":"Most buyers in the last 30 minutes are separate real wallets",
     "flush":"Price just flushed down and buyers are absorbing it","chase":"This entry is chasing a green candle that already ran"}
def jev(st, s):
    key = config.secret("TYPESAFE_API_KEY")
    body = {"model":"jev-latest","state":st,"questions":{k:{"type":"noul","instructions":v} for k,v in Q.items()}}
    for i in range(2):
        try:
            r = requests.post("https://api.typesafe.ai/v1/systemone", json=body, headers={"Authorization":f"Bearer {key}"}, timeout=s["jev_timeout_s"])
            if r.status_code == 429: time.sleep(5); continue
            r.raise_for_status(); return r.json()
        except Exception as e: log.info(f"jev err {e}"); time.sleep(2)
    return None
def analyse(sc, a):
    s = config.load(); mint = sc["mint"]
    p = util.dex_tokens([mint]).get(mint) or {}; sc["_price"] = p.get("priceUsd")
    st = state(sc, a, p, s); model = "rules"; tok = 0
    if s["analyst_mode"] == "jev":
        r = jev(st, s)
        if not r: return finish(sc, None, ["no answer"], "jev-latest", 0, st)
        try: ans = {k: float(r["answers"][k]["noul"]) for k in Q}; assert all(0 <= v <= 1 for v in ans.values())
        except Exception: log.info(f"bad answer raw {r}"); return finish(sc, None, ["bad answer"], r.get("model"), 0, st)
        model = r.get("model"); tok = util.get(r, "usage.input_tokens") or util.get(r, "usage.inputTokens") or 0
    else: ans = rules(p, a, s)
    failed = []
    if ans["alive_6h"] < s["alive_min"]: failed.append(f"alive_6h {ans['alive_6h']:.2f}")
    if ans["organic"] < s["organic_min"]: failed.append(f"organic {ans['organic']:.2f}")
    if ans["chase"] > s["chase_max"]: failed.append(f"chase {ans['chase']:.2f}")
    score = round(100*(ans["alive_6h"]+ans["organic"]+ans["flush"]+(1-ans["chase"]))/4)
    ex = extras(sc, p, s, score); score = max(0, min(100, score + ex["adjust"]))
    if ex.get("kill"): failed.append(ex["kill"])
    if score < s["score_min"]: failed.append(f"score {score}")
    finish(sc, ans, failed, model, tok, st, score, ex)
def extras(sc, p, s, base):
    """Fake-volume flag, smart-wallet bonus, social check + Polish report. Each part fails soft."""
    import gt, volq, social, report
    from agents import wallets
    mint = sc["mint"]; ex = {"adjust": 0, "notes": []}
    vq, vsig = None, {}
    if s["vq_enabled"] and p.get("pairAddress"):
        try: vq, vsig = volq.quality(gt.trades(p["pairAddress"]), p)
        except Exception as e: vsig = {"note": f"trades unavailable ({type(e).__name__})"}
        if vq is not None:
            if vq < s["vq_kill"]: ex["kill"] = f"fake volume q{vq:.2f}"
            elif vq < s["vq_pen_below"]: ex["adjust"] -= s["vq_penalty"]; ex["notes"].append(f"volume q{vq:.2f} -{s['vq_penalty']}")
            bus.activity(f"Volume check {sc.get('name')}", f"Auditor-style flag · quality {vq:.2f}{' · FAKE' if vq < s['vq_kill'] else ''}", "Volume", "reject" if vq < s["vq_kill"] else "scan", mint)
    ex["volume"] = {"quality": vq, **vsig}
    bk = wallets.backers(mint) if s["w_enabled"] else []
    if bk:
        b = min(s["w_max_bonus"], s["w_bonus"] * len(bk)); ex["adjust"] += b; ex["notes"].append(f"{len(bk)} smart wallet(s) +{b}")
    ex["wallets"] = bk
    soc = None
    if s["soc_enabled"] and p.get("pairAddress"):
        try: soc = social.check(p)
        except Exception as e: soc = {"note": type(e).__name__}
    ex["social"] = soc
    rep = report.build(base + ex["adjust"], vq, vsig, soc, bk, s)
    if rep["verdict"] == "ZŁY" and s["soc_bad_penalty"]: ex["adjust"] -= s["soc_bad_penalty"]; ex["notes"].append(f"raport ZŁY -{s['soc_bad_penalty']}")
    ex["report"] = rep
    db.q("insert or replace into coin_extra values(?,?,?,?,?,?,?)", (mint, time.time(), vq, json.dumps(vsig), json.dumps(soc), json.dumps(rep), json.dumps(bk)))
    return ex
def finish(sc, ans, failed, model, tok, st, score=0, ex=None):
    passed = not failed; ex = ex or {}
    out = {"agent":"analyst","mint":sc["mint"],"priceUsd":float(sc.get("_price") or 0) or None,"answers":ans,"score":score,"passed":passed,"failed":failed,"model":model,"inputTokens":tok,
           "adjust": ex.get("adjust", 0), "adjustNotes": ex.get("notes", []), "volume": ex.get("volume"), "wallets": ex.get("wallets", []), "report": ex.get("report")}
    db.q("insert or replace into analyses values(?,?,?,?,?)", (sc["mint"], util.now_iso(), score, int(passed), json.dumps(out)))
    log.info(f"{sc['mint']} {hashlib.sha1(st.encode()).hexdigest()[:10]} {ans} {score} {'PASS' if passed else failed} {tok}")
    if passed: bus.executor.put((sc, out))
    else: bus.activity(f"Rejected {sc.get('name')}", f"Analyst{' (rules)' if model=='rules' else ''} · {failed[0]}", "Skipped", "reject", sc["mint"])
def loop(stop):
    while not stop.is_set():
        db.beat("analyst")
        try: sc, a = bus.analyst.get(timeout=1)
        except Exception: continue
        if db.q("select 1 from analyses where mint=?", (sc["mint"],), True): continue
        if db.kv_get("paused:analyst") or not db.kv_get("desk_running", True): bus.activity(f"Skipped {sc.get('name')}", "Analyst · paused", "Skipped", "reject"); continue
        try: analyse(sc, a)
        except Exception as e: log.info(f"crash {sc['mint']} {e}")
