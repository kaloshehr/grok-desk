"""Telegram (STEP 9): long polling, owner-only. Token from env TG_TOKEN (never logged)."""
import os, re, time, json, datetime, requests, util, config, db, links
log = util.logger("telegram")
API = lambda m: f"https://api.telegram.org/bot{os.environ['TG_TOKEN']}/{m}"
AGENTS = ("scanner", "auditor", "analyst", "executor", "risk")
def owner(): return config.secret("OWNER_CHAT_ID")
def _kind(text):
    t = text.removeprefix("PAPER · ").lower()
    for k in ("signal","bought","sold","stop","trail","timeout","ticket","report","reject","skip","paused","resumed","status","why","app"):
        if k in t[:80]: return k
    return "reply"
def send(text, buttons=None, chat=None, kind=None):
    kind = kind or _kind(text)
    body = {"chat_id": chat or owner(), "text": text, "disable_web_page_preview": True}
    if buttons: body["reply_markup"] = {"inline_keyboard": buttons}
    for i in range(3):
        try:
            r = requests.post(API("sendMessage"), json=body, timeout=15)
            j = r.json() if r.headers.get("content-type","").startswith("application/json") else {}
            if r.ok and j.get("ok"):
                log.info(f"send ok type={kind} msg_id={j['result']['message_id']} len={len(text)} buttons={bool(buttons)}")
                return True
            log.info(f"send fail type={kind} http={r.status_code} desc={str(j.get('description',''))[:80]} try={i+1}")
        except Exception as e: log.info(f"send fail type={kind} err={type(e).__name__} try={i+1}")
        time.sleep(2)
    return False
def fomo_btn(mint, label="Open on FOMO"): return [[{"text": label, "url": links.coin_url(mint)}]]
def find_coin(x):
    x = x.strip().lstrip("$")
    r = db.q("select * from coins where mint=? or lower(symbol)=lower(?) or lower(name)=lower(?) order by migrated_at desc", (x, x, x), True)
    return r
def status_text():
    s = config.load()
    if not s.get("legacy_pipeline"):
        from agents import winners, gems
        return (f"{s['mode']} · alerts-only (winners + gems)\n" + winners.text() + "\n\n" + gems.text())
    from agents.executor import stack
    op = db.q("select name,size,entry,mint from positions where status in ('open','exiting')")
    paused = [a for a in AGENTS if db.kv_get(f"paused:{a}")]
    return (f"{s['mode']} · stack ${stack(s):.2f}" + (f" · free ${db.kv_get('paper_balance', s['paper_balance']):.2f}" if s['mode']=='PAPER' else "") +
            f"\nWaiting room {db.q('select count(*) n from coins where status=\"waiting\"',one=True)['n']} · open {len(op)}" +
            "".join(f"\n- {p['name']} ${p['size']} {links.coin_url(p['mint'])}" for p in op) +
            f"\nstop -{s['stop_pct']}% · trail {s['trail_pct']}% from +{s['trail_on_pct']}%" + (f"\npaused: {', '.join(paused)}" if paused else ""))
def why(x):
    c = find_coin(x)
    if not c: return f"don't know {x}", None
    m = c["mint"]; lines = [f"{c['name'] or m[:6]} · {links.coin_url(m)}"]
    sc = db.q("select verdict,reason from scans where mint=? order by id desc", (m,), True)
    lines.append(f"Scanner: {sc['verdict']} {sc['reason'] or ''}" if sc else f"Scanner: {c['status']}")
    a = db.q("select json from audits where mint=?", (m,), True)
    if a:
        a = json.loads(a["json"]); lines.append("Auditor: " + ("8/8" if a["passed"] else f"red · {a['reason']}"))
        lines += [f"  {l['n']} {l['name']}: {l['value']}" for l in a["layers"]]
    an = db.q("select json from analyses where mint=?", (m,), True)
    if an: an = json.loads(an["json"]); lines.append(f"Analyst: score {an['score']} " + ("pass" if an["passed"] else "· " + ", ".join(an["failed"])) + f" {an['answers']}")
    sk = db.q("select reason from skips where mint=?", (m,), True)
    if sk: lines.append(f"Executor: skipped · {sk['reason']}")
    p = db.q("select * from positions where mint=?", (m,), True)
    if p: lines.append(f"Position: {p['status']} entry {p['entry']} size ${p['size']}" + (f" exit {p['exit_reason']} pnl ${p['pnl']}" if p['exit_reason'] else ""))
    return "\n".join(lines), m
