"""STEP 13 acceptance. Temp DB; prices faked via util.dex_tokens patch; sends captured (test_notify sends 1 real)."""
import sys, os, json, time, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db; db._c = None; db.P = "/tmp/accept.db"
if os.path.exists(db.P): os.remove(db.P)
import config, util, notifier, links, bus
from agents import analyst, executor, risk, auditor
SENT = []; REAL_SEND = notifier.send
notifier.send = lambda text, buttons=None, chat=None, kind=None: (SENT.append((text, buttons)), True)[1]
OV = {}; _load = config.load
config.load = lambda: {**_load(), **OV}
PX = {}
def dex(mints, ttl=0, curve=False): return {m: PX[m] for m in mints if m in PX}
util.dex_tokens = dex
def pair(price, liq=50000, **k): return {"priceUsd": str(price), "liquidity": {"usd": liq}, "volume": {"m5": 0}, **k}
res = {}
def ok(name, cond, *lines):
    res[name] = bool(cond); print(f"[{'PASS' if cond else 'FAIL'}] {name}"); [print("   ", l) for l in lines]
def reset(): 
    for t in ("positions","skips","activity","analyses","kv"): db.q(f"delete from {t}")
    SENT.clear(); OV.clear(); OV.update(mode="PAPER", push_rejects=False, desk_ticket_usd=0, min_size_usd=47.5, desk_max_open=0, cash_reserve_usd=0, daily_loss_usd=0, desk_require_dobry=False, mom_enabled=False); db.kv_set("paper_balance", 200.0)
time.sleep_orig = time.sleep; time.sleep = lambda x: None

# test_notify: one real PAPER push
reset(); M = "7PcJB8ooqEfWLPRZ6y1BYuQuAZL1Tm7csG5JqTjXpump"; u = links.coin_url(M)
r = REAL_SEND(f"PAPER · test_notify · Stonk Inu", [[{"text": "Open on FOMO", "url": u}]], kind="test")
ok("test_notify", r and u == f"https://fomo.family/coin?address={M}&chainId=1399811149", f"telegram ok={r} button url={u}")

# test_gate  (boundary test at the STEP 10 spec threshold 70, independent of the user's live min_score)
reset(); cases = []
OV["score_min"] = 70; OV["chase_max"] = 0.3
def gate(ans):
    analyst.rules = lambda p, a, s: dict(ans); before = bus.executor.qsize()
    analyst.analyse({"mint": "G"+str(len(cases)), "name": "Gate"}, {"layers": []})
    row = json.loads(db.q("select json from analyses where mint=?", ("G"+str(len(cases)),), True)["json"])
    passed = bus.executor.qsize() > before
    while not bus.executor.empty(): bus.executor.get()
    cases.append((ans, row["score"], passed, row["failed"])); return passed
T = dict(alive_6h=.70, organic=.65, chase=.30, flush=.75)
g = [not gate({**T, "alive_6h": .69}), not gate({**T, "organic": .64}), not gate({**T, "chase": .31}),
     not gate({**T, "flush": .71}), gate(T)]
acts = [r["sub"] for r in db.q("select sub from activity")]
ok("test_gate", all(g) and cases[3][1] == 69 and cases[4][1] == 70 and all(a.startswith("Analyst (rules)") for a in acts),
   *[f"{c[0]} score={c[1]} passed={c[2]} failed={c[3]}" for c in cases], f"activity subs: {acts}")
OV.pop("score_min", None)

# test_risk
def openpos(m, entry=1.0, stop=6, age=0):
    db.q("insert into positions(mint,name,mode,entry,size,peak,stop_pct,opened_at,status,json) values(?,?,?,?,?,?,?,?,?,?)",
         (m, m, "PAPER", entry, 47.5, entry, stop, time.time()-age, "open", "{}"))
def run(m, prices):
    for p in prices: PX[m] = pair(p); risk.tick(config.load())
    return db.q("select * from positions where mint=?", (m,), True)
