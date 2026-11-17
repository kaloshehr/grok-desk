"""Early gems (PAPER, read-only). Never places orders or touches funds.

Finds currently cheap coins (MC roughly $5k–$150k) with early-breakout signals:
accelerating volume, buy-heavy prints, unique-buyer growth, fast bonding-curve
progress or a fresh graduation, healthy holder distribution, mint/freeze revoked,
and a bonus if a known winners-cluster is involved. High scores get a Telegram
alert and enter the normal Scanner -> Auditor -> Analyst -> Executor -> Risk
PAPER path (ZŁY is still never bought).
"""
import json, time, db, util, config, notifier, links
log = util.logger("gems")
H = {"Accept": "application/json", "User-Agent": "desk/1.0"}
_alerted = {}

def gem_score(sig):
    """Pure scoring (no I/O) so tests can cover it. `sig` is a dict of already-gathered signals."""
    mc = sig.get("mc") or 0
    lo, hi = sig.get("min_mc", 5000), sig.get("max_mc", 150000)
    if mc < lo or mc > hi: return 0, ["mc out of range"]
    sc, why = 15, []  # in-range base
    acc = sig.get("vol_acc")  # m5*12 / h1
    if acc is not None:
        if acc >= 3: sc += 25; why.append(f"vol accel {acc:.1f}x")
        elif acc >= 1.5: sc += 15; why.append(f"vol accel {acc:.1f}x")
        elif acc < 0.6: sc -= 8
    bs = sig.get("buy_share")  # buys / (buys+sells) over m5
    if bs is not None:
        if bs >= 0.75: sc += 20; why.append(f"buys {100*bs:.0f}%")
        elif bs >= 0.6: sc += 10; why.append(f"buys {100*bs:.0f}%")
        elif bs < 0.4: sc -= 10; why.append("sell pressure")
    m5, h1 = sig.get("chg_m5"), sig.get("chg_h1")
    if m5 is not None and m5 >= 5 and (h1 is None or h1 < 150):
        sc += 10; why.append(f"+{m5:.0f}% 5m early")
    if h1 is not None and h1 >= 400: sc -= 15; why.append("already chased")
    liq, liq_mc = sig.get("liq") or 0, (sig.get("liq") or 0) / mc if mc else 0
    if liq >= 5000: sc += 5
    if liq_mc >= 0.15: sc += 5; why.append("healthy liq")
    if liq and liq < 1000: sc -= 10
    uniq = sig.get("unique_growth")
    if uniq is not None and uniq >= 1.5: sc += 10; why.append("unique buyers growing")
    prog = sig.get("curve_prog")
    if prog is not None and 40 <= prog <= 95: sc += 15; why.append(f"curve {prog:.0f}%")
    if sig.get("just_graduated"): sc += 15; why.append("just graduated")
    if sig.get("mint_revoked"): sc += 8; why.append("mint revoked")
    elif sig.get("mint_revoked") is False: sc -= 20; why.append("mint live")
    if sig.get("freeze_revoked"): sc += 8; why.append("freeze revoked")
    elif sig.get("freeze_revoked") is False: sc -= 12; why.append("freeze live")
    top10, dev = sig.get("top10"), sig.get("dev_pct")
    if dev is not None and dev <= 5: sc += 8
    if dev is not None and dev >= 8:
        sc -= 25; why.append(f"dev {dev:.0f}%")
        if dev >= 25: sc = min(sc, 45)
    if top10 is not None and (dev or 0) < 8 and top10 <= 35: sc += 8; why.append(f"top10 {top10:.0f}%")
    if top10 is not None and top10 >= 50: sc -= 12; why.append(f"top10 {top10:.0f}%")
    if sig.get("bundles") is not None and sig["bundles"] <= 2: sc += 6
    if sig.get("bundles") is not None and sig["bundles"] >= 6: sc -= 12; why.append("bundles")
    if sig.get("cluster_score"):
        sc += 15; why.append(f"cluster {sig['cluster_score']:.0f}")
    sc = max(0, min(100, round(sc, 1)))
    return sc, why

def _vol_acc(p):
    m5 = util.get(p, "volume.m5") or 0
    h1 = util.get(p, "volume.h1") or 0
    if not m5: return None
    return (m5 * 12) / max(h1, 1)

def _buy_share(p, win="m5"):
    b = util.get(p, f"txns.{win}.buys") or 0
    s = util.get(p, f"txns.{win}.sells") or 0
    if b + s <= 0: return None
    return b / (b + s)

