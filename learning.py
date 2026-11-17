"""Learning mode: looks at closed PAPER trades vs the features they had at entry and PROPOSES setting tweaks.
Never applies anything by itself: the owner sends /apply_learn. Hard safe bounds below; rug layers (auditor.*),
mode and anything not in SAFE can never be proposed. Stop can never go below 6%."""
import json, time, statistics, db, util, config
log = util.logger("learning")
SAFE = {("analyst", "min_score"): (60, 85), ("scanner", "min_liquidity_usd"): (15000, 100000),
        ("volume", "penalty_below"): (0.3, 0.8), ("volume", "kill_below"): (0.2, 0.5),
        ("wallets", "bonus"): (0, 20), ("risk", "stop_pct"): (6, 15)}
def _cur(sec, key): return config.nested()[sec][key]
def rows():
    out = []
    for p in db.q("select p.mint,p.pnl,p.size,p.exit_reason,a.json aj,c.scan_liq from positions p left join analyses a on a.mint=p.mint left join coins c on c.mint=p.mint where p.status='closed' and p.pnl is not null"):
        a = json.loads(p["aj"]) if p["aj"] else {}
        out.append({"mint": p["mint"], "win": p["pnl"] > 0, "pnl": p["pnl"], "score": a.get("score"), "liq": p["scan_liq"],
                    "vq": (a.get("volume") or {}).get("quality"), "wallets": len(a.get("wallets") or []),
                    "verdict": (a.get("report") or {}).get("verdict"), "exit": p["exit_reason"]})
    return out
wr = lambda xs: sum(x["win"] for x in xs) / len(xs) if xs else None
def analyse():
    s = config.load(); R = rows(); n = len(R); props = []
    st = {"closed": n, "win_rate": round(wr(R), 2) if R else None, "pnl": round(sum(x["pnl"] for x in R), 2)}
    def prop(sec, key, new, why):
        lo, hi = SAFE[(sec, key)]; old = _cur(sec, key); new = max(lo, min(hi, new))
        if isinstance(old, int): new = int(round(new))
        else: new = round(new, 2)
        if new != old: props.append({"section": sec, "key": key, "old": old, "new": new, "why": why})
    if n < s["learn_min"]: return {"ts": time.time(), "stats": st, "proposals": [], "note": f"za mało zamkniętych transakcji ({n}/{s['learn_min']})"}
    W = [x for x in R if x["win"]]; L = [x for x in R if not x["win"]]
    ms = s["score_min"]; lowg = [x for x in R if x["score"] is not None and x["score"] < ms + 5]; hig = [x for x in R if x["score"] is not None and x["score"] >= ms + 5]
    if len(lowg) >= 2 and wr(lowg) < 0.4 and (not hig or wr(hig) > wr(lowg)):
        prop("analyst", "min_score", ms + 5, f"transakcje z wynikiem {ms}-{ms+4} wygrały tylko {int(100*wr(lowg))}% ({len(lowg)} szt.)")
    elif wr(R) >= 0.6 and n >= 2 * s["learn_min"]:
        prop("analyst", "min_score", ms - 3, f"win rate {int(100*wr(R))}% przy {n} transakcjach - można wpuścić trochę więcej coinów")
    lw = [x["liq"] for x in W if x["liq"]]; ll = [x["liq"] for x in L if x["liq"]]
    if len(lw) >= 2 and len(ll) >= 2 and statistics.median(ll) < 0.7 * statistics.median(lw):
        cur = _cur("scanner", "min_liquidity_usd"); new = round(statistics.median(ll) / 1000) * 1000
        if new > cur: prop("scanner", "min_liquidity_usd", new, f"przegrane miały medianę płynności ${statistics.median(ll):,.0f} vs wygrane ${statistics.median(lw):,.0f}")
    vw = [x["vq"] for x in W if x["vq"] is not None]; vl = [x["vq"] for x in L if x["vq"] is not None]
    if len(vw) >= 2 and len(vl) >= 2 and statistics.mean(vl) < statistics.mean(vw) - 0.1:
        prop("volume", "penalty_below", s["vq_pen_below"] + 0.05, f"przegrane miały gorszą jakość wolumenu ({statistics.mean(vl):.2f} vs {statistics.mean(vw):.2f})")
    wb = [x for x in R if x["wallets"]]; nb = [x for x in R if not x["wallets"]]
    if len(wb) >= 2 and len(nb) >= 2:
        d = wr(wb) - wr(nb)
        if d > 0.2: prop("wallets", "bonus", s["w_bonus"] + 3, f"coiny kupione przez smart-portfele wygrywały {int(100*wr(wb))}% vs {int(100*wr(nb))}%")
        elif d < -0.2: prop("wallets", "bonus", s["w_bonus"] - 3, f"smart-portfele nie pomagały ({int(100*wr(wb))}% vs {int(100*wr(nb))}%)")
    stops = [x for x in L if (x["exit"] or "").startswith("stop")]
    if len(stops) >= 3 and len(stops) / n > 0.6:
        prop("risk", "stop_pct", s["stop_pct"] - 1, f"{len(stops)} z {n} transakcji zamknął stop - mniejsze straty na wejściu (nigdy poniżej 6%)")
    return {"ts": time.time(), "stats": st, "proposals": props}