reset(); openpos("TR"); a = run("TR", [1.25, 1.25*0.87])
OV.update(stop_pct=30, timeout_h=3, flat_min=0, death_liq_drop_pct=60)
openpos("ST", stop=30); b = run("ST", [0.69])
openpos("HOLD", stop=30); h = run("HOLD", [0.86])
openpos("TO", age=3*3600+1); c = run("TO", [1.04])
OV["stop_pct"] = 6; openpos("LO", stop=30); d = run("LO", [0.93])   # live setting applies to open positions
OV["stop_pct"] = 30
ok("test_risk", a["exit_reason"] == "trail" and b["exit_reason"] == "stop" and c["exit_reason"] == "timeout"
   and d["stop_pct"] == 6 and d["exit_reason"] == "stop" and h["status"] == "open",
   f"+25% then -13% off top -> {a['exit_reason']}", f"-31% -> {b['exit_reason']}", f"-14% held -> {h['status']}", f"3h at +4% -> {c['exit_reason']}",
   f"live stop 6% on open pos -7%: stop_pct={d['stop_pct']} status={d['status']}",
   *[t for t, _ in SENT if "hit the" in t])

# test_executor
def sig(m, price=1.0, liq=50000):
    PX[m] = pair(price, liq); executor.run({"mint": m, "name": m, "liquidityUsd": liq}, {"priceUsd": price, "score": 75})
    return db.q("select * from positions where mint=?", (m,), True), db.q("select * from skips where mint=?", (m,), True)
reset(); p1, _ = sig("BUMP")
reset(); OV["below_min"] = "skip"; p2, s2 = sig("SKIP"); w = executor.ticket_check(push=False)
reset(); db.kv_set("paper_balance", 20000.0); p3, _ = sig("CAP", liq=40000)
ok("test_executor", p1 and p1["size"] == 47.5 and not p2 and s2 and "skip" not in (w or "x")[:0] and w and p3["size"] == 800,
   f"$200 stack, 8%=$16 bump -> size {p1 and p1['size']}", f"skip mode -> skipped: {s2 and s2['reason']}",
   f"PAPER $200 + skip warning: {w}", f"stack $20000 liq $40000 -> size {p3['size']} (2% of pool cap)",
   *[t for t, _ in SENT if t.startswith("PAPER · Buy")][:1])

# auditor: recorded fixture = real USDC getAccountInfo (freeze authority set) with mintAuthority nulled
reset(); U = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"; FIX = json.load(open(os.path.join(os.path.dirname(__file__), "fixture_freeze.json")))
_rpc = util.rpc; util.rpc = lambda url, m, p, t=30: FIX if m == "getAccountInfo" else _rpc(url, m, p, t)
db.q("insert into coins(mint,migrated_at) values(?,?)", (U, time.time()))
auditor.audit({"mint": U, "name": "FreezeFixture", "pool": None, "liquidityUsd": 1e6, "ageMin": 10})
A = json.loads(db.q("select json from audits", one=True)["json"]); L = A["layers"]
ok("test_auditor_freeze", L[0]["pass"] and L[1]["pass"] is False and all(l["value"] == "not checked" for l in L[2:]),
   "fixture: real USDC account (freeze set) via Helius, mintAuthority nulled", *[f"{l['n']} {l['name']}: {l['pass']} {l['value']}" for l in L])

