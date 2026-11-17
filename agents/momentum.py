"""Agent 6 Momentum: last check before any buy (desk + copy). GeckoTerminal 1m candles + last ~300 trades
(DexScreener m5 txns as fallback). 0-100 score; hard fails = dump in progress / falling knife."""
import time, json, db, util, config, gt
log = util.logger("momentum")
db.q("create table if not exists momentum(mint text, ts real, score int, passed int, json text)")
def check(mint, p, s=None, who="desk"):
    s = s or config.load(); pool = (p or {}).get("pairAddress"); g = lambda k: util.get(p or {}, k) or 0
    sig = {}; pts = 0; why = []; hard = None
    try:
        c = (util.get(gt.get(f"/networks/solana/pools/{pool}/ohlcv/minute?aggregate=1&limit=30", 20), "data.attributes.ohlcv_list") or [])[::-1]
    except Exception as e: c = []; sig["candles_err"] = type(e).__name__
    try: tr = gt.trades(pool) if pool else []
    except Exception as e: tr = []; sig["trades_err"] = type(e).__name__
    now = time.time(); last = float(p.get("priceUsd") or 0) if p else 0
    if c: last = c[-1][4] or last
    def ago(m):  # close price ~m minutes ago
        old = [k for k in c if k[0] <= now - m*60]; return old[-1][4] if old else (c[0][1] if c else None)
    m5 = 100*(last/ago(5)-1) if c and ago(5) else g("priceChange.m5")
    m15 = 100*(last/ago(15)-1) if c and ago(15) else None
    sig.update(m5=round(m5,1), m15=None if m15 is None else round(m15,1))
    if m5 > 0: pts += 15
    else: why.append(f"5m {m5:+.0f}%")
    if m15 is None: pts += 7; why.append("15m n/a")
    elif m15 > 0: pts += 15
    else: why.append(f"15m {m15:+.0f}%")
    t5 = [t for t in tr if now - t["ts"] <= 300]; t10 = [t for t in tr if 300 < now - t["ts"] <= 600]
    if t5:
        b = [t for t in t5 if t["kind"] == "buy"]; sl = [t for t in t5 if t["kind"] != "buy"]
        bn, sn = len(b), len(sl); bv, sv = sum(t["usd"] for t in b), sum(t["usd"] for t in sl)
        ub, ub_prev = len({t["wallet"] for t in b}), len({t["wallet"] for t in t10 if t["kind"] == "buy"})
    else: bn, sn = g("txns.m5.buys"), g("txns.m5.sells"); bv = sv = None; ub = ub_prev = None
    sig.update(buys=bn, sells=sn, buy_usd=bv and round(bv), sell_usd=sv and round(sv), uniq_buyers=ub, uniq_buyers_prev=ub_prev)
    if bn > sn: pts += 15
    else: why.append(f"buys {bn} ≤ sells {sn}")
    if bv is None: pts += 7
    elif bv > sv: pts += 15
    else: why.append(f"buy vol ${bv:.0f} ≤ sell ${sv:.0f}")
    if ub is None: pts += 5
    elif ub > ub_prev: pts += 15
    elif ub == ub_prev and ub > 0: pts += 7; why.append("buyers flat")
    else: why.append(f"buyers {ub} < {ub_prev}")
    hi = max([k[2] for k in c[-30:]] or [last]) if c else last
    dd = 100*(1 - last/hi) if hi else 0; sig["from_high"] = round(-dd,1)
    if c:
        k = c[-1]; body = 100*(k[4]/k[1]-1) if k[1] else 0; wick = 100*(k[2]/max(k[1],k[4])-1) if max(k[1],k[4]) else 0
        drop3 = 100*(last/c[-4][4]-1) if len(c) >= 4 and c[-4][4] else 0
        sig.update(last_candle=round(body,1), wick=round(wick,1), drop3m=round(drop3,1))
        if body <= -s["mom_dump_pct"] or drop3 <= -s["mom_dump_pct"]: hard = f"dump in progress {min(body,drop3):.0f}%"
        elif wick >= s["mom_wick_pct"]: why.append(f"wick {wick:.0f}%")
        else: pts += 10
    else: pts += 5
    if dd > s["mom_max_from_high_pct"]: hard = hard or f"falling knife −{dd:.0f}% from 30m high"
    else: pts += 15
    score = min(100, pts); passed = hard is None and score >= s["mom_min_score"]
    out = {"mint": mint, "name": util.get(p or {}, "baseToken.symbol"), "who": who, "score": score, "passed": passed, "hard": hard, "why": why, "sig": sig, "ts": now}
    db.q("insert into momentum values(?,?,?,?,?)", (mint, now, score, int(passed), json.dumps(out))); db.beat("momentum", 0)
    log.info(f"{mint} {who} score={score} {'PASS' if passed else 'FAIL'} {hard or ''} {why} {sig}")
    return out
def line(m):
    if not m: return None
    return f"Momentum {m['score']}/100 · 5m {m['sig'].get('m5'):+}%" + (f" · 15m {m['sig']['m15']:+}%" if m['sig'].get('m15') is not None else "") + f" · kupno/sprzedaż {m['sig'].get('buys')}/{m['sig'].get('sells')}" + (f" · {m['hard']}" if m.get('hard') else "")
def recent(n=10): return [json.loads(r["json"]) for r in db.q("select json from momentum order by ts desc limit ?", (n,))]
def loop(stop):
    while not stop.is_set(): db.beat("momentum"); stop.wait(10)