def text(r):
    t = f"PAPER · Nauka: {r['stats']['closed']} zamkniętych, win rate {int(100*(r['stats']['win_rate'] or 0))}%, PnL ${r['stats']['pnl']}\n"
    if not r["proposals"]: return t + (r.get("note") or "Brak propozycji - ustawienia zostają.")
    return t + "\n".join(f"• {p['section']}.{p['key']}: {p['old']} → {p['new']} ({p['why']})" for p in r["proposals"]) + "\n\nWyślij /apply_learn żeby zastosować. Nic nie zmieniam sam."
def run(send=True):
    r = analyse(); db.kv_set("learn_last", r); db.kv_set("learn_last_n", r["stats"]["closed"])
    log.info(f"learn {r['stats']} props={len(r['proposals'])}")
    if send and r["proposals"]:
        import notifier; notifier.send(text(r), kind="report")
    return r
def apply():
    r = db.kv_get("learn_last") or {}; done = []
    for p in r.get("proposals") or []:
        k = (p["section"], p["key"])
        if k not in SAFE or p["section"] == "auditor": continue
        lo, hi = SAFE[k]
        if not lo <= p["new"] <= hi: continue
        try: config.set_value(p["section"], p["key"], p["new"], "learning"); done.append(f"{p['section']}.{p['key']} → {p['new']}")
        except Exception as e: done.append(f"{p['section']}.{p['key']}: {e}")
    if done: r["proposals"] = []; r["applied"] = {"ts": time.time(), "items": done}; db.kv_set("learn_last", r)
    return "PAPER · Zastosowano: " + ", ".join(done) if done else "Brak propozycji do zastosowania."
def loop(stop):
    while not stop.is_set():
        db.beat("learning")
        try:
            s = config.load()
            if s["learn_enabled"]:
                n = db.q("select count(*) n from positions where status='closed'", one=True)["n"]
                day = time.strftime("%Y-%m-%d"); nightly = time.localtime().tm_hour == 3 and db.kv_get("learn_day") != day
                last_n = db.kv_get("learn_last_n") or 0; sent_n = db.kv_get("learn_sent_n") or 0
                first = last_n < s["learn_min"] <= n  # first time the minimum is reached
                if nightly or first or n - sent_n >= s["learn_every"] or db.kv_get("learn_last") is None:
                    if nightly: db.kv_set("learn_day", day)
                    run(send=db.kv_get("learn_last") is not None); db.kv_set("learn_sent_n", n)
                elif n != last_n:  # keep the app counter current without a Telegram message
                    run(send=False)
        except Exception as e: log.info(f"crash {type(e).__name__}: {e}")
        stop.wait(60)
