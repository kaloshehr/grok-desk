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

UNSAFE_DEX = {"meteora", "meteoradbc"}
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

def is_pump_origin(mint, p=None, launchpad=None):
    if (mint or "").endswith("pump"): return True
    if (p or {}).get("dexId") == "pumpfun": return True
    if isinstance(launchpad, dict):
        launchpad = " ".join(str(launchpad.get(k) or "") for k in ("name", "platform"))
    if launchpad and "pump" in str(launchpad).lower().replace(".", "").replace(" ", ""):
        return True
    return False

def is_true_graduation(mint, p, launchpad=None, now=None):
    """True only for a recent pump.fun -> PumpSwap canonical migration."""
    if not is_pump_origin(mint, p, launchpad): return False
    if (p or {}).get("dexId") != "pumpswap": return False
    created = (p.get("pairCreatedAt") or 0) / 1000 if p else 0
    if not created: return False
    age = (now or time.time()) - created
    return 0 < age < 20 * 60

def lp_safety(mint, p, rep=None, locked_min=99):
    """(safe, why, locked_pct). safe is False for removable LP (Meteora etc.) unless verifiably locked."""
    dex = ((p or {}).get("dexId") or "").lower()
    labels = [str(x).lower() for x in ((p or {}).get("labels") or [])]
    pair = (p or {}).get("pairAddress")
    locked = mtype = None
    if rep:
        markets = rep.get("markets") or []
        mk = next((m for m in markets if pair and m.get("pubkey") == pair), None)
        if mk is None and len(markets) == 1: mk = markets[0]
        if mk:
            try: locked = float(util.get(mk, "lp.lpLockedPct"))
            except Exception: locked = None
            mtype = (mk.get("marketType") or mk.get("type") or "").lower()
    meteora = dex in UNSAFE_DEX or any("dyn" in x or "dlmm" in x or "damm" in x for x in labels) or (mtype or "").startswith("meteora")
    pump_can = is_pump_origin(mint, p, (rep or {}).get("launchpad")) and dex == "pumpswap"
    if pump_can:
        if locked is not None and locked < locked_min:
            return False, f"PumpSwap LP {locked:.0f}% unlocked", locked
        return True, "PumpSwap canonical", locked
    if meteora:
        if locked is not None and locked >= locked_min:
            return True, f"Meteora LP {locked:.0f}% locked", locked
        return False, "Meteora unlocked LP", locked if locked is not None else 0.0
    if dex == "pumpfun":
        return None, "still on bonding curve", None
    if locked is not None and locked >= locked_min:
        return True, f"LP {locked:.0f}% locked", locked
    if locked is not None:
        return False, f"{dex or 'pool'} LP {locked:.0f}% unlocked", locked
    if dex:
        return False, f"{dex} LP not locked", None
    return None, "LP unknown", None

def parse_rugcheck(rep):
    """Pull LP / origin / extension risks from a rugcheck report (no I/O)."""
    if not rep: return {}
    tm = rep.get("tokenMeta") or {}
    te = rep.get("token_extensions") or rep.get("tokenExtensions") or {}
    tf = rep.get("transferFee") or {}
    if not isinstance(te, dict): te = {}
    if not isinstance(tf, dict): tf = {}
    fee_pct = 0
    try: fee_pct = float(tf.get("pct") or 0)
    except Exception: pass
    tfc = te.get("transferFeeConfig") or {}
    if isinstance(tfc, dict) and tfc:
        try: fee_pct = max(fee_pct, float((tfc.get("newerTransferFee") or tfc).get("transferFeeBasisPoints") or 0) / 100)
        except Exception: pass
    dangerous = bool(te.get("permanentDelegate") or te.get("defaultAccountState") or te.get("nonTransferable") or fee_pct > 0)
    return {
        "launchpad": rep.get("launchpad"),
        "mutable": tm.get("mutable") if isinstance(tm, dict) else None,
        "transfer_fee_pct": fee_pct,
        "dangerous_ext": dangerous,
        "token_program": rep.get("tokenProgram") or (rep.get("token") or {}).get("tokenProgram"),
        "creator": rep.get("creator"),
        "rc_rugged": bool(rep.get("rugged")),
        "markets": rep.get("markets") or [],
    }