def report_text():
    s = config.load(); since = time.time() - 24*3600
    if not s.get("legacy_pipeline"):
        from agents import winners, gems
        top = db.q("select name,max_mult,ath_mc,rug from winner_coins where coalesce(rank,0)>0 and coalesce(rug,0)=0 order by rank")
        rugs = db.q("select name,peak_liq,last_liq from gem_rugs where ts>?", (since,))
        gems_n = db.q("select count(*) n from gem_scores where ts>?", (since,), True)["n"]
        links_n = db.q("select count(distinct addr) n from winner_roles", one=True)["n"]
        lines = [f"{'PAPER · ' if s['mode']=='PAPER' else ''}Report · alerts-only (winners + gems)",
                 f"{len(top)} in the top-10 · {links_n} linked wallets · {gems_n} gems scored last 24h · {len(rugs)} rugs",
                 winners.text()]
        for r in rugs:
            lines.append(f"  RUG {r['name']} · liq ${r['peak_liq'] or 0:,.0f} → ${r['last_liq'] or 0:,.2f}")
        lines.append(gems.text())
        return "\n".join(lines)
    from agents.executor import stack
    cl = db.q("select * from positions where closed_at>? ", (since,))
    pnl = sum(p["pnl"] or 0 for p in cl); w = sum(1 for p in cl if (p["pnl"] or 0) > 0)
    k = db.q("select count(*) n from coins where migrated_at>?", (since,), True)["n"]
    rej = db.q("select count(*) n from audits where passed=0", one=True)["n"]
    g = db.kv_get("gpa_down_kills", 0)
    return ((f"Layer 8 was down: {g} coins killed by it\n" if g else "") +
            f"{'PAPER · ' if s['mode']=='PAPER' else ''}Report · stack ${stack(s):.2f}\n{k} pools seen · {rej} rejected by Auditor · "
            f"{len(cl)} closed trades ({w} won) · PnL ${pnl:+.2f}" + "".join(f"\n- {p['name']} {p['exit_reason']} ${p['pnl']:+.2f} {links.coin_url(p['mint'])}" for p in cl))
def full_paper_night():
    st = db.kv_get("paper_since")
    return bool(st) and time.time() - st >= 8*3600
def command(t):
    s = config.load(); raw = t.strip(); t = raw.lower()
    if t in ("status", "/status", "/start"): return status_text(), None
    if t in ("report", "/report"): return report_text(), None
    m = re.match(r"why (.+)", t)
    if m: return why(m.group(1))
    m = re.match(r"buy (.+)", t)
    if m:
        c = find_coin(m.group(1)); from agents import executor
        if not c: return f"don't know {m.group(1)}", None
        return ("marked bought" if executor.fill(c["mint"]) else "no pending signal for that coin (I only track buys I signalled)"), c["mint"]
    m = re.match(r"sell (.+)", t)
    if m:
        c = find_coin(m.group(1))
        p = c and db.q("select * from positions where mint=? and status in ('open','exiting')", (c["mint"],), True)
        if not p: return "no open position for that coin", c["mint"] if c else None
        if p["status"] == "exiting": db.q("update positions set status='closed' where id=?", (p["id"],)); return "closed", c["mint"]
        from agents import risk
        pr = util.dex_tokens([c["mint"]], ttl=0).get(c["mint"]); price = float(pr.get("priceUsd") or 0) if pr else 0
        if not price: return "no price right now, tap Sell on FOMO", c["mint"]
        risk.close(p, price, "manual", s); return f"exit at {price}", c["mint"]
    m = re.match(r"^/?wallet_add\s+([1-9A-HJ-NP-Za-km-z]{32,44})$", raw, re.I)
    if m:
        a = m.group(1); db.q("insert or ignore into wallets(addr,origin,tracked,added_at) values(?,?,1,?)", (a, "manual", time.time()))
        db.q("update wallets set tracked=1,origin='manual' where addr=?", (a,))
        return f"Tracking wallet {a[:4]}…{a[-4:]} (PAPER, read-only). Its buys go to the Scanner as source 'wallets'.", None
    m = re.match(r"^/?wallet_remove\s+(\S+)$", raw, re.I)
    if m:
        k = m.group(1); r = db.q("select addr from wallets where addr=? or addr like ?", (k, k + "%"), True)
        if not r: return "no such wallet", None
        db.q("update wallets set tracked=0,origin='removed' where addr=?", (r["addr"],)); return f"Stopped tracking {r['addr'][:4]}…{r['addr'][-4:]}", None
    if t in ("/copy_on", "copy_on", "/copy_off", "copy_off"):
        on = t.endswith("on"); config.set_value("copy", "enabled", on)
        return ("Copy trading ON (PAPER): kopiuję zakupy Top 10 portfeli z 7 dni" if on else "Copy trading OFF: nowe kopie wstrzymane, otwarte pozycje dalej pilnuje Risk"), None
    if t in ("/top", "top", "/copy", "copy"):
        rs = db.q("select * from copy_top order by rank"); n = db.q("select count(*) n from positions where strategy='copy' and status='open'", one=True)["n"]
        return (f"Copy {'ON' if s['copy_enabled'] else 'OFF'} · {n} open · TP +{s['copy_tp_pct']}%\n" + "\n".join(f"#{r['rank']} {r['addr'][:4]}…{r['addr'][-4:]} ${r['pnl']:,.0f} · {r['win_rate']*100:.0f}% win · {r['closed']} closed" for r in rs)) or "no top wallets yet", None
    if t in ("/wallets", "wallets"):
        rs = db.q("select addr,win_rate,pnl,coins from wallets where tracked=1 order by score desc limit 15")
        return ("Tracked wallets:\n" + "\n".join(f"{r['addr'][:4]}…{r['addr'][-4:]} · win {int(100*(r['win_rate'] or 0))}% · PnL ${r['pnl'] or 0:,.0f} · {r['coins']} coins" for r in rs)) if rs else "No tracked wallets yet.", None
    if t in ("/winners", "winners"):
        from agents import winners; return winners.text(), None
    if t in ("/gems", "gems"):
        from agents import gems; return gems.text(), None
    if t in ("/apply_learn", "apply_learn"):
        import learning; return learning.apply(), None
    if t in ("/learn", "learn"):
        import learning; return learning.text(learning.run(send=False)), None
    if t in ("/start_desk", "start_desk", "start desk"):
        db.kv_set("desk_running", True)
        return f"{'PAPER · ' if s['mode']=='PAPER' else ''}Desk started: Analyst signals + Executor entries on. Mode unchanged ({s['mode']}); LIVE still needs typing LIVE.", None
    if t in ("/stop_desk", "stop_desk", "stop desk"):
        db.kv_set("desk_running", False)
        return "Desk paused: no new signals or entries. Scanner/Auditor keep watching, Risk keeps guarding open positions.", None
    m = re.match(r"(pause|resume) (\w+)", t)
    if m:
        act, ag = m.groups()
        if ag not in AGENTS: return f"agents: {', '.join(AGENTS)}", None
        if ag == "risk" and act == "pause" and db.q("select 1 from positions where status in ('open','exiting')", one=True):
            return "Risk can't be paused while a position is open", None
        db.kv_set(f"paused:{ag}", act == "pause"); return f"{ag} {act}d", None
    m = re.match(r"set (.+?) to (.+)", t)
    if m:
        f = config.find(m.group(1))
        if not f or f[1] == "mode": return f"unknown setting {m.group(1)}", None
        try: old, new = config.set_value(*f, m.group(2))
        except ValueError as e: return str(e), None
        if f[1] == "stop_pct": return f"stop -{old:g}% -> -{new:g}% (new positions; open ones only if tighter)", None
        u = "%" if f[1].endswith("pct") else ""
        return f"{f[1].replace('_pct','')} {old}{u} -> {new}{u}", None
    m = re.match(r"stack \$?(\d+(\.\d+)?)", t)
    if m:
        try: old, new = config.set_value("desk", "stack", m.group(1))
        except ValueError as e: return str(e), None
        return f"LIVE stack ${old} -> ${new}", None
    if t == "paper":
        config.set_value("desk", "mode", "PAPER"); db.kv_set("paper_since", time.time()); return "PAPER mode", None
    if t == "live":
        if not full_paper_night(): return "LIVE needs at least one full PAPER night (8h) first", None
        db.kv_set("live_armed", time.time()); return "Type LIVE (capitals) within 60s to switch. Signals only: you still tap every trade in FOMO.", None
    if raw == "LIVE":
        if not (db.kv_get("live_armed") and time.time() - db.kv_get("live_armed") < 60) or not full_paper_night(): return "send 'live' first", None
        config.set_value("desk", "mode", "LIVE"); return f"LIVE · stack ${config.load()['stack']} (set with 'stack N')", None
    return ("commands:\nstatus · report · why X · buy X · sell X\npause <agent> · resume <agent>\n"
            "winners · gems · set <setting> to <v> (e.g. set stop to -8%) · stack N · paper · live"), None
