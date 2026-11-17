"""Agent 5 Risk: exits. Polls DexScreener (one batched call per 30 open mints) every risk.poll_s (2.5s) for open positions only."""
import time, json, db, util, config, links, fees, notifier, bus
log = util.logger("risk")
def close(pos, price, reason, s):
    pnl_gross = pos["size"] * (price/pos["entry"] - 1); keys0 = pos.keys(); tpd = ("tp_done" in keys0 and pos["tp_done"])
    fe = (0 if tpd else fees.fee(pos["size"])) + fees.fee(pos["size"] + pnl_gross)  # buy fee already booked in part_pnl after a partial
    keys = pos.keys(); part = (pos["part_pnl"] or 0) if "part_pnl" in keys else 0; base = (pos["orig_size"] or pos["size"]) if "orig_size" in keys else pos["size"]
    copy = "strategy" in keys and pos["strategy"] == "copy"
    pnl = round(pnl_gross - fe + part, 2); pct = round(100*pnl/base, 1); link = links.coin_url(pos["mint"])
    status = "closed" if pos["mode"] == "PAPER" else "exiting"
    db.q("update positions set status=?,exit_price=?,exit_reason=?,closed_at=?,pnl=?,fees=?,last_push=? where id=?",
         (status, price, reason, time.time(), pnl, round(fe,2), time.time(), pos["id"]))
    if pos["mode"] == "PAPER": db.kv_set("paper_balance", db.kv_get("paper_balance", s["paper_balance"]) + pos["size"] + pnl_gross - fees.fee(pos["size"] + pnl_gross))
    out = {"agent":"risk","mint":pos["mint"],"name":pos["name"],"ts":util.now_iso(),"reason":reason,"entryPrice":pos["entry"],
           "exitPrice":price,"peakPrice":pos["peak"],"pnlUsd":pnl,"pct":pct,"feesUsd":round(fe,2),"stopPct":pos["stop_pct"],
           "mode":pos["mode"],"status":status,"link":link}
    log.info(json.dumps(out))
    msg = {"trail": f"Sold {pos['name']} · {pnl:+.2f}$ ({pct:+.1f}%)", "stop": f"{pos['name']} hit the -{pos['stop_pct']}% stop · -${abs(pnl):.2f}",
           "timeout": f"Sell {pos['name']}, dead money · {pct:+.1f}% in {s['timeout_h']}h", "pulled": f"{pos['name']}: liquidity pulled / -{s.get('death_liq_drop_pct')}% since entry, sold · {pnl:+.2f}$", "tp": f"Take profit {pos['name']} · {pnl:+.2f}$ ({pct:+.1f}%)", "follow": f"Copied wallet sold {pos['name']} → sold · {pnl:+.2f}$ ({pct:+.1f}%)", "manual": f"Closed {pos['name']} · {pnl:+.2f}$ ({pct:+.1f}%)", "breakeven": f"Break-even stop {pos['name']} · {pnl:+.2f}$ ({pct:+.1f}%)", "flat": f"Time stop {pos['name']}, flat after {s.get('flat_min')} min · {pnl:+.2f}$ ({pct:+.1f}%)"}.get(reason, f"Closed {pos['name']} ({reason}) · {pnl:+.2f}$")
    btn = [[{"text":"Sell on FOMO","url":link}]]
    if pos["mode"] == "LIVE": btn.append([{"text":"Sold","callback_data":f"sold:{pos['mint']}"}])
    notifier.send(("PAPER · " if pos["mode"]=="PAPER" else "") + ("Kopia · " if copy else "") + msg, btn)
    if reason == "stop": bus.activity(f"Stopped {pos['name']}", f"Risk · -{pos['stop_pct']}% stop", f"-${abs(pnl):.2f}", "stop")
    else: bus.activity(f"Sold {pos['name']}", f"{'Kopia' if copy else 'Risk'} · {'trailing stop' if reason=='trail' else reason}", f"{pnl:+.2f}$", f"{pct:+.1f}%")
def partial(pos, price, s):
    """copy two_step: sell 50% at tp_pct, keep the rest for tp2/trail."""
    db.kv_set(f"tp:{pos['id']}", [time.time(), price])
    half = pos["size"]/2; g = half*(price/pos["entry"]-1); fe = fees.fee(half+g)
    db.q("update positions set size=?,tp_done=1,part_pnl=?,trail_active=1 where id=?", (round(pos["size"]-half,2), round(g-fe-fees.fee(pos["orig_size"] or pos["size"]),2), pos["id"]))
    if pos["mode"] == "PAPER": db.kv_set("paper_balance", db.kv_get("paper_balance", s["paper_balance"]) + half + g - fe)
    db.q("update positions set orig_size=coalesce(orig_size,?) where id=?", (pos["size"], pos["id"]))
    who = "Kopia" if pos["strategy"] == "copy" else "Desk"; rest = (f"trail {s['trail_pct']}%" + (" + break-even" if s.get("breakeven_after_tp") else "")) if (pos["strategy"] != "copy" or s.get("copy_trail_after_tp")) else f"+{s['copy_tp2_pct']}% / trail"
    notifier.send(("PAPER · " if pos["mode"] == "PAPER" else "") + f"{who} · sold 50% {pos['name']} at +{100*(price/pos['entry']-1):.1f}%, rest: {rest}", [[{"text":"Sell on FOMO","url":links.coin_url(pos['mint'])}]])
    bus.activity(f"Sold 50% {pos['name']}", f"{who} · take profit 1", f"{g-fe:+.2f}$", f"{100*(price/pos['entry']-1):+.1f}%", pos["mint"])