def blocked(sig, s=None):
    """Hard stop: no Telegram alert and no PAPER feed."""
    s = s or {}
    if sig.get("rugged"): return "rugged"
    if sig.get("dev_pct") is not None and sig["dev_pct"] >= 8: return "dev"
    if sig.get("mint_revoked") is False: return "mint live"
    if sig.get("dangerous_ext") or (sig.get("transfer_fee_pct") or 0) > 0: return "token extension"
    if sig.get("mutable"): return "mutable metadata"
    if s.get("gems_require_pump_origin", True) and not sig.get("pump_origin"): return "not pump.fun origin"
    if s.get("gems_require_lp_safe", True) and sig.get("lp_safe") is not True: return "LP not safe"
    return None


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
    if sig.get("pump_origin"): sc += 8; why.append("pump.fun origin")
    elif sig.get("pump_origin") is False:
        sc -= 20; why.append("not pump.fun origin"); sc = min(sc, 50)
    if sig.get("lp_safe") is True:
        sc += 10; why.append(sig.get("lp_why") or "LP locked")
    elif sig.get("lp_safe") is False:
        sc -= 40; why.append(sig.get("lp_why") or "LP unlocked"); sc = min(sc, 40)
    if sig.get("mutable"): sc -= 15; why.append("mutable metadata")
    if sig.get("dangerous_ext") or (sig.get("transfer_fee_pct") or 0) > 0:
        sc -= 20; why.append("transfer fee / token extension"); sc = min(sc, 50)
    if sig.get("creator_rugs"):
        n = int(sig["creator_rugs"]); sc -= 15 * min(n, 3); why.append(f"creator rugged {n}x")
    if sig.get("rugged") or sig.get("rc_rugged"):
        sc = min(sc, 10); why.append("rugged")
    # just_graduated bonus is only valid for true pump.fun migrations (caller must set it that way)
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
    extra = dict(extra or {})
    sig = signals_from_pair(p, s, extra)
    sig["dex"] = (p or {}).get("dexId")
    sig["liq"] = sig.get("liq") if sig.get("liq") is not None else (util.get(p, "liquidity.usd") or 0)
    # just graduated only for true pump.fun -> PumpSwap migrations
    extra.pop("just_graduated", None)
    sig.pop("just_graduated", None)
    rec = None
    if not extra.get("skip_deep"):
        try: rec = util.rugcheck(mint, ttl=300)
        except Exception: rec = None
    meta = parse_rugcheck(rec)
    sig.update({k: meta[k] for k in ("mutable", "transfer_fee_pct", "dangerous_ext", "rc_rugged")})
    creator = extra.get("creator") or meta.get("creator")
    if not creator:
        c = db.q("select creator from coins where mint=?", (mint,), True)
        creator = c["creator"] if c else None
    sig["creator"] = creator
    sig["pump_origin"] = is_pump_origin(mint, p, meta.get("launchpad"))
    sig["just_graduated"] = is_true_graduation(mint, p, meta.get("launchpad"))
    locked_min = s.get("gems_lp_locked_min_pct", 99)
    safe, lp_why, locked = lp_safety(mint, p, rec, locked_min)
    sig["lp_safe"], sig["lp_why"], sig["lp_locked_pct"] = safe, lp_why, locked
    if rec:
        top10, dev, bundles = _holders(mint)
        if sig.get("top10") is None: sig["top10"] = top10
        if sig.get("dev_pct") is None: sig["dev_pct"] = dev
        if sig.get("bundles") is None: sig["bundles"] = bundles
    if not extra.get("skip_deep"):
        mint_r, freeze_r = _authorities(mint)
        if mint_r is not None: sig["mint_revoked"] = mint_r
        if freeze_r is not None: sig["freeze_revoked"] = freeze_r
        bonus = _cluster_bonus(mint, creator)
        if bonus: sig["cluster_score"] = bonus
    if creator:
        n = db.q("select count(*) n from gem_rugs where creator=?", (creator,), True)
        if n and n["n"]: sig["creator_rugs"] = n["n"]
    already = db.q("select rug from gem_scores where mint=?", (mint,), True)
    if already and already["rug"]: sig["rugged"] = True
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

