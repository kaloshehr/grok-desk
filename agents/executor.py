"""Agent 4 Executor: sizing + 1 push. Never places orders (FOMO has no public order API).
below_min: 'bump' -> ticket raised to min_ticket_usd if free covers it, else skip; 'skip' -> skip.
Recheck at push vs Scanner: liquidity drop > recheck_liq_drop_pct or price up > recheck_price_up_pct since Analyst -> skip.
LIVE: [Bought]/[Skip]; no tap within confirm_window_min -> 'missed'. PAPER fills after 1.2s."""
import json, time, db, util, bus, config, links, fees, notifier
log = util.logger("executor")
def raport(an):
    r = an.get("report")
    if not r: return ""
    return f"\n\nRaport: {r['verdict']}\n" + "\n".join("• " + b for b in r["bullets"])
def open_money(): return sum(r["size"] for r in db.q("select size from positions where status in ('open','exiting')"))
def stack(s):
    if s["mode"] == "PAPER": return db.kv_get("paper_balance", s["paper_balance"]) + open_money()
    return s["stack"]
def tier_size(s, equity, liq, v=None, mom=None, n_w=0, big=False, smart=0):
    """Conviction tiers: base / strong / very strong; capped by % of equity and % of pool liquidity."""
    base = s.get("tier_base_usd", 100); strong = s.get("tier_strong_usd", 250); top = s.get("tier_top_usd", 1000)
    m = (mom or {}).get("score") or 0; hard = (mom or {}).get("hard")
    if v == "DOBRY" and m >= s.get("tier_top_mom", 85) and not hard and (smart >= 1 or n_w >= 2): raw, tier = top, "very strong"
    elif (v == "DOBRY" and m >= s.get("tier_strong_mom", 75) and not hard) or n_w >= 2 or big: raw, tier = strong, "strong"
    else: raw, tier = base, "base"
    caps = {"equity%": equity * s.get("tier_max_equity_pct", 25)/100, "pool%": liq * s["size_liq_pct"]/100}
    size = min([raw] + list(caps.values())); cap = tier if size == raw else min(caps, key=caps.get)
    return round(size, 2), tier, cap
def prefix(s): return "PAPER · " if s["mode"] == "PAPER" else ""
def ticket_check(push=True):
    s = config.load(); st = stack(s); min_stack = s["min_size_usd"] / (s["size_pct"]/100)
    if st >= min_stack: return None
    pct = round(100 * s["min_size_usd"] / st, 1)
    msg = (f"tickets bumped to ${s['min_size_usd']:.2f} = {pct}% of stack (stack ${st:.2f} < ${min_stack:.2f})" if s["below_min"] == "bump"
           else f"stack ${st:.2f} < ${min_stack:.2f}: {s['size_pct']}% tickets are under ${s['min_size_usd']:.2f}, every signal will be skipped")
    if push: notifier.send(prefix(s) + msg)
    return msg