def _unique_growth(trades):
    if not trades or len(trades) < 8: return None
    mid = trades[0]["ts"] - (trades[0]["ts"] - trades[-1]["ts"]) / 2 if trades[-1]["ts"] else 0
    if not mid: return None
    early = {t["wallet"] for t in trades if t.get("kind") == "buy" and t.get("ts") and t["ts"] < mid and t.get("wallet")}
    late = {t["wallet"] for t in trades if t.get("kind") == "buy" and t.get("ts") and t["ts"] >= mid and t.get("wallet")}
    if not early: return None
    return len(late) / max(len(early), 1)

def _authorities(mint):
    try:
        a = util.research_rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}], ttl=180)
        info = util.get(a, "value.data.parsed.info") or {}
        return info.get("mintAuthority") is None, info.get("freezeAuthority") is None
    except Exception:
        return None, None

def _holders(mint):
    """Cheap holder view from rugcheck (cached). Returns top10_pct, dev_pct, bundles or Nones."""
    try:
        rep = util.rugcheck(mint, ttl=300)
    except Exception:
        return None, None, None
    top = rep.get("topHolders") or []
    # rugcheck topHolders usually include pct / insider flags
    pcts = []
    dev = None
    bundles = 0
    creator = rep.get("creator")
    for h in top[:12]:
        if not isinstance(h, dict): continue
        p = h.get("pct") if h.get("pct") is not None else h.get("percentage")
        try: p = float(p)
        except Exception: p = None
        if p is None: continue
        ins = h.get("insider") or h.get("bundled")
        if ins: bundles += 1
        owner = h.get("owner") or h.get("address")
        if creator and owner == creator: dev = p
        pcts.append(p)
    top10 = sum(pcts[:10]) if pcts else None
    return top10, dev, bundles or None

def _cluster_bonus(mint, creator):
    try:
        from agents import winners
    except Exception:
        return None
    addrs = [creator] if creator else []
    for r in db.q("select addr from winner_roles where mint=? limit 30", (mint,)):
        addrs.append(r["addr"])
    s = config.load()
    hits = winners.known_good_cluster([a for a in addrs if a], s)
    if not hits: return None
    return max(h.get("use_score") or 0 for h in hits)

def signals_from_pair(p, s, extra=None):
    extra = extra or {}
    mc = float(p.get("marketCap") or p.get("fdv") or 0)
    sig = {
        "mc": mc, "min_mc": s["gems_min_mc_usd"], "max_mc": s["gems_max_mc_usd"],
        "vol_acc": _vol_acc(p), "buy_share": _buy_share(p),
        "chg_m5": util.get(p, "priceChange.m5"), "chg_h1": util.get(p, "priceChange.h1"),
        "liq": util.get(p, "liquidity.usd") or 0,
    }
    sig.update(extra)
    return sig

def evaluate(mint, p, s, extra=None):
    sig = signals_from_pair(p, s, extra)
    # authorities / holders only if the cheap market signals already look decent
    pre, _ = gem_score({**sig, "mint_revoked": True, "freeze_revoked": True})  # optimistic peek
    if pre >= 45 and not sig.get("skip_deep"):
        mint_r, freeze_r = _authorities(mint)
        sig["mint_revoked"], sig["freeze_revoked"] = mint_r, freeze_r
        top10, dev, bundles = _holders(mint)
        sig["top10"], sig["dev_pct"], sig["bundles"] = top10, dev, bundles
        creator = (extra or {}).get("creator")
        if not creator:
            c = db.q("select creator from coins where mint=?", (mint,), True)
            creator = c["creator"] if c else None
        bonus = _cluster_bonus(mint, creator)
        if bonus: sig["cluster_score"] = bonus
    sc, why = gem_score(sig)
    return sc, why, sig

def _candidates(s):
    mints = []
    # bonding-curve coins the prefilter is already watching
    try:
        from agents import prefilter
        for mint, v in prefilter.watched().items():
            mints.append((mint, {"curve_prog": v.get("prog"), "creator": None, "name": v.get("name"), "just_graduated": False}))
    except Exception:
        pass
    # recent new / trending pools from sources the desk already uses
    try:
        from agents import trend
        for src, fn in (("gecko_new", trend.gecko_new), ("gecko", trend.gecko)):
            try:
                for m in fn()[:40]:
                    mints.append((m, {}))
            except Exception as e:
                log.info(f"{src} {type(e).__name__}")
    except Exception:
        pass
    # waiting-room / recent coins in the cheap MC band later
    for r in db.q("select mint,name,creator from coins where status in ('waiting','curve') order by found_at desc limit 40"):
        mints.append((r["mint"], {"name": r["name"], "creator": r["creator"]}))
    # unique, first extra wins
    out, seen = [], set()
    for m, extra in mints:
        if not m or m in seen: continue
        seen.add(m); out.append((m, extra))
    return out[:80]