def _persist(mint, name, sc, why, sig, p=None):
    liq = sig.get("liq") or 0
    prev = db.q("select peak_liq,alerted_at,alert_liq,rug from gem_scores where mint=?", (mint,), True)
    peak = max(liq, (prev["peak_liq"] if prev and prev["peak_liq"] else 0) or 0)
    js = json.dumps({k: sig[k] for k in sig if k not in ("pair", "markets")})
    db.q("insert into gem_scores(mint,ts,score,mc,name,reasons,json,peak_liq,last_liq,creator,dex) values(?,?,?,?,?,?,?,?,?,?,?) on conflict(mint) do update set ts=excluded.ts,score=excluded.score,mc=excluded.mc,name=excluded.name,reasons=excluded.reasons,json=excluded.json,peak_liq=max(coalesce(gem_scores.peak_liq,0),excluded.peak_liq),last_liq=excluded.last_liq,creator=coalesce(excluded.creator,gem_scores.creator),dex=excluded.dex",
         (mint, time.time(), sc, sig.get("mc") or 0, name, " · ".join(why), js, peak, liq, sig.get("creator"), sig.get("dex")))
    return prev

def consider(mint, s, extra=None, p=None, alert=True):
    extra = extra or {}
    try:
        if p is None: p = (util.dex_tokens([mint], ttl=20, curve=True) or {}).get(mint)
    except Exception as e:
        log.info(f"dex {mint} {type(e).__name__}"); return None
    if not p: return None
    sc, why, sig = evaluate(mint, p, s, extra)
    name = extra.get("name") or util.get(p, "baseToken.name")
    prev = _persist(mint, name, sc, why, sig, p)
    why_block = blocked(sig, s)
    if alert and sc >= s["gems_alert_min_score"] and not why_block:
        _alert(name, mint, sc, why, sig.get("mc") or 0)
        liq = sig.get("liq") or 0
        if not (prev and prev["alerted_at"]):
            db.q("update gem_scores set alerted_at=?,alert_liq=? where mint=?", (time.time(), liq, mint))
    if alert and sc >= s["gems_min_score"] and not why_block:
        from agents import winners
        winners._feed(mint, "gems", name)
    return sc, why, sig, why_block

def record_rug(mint, name, peak, last, creator, dex, why="liq pulled"):
    already = db.q("select mint from gem_rugs where mint=?", (mint,), True)
    db.q("update gem_scores set rug=1,rug_at=coalesce(rug_at,?),last_liq=?,score=min(score,10),reasons=? where mint=?",
         (time.time(), last, "RUG · " + why, mint))
    db.q("insert or ignore into gem_rugs(mint,ts,name,peak_liq,last_liq,creator,dex,json) values(?,?,?,?,?,?,?,?)",
         (mint, time.time(), name, peak, last, creator, dex, json.dumps({"why": why})))
    try:
        from agents import winners
        winners.mark_rug(mint, creator=creator, name=name, why=why)
    except Exception as e:
        log.info(f"mark_rug {type(e).__name__}")
    if already: return False
    drop = (100 * (1 - last / peak)) if peak else 100
    notifier.send(f"PAPER · RUG: {name or mint[:6]} · liq ${peak:,.0f} → ${last:,.2f} (−{drop:.0f}%)" + (f" · {dex}" if dex else ""))
    try:
        import bus; bus.activity(f"RUG: {name or mint[:6]}", why, f"−{drop:.0f}%", "reject", mint)
    except Exception:
        pass
    log.info(f"RUG {name or mint[:6]} peak={peak} last={last} {dex or ''}")
    return True

def watch_alerted(s, pairs=None):
    """Re-check gems we scored/alerted; liq drop > rug_liq_drop_pct => rug note + penalties."""
    drop_need = s.get("gems_rug_liq_drop_pct", 80)
    rows = db.q("select * from gem_scores where coalesce(rug,0)=0 and (alerted_at is not null or peak_liq>0 or score>=60)")
    if not rows: return 0
    need = [r["mint"] for r in rows]
    if pairs is None:
        try: pairs = util.dex_tokens(need, ttl=15, curve=True)
        except Exception as e:
            log.info(f"watch dex {type(e).__name__}"); return 0
    n = 0
    for r in rows:
        db.beat("gems")
        p = (pairs or {}).get(r["mint"])
        liq = util.get(p, "liquidity.usd") if p else None
        if liq is None: liq = 0 if p and p.get("dexId") != "pumpfun" else None
        if liq is None: continue
        peak = max(r["peak_liq"] or 0, r["alert_liq"] or 0, liq)
        # don't treat "still on the curve / never had a pool" as a rug
        if peak < 1000: 
            db.q("update gem_scores set last_liq=?,peak_liq=? where mint=?", (liq, peak, r["mint"]))
            continue
        db.q("update gem_scores set last_liq=?,peak_liq=? where mint=?", (liq, peak, r["mint"]))
        if liq <= peak * (1 - drop_need / 100):
            dex = (p or {}).get("dexId") or r["dex"]
            if record_rug(r["mint"], r["name"], peak, liq, r["creator"], dex, f"liq dropped {100*(1-liq/peak):.0f}%"):
                n += 1
    return n

