"""Funnel counters: in-memory per-minute counts, flushed to sqlite every 20s. No per-coin Activity rows."""
import time, threading, db
_l = threading.Lock(); _m = {}; _last = [0.0]
db.q("create table if not exists funnel(minute integer,stage text,n integer,primary key(minute,stage))")
def ev(stage, n=1):
    k = (int(time.time() // 60), stage)
    with _l: _m[k] = _m.get(k, 0) + n
    if time.time() - _last[0] > 20: flush()
def flush():
    with _l: items = list(_m.items()); _m.clear(); _last[0] = time.time()
    for (mi, st), n in items:
        db.q("insert into funnel values(?,?,?) on conflict(minute,stage) do update set n=n+excluded.n", (mi, st, n))
def window(minutes=15):
    flush(); since = int(time.time() // 60) - minutes
    return {r["stage"]: r["n"] for r in db.q("select stage,sum(n) n from funnel where minute>? group by 1", (since,))}
def since(t0):
    flush(); return {r["stage"]: r["n"] for r in db.q("select stage,sum(n) n from funnel where minute>=? group by 1", (int(t0 // 60),))}