def _alert(name, mint, sc, why, mc):
    key = mint
    if time.time() - _alerted.get(key, 0) < 6 * 3600: return
    _alerted[key] = time.time()
    notifier.send(f"PAPER · Early gem: {name or mint[:6]} · score {sc:.0f} · MC ${mc:,.0f}\n" + " · ".join(why[:6]) + f"\n{links.coin_url(mint)}",
                  notifier.fomo_btn(mint))
    try:
        import bus; bus.activity(f"Early gem: {name or mint[:6]}", " · ".join(why[:4]), f"score {sc:.0f}", "info", mint)
    except Exception:
        pass

def consider(mint, s, extra=None, p=None):
    extra = extra or {}
    try:
        if p is None: p = (util.dex_tokens([mint], ttl=20, curve=True) or {}).get(mint)
    except Exception as e:
        log.info(f"dex {mint} {type(e).__name__}"); return None
    if not p: return None
    created = (p.get("pairCreatedAt") or 0) / 1000
    if created and time.time() - created < 20 * 60 and p.get("dexId") != "pumpfun":
        extra = {**extra, "just_graduated": extra.get("just_graduated") or True}
    sc, why, sig = evaluate(mint, p, s, extra)
    name = extra.get("name") or util.get(p, "baseToken.name")
    mc = sig.get("mc") or 0
    db.q("insert into gem_scores(mint,ts,score,mc,name,reasons,json) values(?,?,?,?,?,?,?) on conflict(mint) do update set ts=excluded.ts,score=excluded.score,mc=excluded.mc,name=excluded.name,reasons=excluded.reasons,json=excluded.json",
         (mint, time.time(), sc, mc, name, " · ".join(why), json.dumps({k: sig[k] for k in sig if k not in ("pair",)})))
    red = (sig.get("dev_pct") is not None and sig["dev_pct"] >= 8) or (sig.get("mint_revoked") is False)
    if sc >= s["gems_alert_min_score"] and not red:
        _alert(name, mint, sc, why, mc)
    if sc >= s["gems_min_score"] and not red:
        from agents import winners
        winners._feed(mint, "gems", name)
    return sc

def on_new_token(m):
    # cheap hook: if the create already looks crowded (high initial buy) we skip; otherwise wait for the loop
    return

def text():
    rows = db.q("select * from gem_scores where ts>? order by score desc limit 10", (time.time() - 6 * 3600,))
    if not rows: return "Early gems · none scored in the last 6h yet."
    lines = [f"Early gems · {len(rows)} in last 6h (showing top {min(10,len(rows))})"]
    for r in rows:
        lines.append(f"{r['score']:.0f}  {r['name'] or r['mint'][:6]}  MC ${r['mc'] or 0:,.0f}  {r['reasons'] or ''}")
    return "\n".join(lines)

def loop(stop):
    first = True
    while not stop.is_set():
        db.beat("gems")
        s = config.load()
        if not s.get("gems_enabled", True) or not db.kv_get("desk_running", True):
            stop.wait(10); continue
        try:
            log.info("scoring cheap early-breakout coins")
            cands = _candidates(s)
            # batch dex lookups
            need = [m for m, _ in cands]
            try: pairs = util.dex_tokens(need, ttl=20, curve=True)
            except util.Http429:
                log.info("dex 429"); stop.wait(30); continue
            except Exception as e:
                log.info(f"dex {type(e).__name__}"); pairs = {}
            scored = 0
            cap = 25 if first else 40
            deep = 0
            for mint, extra in cands[:cap]:
                db.beat("gems")
                p = pairs.get(mint)
                if not p: continue
                mc = float(p.get("marketCap") or p.get("fdv") or 0)
                if mc and (mc < s["gems_min_mc_usd"] or mc > s["gems_max_mc_usd"]): continue
                if not mc and p.get("dexId") != "pumpfun": continue
                if deep >= 5: extra = {**extra, "skip_deep": True}
                try:
                    sc = consider(mint, s, extra, p)
                    if sc is not None:
                        scored += 1
                        if sc >= 45 and not extra.get("skip_deep"): deep += 1
                except Exception as e:
                    log.info(f"score {mint} {type(e).__name__}: {config.redact(e)}")
            log.info(f"cycle looked={len(cands)} scored={scored}")
        except Exception as e:
            log.info(f"cycle err {type(e).__name__}: {config.redact(e)}")
        first = False
        stop.wait(s.get("gems_poll_s", 120))