def rescore_existing(s=None, alert=False):
    """Re-score rows already in gem_scores with current safety rules. Returns list of result dicts."""
    s = s or config.load()
    rows = db.q("select * from gem_scores")
    if not rows: return []
    try: pairs = util.dex_tokens([r["mint"] for r in rows], ttl=0, curve=True)
    except Exception as e:
        log.info(f"rescore dex {type(e).__name__}"); pairs = {}
    out = []
    for r in rows:
        db.beat("gems")
        p = pairs.get(r["mint"])
        extra = {"name": r["name"], "creator": r["creator"]}
        if not p:
            out.append({"name": r["name"], "mint": r["mint"], "score": 0, "pass": False, "why": "no pair", "block": "no pair"})
            continue
        try:
            sc, why, sig, block = consider(r["mint"], s, extra, p, alert=alert)
        except Exception as e:
            out.append({"name": r["name"], "mint": r["mint"], "score": None, "pass": False, "why": type(e).__name__, "block": "error"})
            continue
        ok = sc >= s["gems_min_score"] and not block
        out.append({"name": r["name"], "mint": r["mint"], "score": sc, "pass": ok, "alert": sc >= s["gems_alert_min_score"] and not block,
                    "why": " · ".join(why), "block": block, "dex": sig.get("dex"), "liq": sig.get("liq"),
                    "lp_safe": sig.get("lp_safe"), "pump_origin": sig.get("pump_origin"), "rugged": sig.get("rugged")})
    watch_alerted(s, pairs)
    return out

def on_new_token(m):
    # cheap hook: if the create already looks crowded (high initial buy) we skip; otherwise wait for the loop
    return

def text():
    rows = db.q("select * from gem_scores where ts>? order by rug, score desc limit 12", (time.time() - 6 * 3600,))
    if not rows: return "Early gems · none scored in the last 6h yet."
    rugs = db.q("select count(*) n from gem_rugs where ts>?", (time.time() - 6 * 3600,), True)["n"]
    lines = [f"Early gems · {len(rows)} in last 6h" + (f" · {rugs} rugged" if rugs else "")]
    for r in rows:
        tag = "RUG " if r["rug"] else f"{r['score']:.0f}  "
        lines.append(f"{tag}{r['name'] or r['mint'][:6]}  MC ${r['mc'] or 0:,.0f}  {r['reasons'] or ''}")
    return "\n".join(lines)

def loop(stop):
    first = True
    while not stop.is_set():
        db.beat("gems")
        s = config.load()
        if not s.get("gems_enabled", True) or not db.kv_get("desk_running", True):
            stop.wait(10); continue
        try:
            try:
                n = watch_alerted(s)
                if n: log.info(f"watch marked {n} rugs")
            except Exception as e:
                log.info(f"watch {type(e).__name__}: {config.redact(e)}")
            if first:
                try:
                    log.info("rescoring stored gems with LP/origin rules")
                    rescore_existing(s, alert=False)
                except Exception as e:
                    log.info(f"rescore {type(e).__name__}: {config.redact(e)}")
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
                    got = consider(mint, s, extra, p)
                    if got is not None:
                        sc = got[0] if isinstance(got, tuple) else got
                        scored += 1
                        if sc >= 45 and not extra.get("skip_deep"): deep += 1
                except Exception as e:
                    log.info(f"score {mint} {type(e).__name__}: {config.redact(e)}")
            log.info(f"cycle looked={len(cands)} scored={scored}")
        except Exception as e:
            log.info(f"cycle err {type(e).__name__}: {config.redact(e)}")
        first = False
        stop.wait(s.get("gems_poll_s", 120))