# test_task11: fake volume, report verdict, learning bounds, wallet commands
import volq, report, learning, notifier as _nt
org = [{"wallet": f"w{i}", "kind": "buy" if i % 3 else "sell", "usd": 10 + i * 1.37, "ts": 0, "tx": str(i)} for i in range(300)]
fake = [{"wallet": f"b{i % 6}", "kind": "buy" if i % 2 else "sell", "usd": 250.0, "ts": 0, "tx": str(i)} for i in range(300)]
pr = {"liquidity": {"usd": 50000}, "volume": {"h1": 40000}}
qo, _ = volq.quality(org, pr); qf, sf = volq.quality(fake, pr); S_ = config.load()
rb = report.build(60, qf, sf, {"listed": False}, [], S_); rg = report.build(80, qo, {"unique_wallets": 300, "trades": 300}, {"listed": True, "x": {"handle": "a", "ok": True, "followers": 5000}, "telegram": {"name": "x", "ok": True, "members": 900}, "website": {"ok": True}}, [], S_)
db.q("delete from kv where k='learn_last'") if False else None
db.kv_set("learn_last", {"stats": {}, "proposals": [{"section": "risk", "key": "stop_pct", "old": 6, "new": 3, "why": "x"}, {"section": "auditor", "key": "max_top10_pct", "old": 30, "new": 90, "why": "x"}]})
ap = learning.apply()
addr = "FHpcNSe6tb2n15bAdq4BkeYWGyZKFD7yLYrH92ng7wCT"
_nt.command("/wallet_add " + addr); tracked = db.q("select tracked from wallets where addr=?", (addr,), True)
_nt.command("/wallet_remove " + addr); un = db.q("select tracked from wallets where addr=?", (addr,), True)
ok("test_task11", qo >= 0.7 and qf < S_["vq_kill"] and rb["verdict"] == "ZŁY" and rg["verdict"] == "DOBRY" and 3 <= len(rg["bullets"]) <= 5
   and "Brak" in ap and tracked and tracked["tracked"] == 1 and un["tracked"] == 0 and config.load()["stop_pct"] >= 6,
   f"organic q={qo} fake q={qf} {sf}", f"bad={rb['verdict']} good={rg['verdict']} {rg['bullets']}", f"unsafe apply -> {ap}")
# test_task12: pre-filter cuts heavy dev buys, curve-promoted coin goes to waiting only on migration, filters untouched
from agents import prefilter, scanner as _sc
import stats
prefilter.on_create({"mint": "DEVHEAVY", "bondingCurveKey": "C1", "initialBuy": 3e8})
prefilter.on_create({"mint": "CURVEOK", "bondingCurveKey": "C2", "initialBuy": 1e7, "name": "Ok", "symbol": "OK"})
heavy = "DEVHEAVY" in prefilter.W; okw = "CURVEOK" in prefilter.W
prefilter.W["CURVEOK"]["ts"] -= 120
_orig = prefilter.curves; prefilter.curves = lambda keys, u: {"C2": (60.0, False)}
prefilter.tick(config.load()); prefilter.curves = _orig
st1 = db.q("select status from coins where mint='CURVEOK'", (), True)
_rc = util.rugcheck; util.rugcheck = lambda *a, **k: {}
_sc.on_migration({"mint": "CURVEOK", "signature": "s"}); util.rugcheck = _rc
st2 = db.q("select status from coins where mint='CURVEOK'", (), True)
S2 = config.load(); fun = stats.window(15)
ok("test_task12", not heavy and okw and st1["status"] == "curve" and st2["status"] == "waiting" and S2["min_liq_usd"] == 10000
   and S2["max_top10_pct"] == 45 and S2["score_min"] == 60 and fun.get("pre_kill", 0) >= 1 and fun.get("pre_pass", 0) >= 1,
   f"dev 30% kept={heavy} curve 60% -> {st1['status']} -> migrate -> {st2['status']}", f"funnel {fun}")
db.q("delete from coins where mint='CURVEOK'")