def callback(data):
    from agents import executor
    kind, mint = data.split(":", 1)
    if kind == "bought": return "position opened" if executor.fill(mint) else "nothing pending"
    if kind == "skip":
        pend = db.kv_get(f"pending:{mint}")
        if pend: executor.skip(pend["out"], "you skipped", pend["sc"])
        return "skipped"
    if kind == "sold":
        db.q("update positions set status='closed' where mint=? and status='exiting'", (mint,)); return "closed"
def daily(s):
    today = datetime.date.today().isoformat()
    if time.strftime("%H:%M") >= s["report_time"] and db.kv_get("report_day") != today and time.strftime("%H") < "12":
        db.kv_set("report_day", today); send(report_text())
def loop(stop):
    off = db.kv_get("tg_offset", 0)
    while not stop.is_set():
        db.beat("desk")
        try: daily(config.load())
        except Exception as e: log.info(f"report err {e}")
        try:
            r = requests.get(API("getUpdates"), params={"offset": off, "timeout": 25}, timeout=35).json()
            for u in r.get("result", []):
                off = u["update_id"] + 1; db.kv_set("tg_offset", off)
                if "callback_query" in u:
                    cq = u["callback_query"]
                    if str(cq["from"]["id"]) != str(owner()): continue
                    res = callback(cq.get("data", "")); requests.post(API("answerCallbackQuery"), json={"callback_query_id": cq["id"], "text": res}, timeout=10)
                elif "message" in u:
                    msg = u["message"]
                    if str(msg["chat"]["id"]) != str(owner()): continue
                    txt, mint = command(msg.get("text", ""))
                    send(txt, fomo_btn(mint) if mint else None)
        except Exception as e: log.info(f"poll err {type(e).__name__}"); stop.wait(5)