_lastp = {}; _lastlog = {}
def tick(s):
    open_ = db.q("select * from positions where status='open'")
    for pos in db.q("select * from positions where status='exiting' and mode='LIVE'"):
        if time.time() - (pos["last_push"] or 0) > s["sold_reping_min"]*60 and not db.kv_get(f"reping:{pos['mint']}"):
            notifier.send(f"{pos['name']} still open, tap Sold when done", [[{"text":"Sell on FOMO","url":links.coin_url(pos["mint"])}],[{"text":"Sold","callback_data":f"sold:{pos['mint']}"}]])
            db.kv_set(f"reping:{pos['mint']}", 1)
    if not open_: return
    pairs = util.dex_tokens(list({p["mint"] for p in open_}), ttl=0, curve=True); db.kv_set("risk_last_tick", time.time())
    for pos in open_:
        p = pairs.get(pos["mint"]); price = float(p.get("priceUsd") or 0) if p else 0
        stop_pct = s["stop_pct"]  # live setting applies to open positions too
        if stop_pct != pos["stop_pct"]: db.q("update positions set stop_pct=? where id=?", (stop_pct, pos["id"]))
        if not price:
            n = pos["no_price"] + 1; db.q("update positions set no_price=? where id=?", (n, pos["id"]))
            if n == max(1, round(s["no_price_polls"] * 15 / s["risk_poll_s"])): notifier.send(("PAPER · " if pos["mode"]=="PAPER" else "")+f"{pos['name']}: no price for 1 min", [[{"text":"Sell on FOMO","url":links.coin_url(pos["mint"])}]])
            log.info(f"{pos['mint']} no price ({n})"); continue
        if time.time() - _lastp.get(pos["mint"], 0) >= 10:
            _lastp[pos["mint"]] = time.time(); db.q("insert into prices values(?,?,?,?)", (pos["mint"], time.time(), price, util.get(p,"volume.m5")))
        peak = max(pos["peak"], price); trail = pos["trail_active"] or price >= pos["entry"]*(1+s["trail_on_pct"]/100)
        db.q("update positions set peak=?,trail_active=?,no_price=0 where id=?", (peak, int(trail), pos["id"]))
        pos = db.q("select * from positions where id=?", (pos["id"],), True)
        stop_line = pos["entry"]*(1-stop_pct/100); trail_line = peak*(1-s["trail_pct"]/100) if trail else None
        act = None
        copy = pos["strategy"] == "copy"; gain = 100*(price/pos["entry"]-1)
        liq = util.get(p,"liquidity.usd") or 0.2*(p.get("marketCap") or p.get("fdv") or 0); sell_px = price*(1 - min(0.25, pos["size"]/max(liq/2,1)) - 0.005)  # sell-side impact on the paper fill (desk + copy)
        if copy:
            if s["copy_two_step"] and not pos["tp_done"] and gain >= s["copy_tp_pct"]: partial(pos, sell_px, s); continue
            tgt = s["copy_tp2_pct"] if s["copy_two_step"] else s["copy_tp_pct"]
            if not (s["copy_two_step"] and s.get("copy_trail_after_tp")) and gain >= tgt: close(pos, sell_px, "tp", s); continue
        elif s.get("tp1_pct") and not pos["tp_done"] and gain >= s["tp1_pct"]: partial(pos, sell_px if s["mode"] == "PAPER" else price, s); continue
        if pos["tp_done"] and s.get("breakeven_after_tp"): stop_line = max(stop_line, pos["entry"])
        liq_now = util.get(p,"liquidity.usd") or 0; el = db.kv_get(f"entry_liq:{pos['id']}")
        if el is None and liq_now > 0: el = liq_now; db.kv_set(f"entry_liq:{pos['id']}", el)
        dd = s.get("death_liq_drop_pct") or 0
        if dd and el and 0 < liq_now < el*(1-dd/100) and not (copy and p.get("dexId") == "pumpfun"): act = "pulled"
        elif (util.get(p,"liquidity.usd") or 0) <= 0 and not (copy and p.get("dexId") == "pumpfun"): act = "pulled"
        elif price <= stop_line: act = "breakeven" if pos["tp_done"] and stop_line >= pos["entry"] else "stop"
        elif trail and price <= trail_line: act = "trail"
        elif not copy and s.get("flat_min") and not pos["tp_done"] and time.time()-pos["opened_at"] >= s["flat_min"]*60 and abs(gain) <= s["flat_pct"]: act = "flat"
        elif time.time()-pos["opened_at"] >= s["timeout_h"]*3600 and price < pos["entry"]*(1+s["timeout_min_gain_pct"]/100): act = "timeout"
        if act or time.time() - _lastlog.get(pos["id"], 0) >= 60:
            _lastlog[pos["id"]] = time.time(); log.info(f"{pos['mint']} price={price} peak={peak} stop={stop_line} trail={trail_line} action={act}")
        if act: close(pos, sell_px if s["mode"] == "PAPER" and act != "pulled" else price, act, s)
def loop(stop):
    while not stop.is_set():
        db.beat("risk")
        s = config.load()
        if db.kv_get("paused:risk") and not db.q("select 1 from positions where status in ('open','exiting')", one=True): stop.wait(5); continue
        try: tick(s)
        except util.Http429: log.info("429 back off, polls missed"); stop.wait(s["risk_429_wait_s"]); continue
        except Exception as e: log.info(f"err {e}")
        stop.wait(s["risk_poll_s"])