# test_copy: top wallet buys -> paper copy entry (fast rug ok) -> TP +15% sells all; second copy exits on follow_sell; bot wallet excluded
from agents import copytrade as ct
reset(); db.q("delete from copy_top"); db.q("delete from copy_events")
W1 = "TopWa11et1111111111111111111111111111111111"
db.q("insert into copy_top values(?,?,?,?,?,?,?,?,?,?)", (W1, 1, 500, .8, 10, 60, 8, 600, time.time(), "{}"))
_fr = ct.fast_rug; ct.fast_rug = lambda mint, p, s: None
OV.update(copy_enabled=True, copy_two_step=False, copy_tp_pct=15, copy_follow_sell=True, copy_max_open=3, copy_min_buy_sol=0.1, copy_size_pct=25)
PX["CPY1"] = pair(1.0, 100000, pairAddress="P1", baseToken={"symbol": "CPY1"}); now = time.time()
ct.on_buy(W1, "CPY1", 2.0, now-3, now-1, "ws")
p1 = db.q("select * from positions where mint='CPY1'", (), True)
imp_ok = p1 and p1["strategy"] == "copy" and p1["copied"] == W1 and p1["entry"] > 1.0 and p1["entry"] < 1.01
PX["CPY1"] = pair(1.0, 100000)
ct.on_buy(W1, "LOWLIQ", 2.0, now, now, "ws") if not PX.update(LOWLIQ=pair(1.0, 5000)) else None
low = db.q("select reason from copy_events where mint='LOWLIQ'", (), True); lowpos = db.q("select 1 from positions where mint='LOWLIQ'", (), True)
PX["CPY1"] = pair(p1["entry"]*1.16, 100000); risk.tick(config.load())
c1 = db.q("select * from positions where mint='CPY1'", (), True)
PX["CPY2"] = pair(2.0, 80000, baseToken={"symbol": "CPY2"}); ct.on_buy(W1, "CPY2", 1.0, now, now, "ws")
PX["CPY2"] = pair(2.04, 80000); ct.on_sell(W1, "CPY2", now, now)
c2 = db.q("select * from positions where mint='CPY2'", (), True)
dsk = db.q("select count(*) n from positions where strategy='desk'", (), True)["n"]
bot = {"tpd": 900}; OV["copy_max_trades_day"] = 120
_h = ct.history; ct.history = lambda w, days=7, pages=8: ([{"timestamp": time.time()-i*60, "accountData": [], "tokenTransfers": []} for i in range(300)], False)
br = ct.score_wallet("BOT", set(), 100.0); ct.history = _h; ct.fast_rug = _fr
msgs = [t for t, b in SENT if "Kopia" in t]
ok("test_copy", imp_ok and c1["status"] == "closed" and c1["exit_reason"] == "tp" and c1["pnl"] > 0 and c2["status"] == "closed" and c2["exit_reason"] == "follow"
   and low and lowpos and br["bot"] and len(msgs) >= 3 and dsk == 0,
   f"entry {p1['entry']:.5f} vs quote 1.0 (impact+slip)", f"CPY1 {c1['exit_reason']} pnl {c1['pnl']}", f"CPY2 {c2['exit_reason']} pnl {c2['pnl']}",
   f"low liq copied (no liq filter): {bool(lowpos)}", f"bot: {br['bot']}", f"tg: {msgs[:3]}")
# test_strategy: desk tp1 50% at +25% -> break-even stop; flat time stop; daily loss guard
import guard
ST = dict(stop_pct=7, tp1_pct=25, trail_on_pct=25, trail_pct=12, breakeven_after_tp=True, flat_min=45, flat_pct=3)
reset(); OV.update(ST); openpos("BE", age=60); PX["BE"] = pair(1.26); risk.tick(config.load()); b1 = db.q("select * from positions where mint='BE'", (), True)
PX["BE"] = pair(0.999); risk.tick(config.load()); b2 = db.q("select * from positions where mint='BE'", (), True)
reset(); OV.update(ST); openpos("FLAT", age=46*60); PX["FLAT"] = pair(1.01); risk.tick(config.load()); f1 = db.q("select * from positions where mint='FLAT'", (), True)
reset(); db.q("insert into positions(mint,name,mode,entry,size,peak,stop_pct,opened_at,status,json,pnl,closed_at) values('L','L','PAPER',1,30,1,7,?,'closed','{}',-21,?)", (time.time(), time.time()))
gb = guard.block({**config.load(), "daily_loss_usd": 20}, 31, 200); gr = guard.block({**config.load(), "daily_loss_usd": 0, "cash_reserve_usd": 20}, 51, 60)
ok("test_strategy", b1["tp_done"] == 1 and b1["status"] == "open" and b2["status"] == "closed" and b2["exit_reason"] == "breakeven" and f1["exit_reason"] == "flat" and gb and "daily" in gb and gr and "reserve" in gr,
   f"tp1 size {b1['size']} part {b1['part_pnl']} -> {b2['exit_reason']} pnl {b2['pnl']}", f"flat -> {f1['exit_reason']} pnl {f1['pnl']}", f"guard: {gb} | {gr}")
print("RESULT", json.dumps(res))