def run(sc, an):
    s = config.load(); mint = sc["mint"]
    if db.kv_get("paused:executor"): return skip({"mint": mint, "status": "sent"}, "executor paused", sc)
    if not db.kv_get("desk_running", True): return skip({"mint": mint, "status": "sent"}, "desk stopped", sc)
    if db.q("select 1 from positions where mint=?", (mint,), True) or db.q("select 1 from skips where mint=?", (mint,), True): return
    p = util.dex_tokens([mint], ttl=0).get(mint); price = float(p.get("priceUsd") or 0) if p else 0
    link = links.coin_url(mint, "solana")
    out = {"agent":"executor","mint":mint,"signalPrice":price,"sizeUsd":0,"stackPct":0,"link":link,"mode":s["mode"],"status":"sent","reason":None}
    if not price: return skip(out, "no price", sc)
    liq = util.get(p, "liquidity.usd") or 0
    if sc.get("liquidityUsd") and liq < sc["liquidityUsd"] * (1 - s["recheck_liq_drop_pct"]/100): return skip(out, f"liquidity -{100-100*liq/sc['liquidityUsd']:.0f}% since scan", sc)
    an_price = an.get("priceUsd")
    if an_price and price > an_price * (1 + s["recheck_price_up_pct"]/100): return skip(out, f"price +{100*price/an_price-100:.0f}% since score", sc)
    v = (an.get("report") or {}).get("verdict")
    # DOBRY buys; NIEPEWNY only with strong momentum (>= desk_niepewny_mom, no hard flag); ZŁY / no report never
    if s.get("desk_require_dobry", True) and v != "DOBRY" and not (v == "NIEPEWNY" and s.get("mom_enabled") and s.get("desk_niepewny_mom", 70)):
        return skip(out, f"raport {v or 'brak'} (desk buys DOBRY, NIEPEWNY only with momentum)", sc)
    if s.get("mom_enabled"):
        from agents import momentum; mo = momentum.check(mint, p, s, "desk"); an["momentum"] = mo
        if not mo["passed"]: return skip(out, f"momentum {mo['score']}" + (f" · {mo['hard']}" if mo["hard"] else ""), sc)
        if v == "NIEPEWNY" and (mo["score"] < s.get("desk_niepewny_mom", 70) or mo.get("hard")):
            return skip(out, f"raport NIEPEWNY + momentum {mo['score']} < {s.get('desk_niepewny_mom', 70)}", sc)
    st = stack(s); free = st - open_money()
    if s.get("desk_max_open") and db.q("select count(*) n from positions where strategy='desk' and status in ('open','exiting')", one=True)["n"] >= s["desk_max_open"]: return skip(out, f"max {s['desk_max_open']} desk positions", sc)
    liq_cap = liq * s["size_liq_pct"]/100
    if s.get("tier_enabled"):
        from agents import wallets as _wl
        try: smart = len(_wl.backers(mint))
        except Exception: smart = 0
        raw = None; size, tier, cap = tier_size(s, st, liq, v, an.get("momentum"), smart=smart); out.update(tier=tier, smartWallets=smart)
    else:
        raw = s.get("desk_ticket_usd") or st * s["size_pct"]/100
        size = min(raw, liq_cap); cap = "stack%" if size == raw else "pool%"
    if size < s["min_size_usd"]:
        if s["below_min"] == "bump" and liq_cap >= s["min_size_usd"] and free >= s["min_size_usd"]: size = s["min_size_usd"]; cap = "bumped"
        else: return skip(out, f"ticket ${size:.2f} < ${s['min_size_usd']:.2f}" + ("" if s["below_min"] == "skip" else " (can't bump)"), sc)
    if size > free: return skip(out, f"free ${free:.2f} < ticket", sc)
    import guard; g = guard.block(s, size + fees.fee(size), free)
    if g: return skip(out, g, sc)
    size = round(size, 2); pct = round(100*size/st, 1); out.update(sizeUsd=size, stackPct=pct)
    log.info(f"{mint} stack={st:.2f} raw={raw} cap={cap} size={size} mode={s['mode']} link={link}")
    bus.live["executing"] = {"mint": mint, "name": sc["name"], "size": size, "pct": pct, "t": time.time()}
    btns = [[{"text":"Buy on FOMO","url":link}]]
    if s["mode"] == "LIVE": btns.append([{"text":"Bought","callback_data":f"bought:{mint}"},{"text":"Skip","callback_data":f"skip:{mint}"}])
    ok = notifier.send(f"{prefix(s)}Buy {sc['name']} · ${size} · {pct}% of stack · score {an['score']}" + raport(an) + ("\n" + __import__("agents.momentum", fromlist=["x"]).line(an.get("momentum")) if an.get("momentum") else ""), btns)
    db.kv_set(f"pending:{mint}", {"sc": sc, "out": out, "price": price, "liq": liq, "t": time.time()})
    if s["mode"] == "PAPER":
        if not ok: log.info("push failed")
        time.sleep(1.2); fill(mint, price)
    elif not ok: log.info(f"{mint} push failed, no position")
def fill(mint, price=None):
    pend = db.kv_get(f"pending:{mint}")
    if not pend or db.q("select 1 from positions where mint=?", (mint,), True): return False
    s = config.load(); out = pend["out"]; sc = pend["sc"]; price = price or pend["price"]
    if s["mode"] == "PAPER":
        from agents.copytrade import impact as _imp
        liq = pend.get("liq") or sc.get("liquidityUsd") or 0
        if liq: imp = _imp(out["sizeUsd"], liq); out["quote"] = price; out["impactPct"] = round(100*imp, 2); price = price * (1 + imp + 0.005)  # buy-side slippage on the paper fill
        db.kv_set("paper_balance", db.kv_get("paper_balance", s["paper_balance"]) - out["sizeUsd"] - fees.fee(out["sizeUsd"]))
    db.q("insert into positions(mint,name,mode,entry,size,peak,stop_pct,opened_at,status,json) values(?,?,?,?,?,?,?,?,?,?)",
         (mint, sc["name"], s["mode"], price, out["sizeUsd"], price, s["stop_pct"], time.time(), "open", json.dumps(out)))
    out["status"] = "bought"; log.info(json.dumps(out)); bus.live["executing"] = None; db.q("delete from kv where k=?", (f"pending:{mint}",))
    bus.activity(f"Bought {sc['name']}", f"Executor · {time.strftime('%H:%M')}", f"${out['sizeUsd']} · {out['stackPct']}% of stack", "buy")
    return True
def skip(out, reason, sc=None, status="skipped"):
    out.update(status=status, reason=reason); log.info(json.dumps(out)); bus.live["executing"] = None
    db.q("insert or replace into skips values(?,?,?)", (out["mint"], util.now_iso(), reason))
    db.q("delete from kv where k=?", (f"pending:{out['mint']}",))
    bus.activity(f"{'Missed' if status=='missed' else 'Skipped'} {(sc or {}).get('name') or out['mint'][:6]}", f"Executor · {reason}", "Skipped", "reject", out["mint"])
def expire():
    s = config.load()
    for r in db.q("select k,v from kv where k like 'pending:%'"):
        p = json.loads(r["v"])
        if time.time() - p.get("t", 0) > s["exec_confirm_min"]*60: skip(p["out"], "no tap in time", p["sc"], "missed")
def loop(stop):
    while not stop.is_set():
        db.beat("executor")
        try: expire()
        except Exception as e: log.info(f"expire err {e}")
        try: sc, an = bus.executor.get(timeout=1)
        except Exception: continue
        try: run(sc, an)
        except Exception as e: log.info(f"crash {e}")
