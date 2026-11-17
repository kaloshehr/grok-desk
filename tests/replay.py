"""Replay all logged migrations (desk.db coins) through PAPER pipeline on a temp DB with live DexScreener/RPC/RugCheck data now."""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
coins = [dict(r) for r in db.q("select mint,creator,creator_balance,migrated_at from coins order by migrated_at")]
db._c = None; db.P = "/tmp/replay.db"
if os.path.exists(db.P): os.remove(db.P)
import config, util, notifier, bus
from agents import scanner, auditor, analyst, executor, risk
SENT = []; notifier.send = lambda t, b=None, chat=None, kind=None: (SENT.append(t), True)[1]
db.kv_set("paper_balance", 200.0); s = config.load()
span = (coins[-1]["migrated_at"] - coins[0]["migrated_at"]) / 3600
print(f"logged migrations: {len(coins)} spanning {span:.2f}h")
for c in coins: db.q("insert into coins(mint,creator,creator_balance,migrated_at,status) values(?,?,?,?,'waiting')", (c["mint"], c["creator"], c["creator_balance"], c["migrated_at"]))
s["min_pool_age_min"] = 0
pairs = util.dex_tokens([c["mint"] for c in coins])
for c in db.q("select * from coins"):
    if c["mint"] in pairs: scanner.decide(c, pairs[c["mint"]], s)
    else: db.q("update coins set status='kill',reason='no dex pair' where mint=?", (c["mint"],))
while not bus.auditor.empty(): auditor.audit(bus.auditor.get())
while not bus.analyst.empty(): analyst.analyse(*bus.analyst.get())
while not bus.executor.empty(): executor.run(*bus.executor.get())
for i in range(4):
    risk.tick(config.load()); time.sleep(15)
print("scanner:", [(r["status"], r["n"]) for r in db.q("select status,count(*) n from coins group by status")])
print("Activity rows:"); [print("  ", r["ts"], "|", r["title"], "|", r["sub"], "|", r["right"]) for r in db.q("select * from activity order by id")]
print("pushes:", SENT); print("paper balance:", db.kv_get("paper_balance"))
