"""STEP 4 schema (sqlite)."""
import sqlite3, os, threading, json
from config import BASE
P = os.path.join(BASE, "desk.db"); _l = threading.Lock()
SCHEMA = """
create table if not exists coins(mint text primary key,name text,symbol text,pool text,creator text,
  creator_balance real,migrated_at real,signature text,status text default 'waiting',reason text,scan_liq real,passed_at real);
create table if not exists scans(id integer primary key,mint text,ts text,verdict text,reason text,json text);
create table if not exists prices(mint text,ts real,price real,vol_m5 real);
create index if not exists ip on prices(mint,ts);
create table if not exists audits(mint text primary key,ts text,passed int,json text);
create table if not exists analyses(mint text primary key,ts text,score int,passed int,json text);
create table if not exists positions(id integer primary key,mint text unique,name text,mode text,entry real,size real,
  peak real,stop_pct real,trail_active int default 0,opened_at real,status text,exit_price real,exit_reason text,
  closed_at real,pnl real,fees real,last_push real,no_price int default 0,json text);
create table if not exists heartbeats(agent text primary key,ts real);
create table if not exists skips(mint text primary key,ts text,reason text);
create table if not exists activity(id integer primary key,ts text,title text,sub text,right text,kind text);
create table if not exists runners(day text,key text,primary key(day,key));
create table if not exists kv(k text primary key,v text);
create table if not exists wallets(addr text primary key,origin text,tracked int default 0,coins int default 0,wins int default 0,
  pnl real default 0,win_rate real,score real,last_buy_ts real,last_buy_mint text,last_sig text,added_at real,json text);
create table if not exists wallet_buys(id integer primary key,addr text,mint text,ts real,usd real,sig text unique);
create table if not exists coin_extra(mint text primary key,ts real,vq real,vq_json text,social text,report text,wallets text);
create table if not exists change_log(id integer primary key,ts text,section text,key text,old text,new text,who text);
"""
def conn():
    c = sqlite3.connect(P, timeout=30, check_same_thread=False); c.row_factory = sqlite3.Row; return c
_c = None
def q(sql, args=(), one=False):
    global _c
    with _l:
        if _c is None:
            _c = conn(); _c.executescript(SCHEMA)
            for col in ("source text default 'pumpportal'", "found_at real"):
                try: _c.execute("alter table coins add column " + col)
                except Exception: pass
            for col in ("strategy text default 'desk'", "copied text", "tp_done integer default 0", "part_pnl real default 0", "orig_size real"):
                try: _c.execute("alter table positions add column " + col)
                except Exception: pass
            _c.execute("create table if not exists copy_top(addr text primary key,rank int,pnl real,win_rate real,closed int,trades int,tpd real,score real,updated real,json text)")

            _c.execute("create table if not exists winner_coins(mint text primary key,name text,symbol text,pool text,creator text,found_at real,launched_at real,ath_mc real,max_mult real,first_mc real,status text,rug int default 0,analyzed int default 0,json text)")
            _c.execute("create table if not exists winner_wallets(addr text primary key,role text,cluster_id integer,wins int default 0,launches int default 0,snipes int default 0,rugs int default 0,avg_hold_s real,dump_speed real,score real,first_seen real,json text)")
            _c.execute("create table if not exists winner_clusters(id integer primary key,funder text,wallets_n int default 0,wins int default 0,launches int default 0,snipes int default 0,rugs int default 0,avg_dump_s real,score real,label text,updated real,json text)")
            _c.execute("create table if not exists winner_roles(mint text,addr text,role text,buy_ts real,sell_ts real,hold_s real,usd real,primary key(mint,addr,role))")
            _c.execute("create table if not exists gem_scores(mint text primary key,ts real,score real,mc real,name text,reasons text,json text)")
            _c.execute("create index if not exists i_wclusters_score on winner_clusters(score desc)")
            _c.execute("create index if not exists i_gems_ts on gem_scores(ts desc)")
            for col in ("peak_liq real", "last_liq real", "alerted_at real", "alert_liq real", "rug int default 0", "rug_at real", "creator text", "dex text"):
                try: _c.execute("alter table gem_scores add column " + col)
                except Exception: pass
            for col in ("rank int", "walk_before text", "walk_done int default 0", "mapped_at real"):
                try: _c.execute("alter table winner_coins add column " + col)
                except Exception: pass
            _c.execute("create index if not exists i_wroles_addr on winner_roles(addr)")
            _c.execute("create table if not exists gem_rugs(mint text primary key,ts real,name text,peak_liq real,last_liq real,creator text,dex text,json text)")

            _c.execute("create table if not exists copy_events(id integer primary key,ts real,addr text,mint text,kind text,block_ts real,detect_ts real,action text,reason text,latency real)")
        cur = _c.execute(sql, args); r = cur.fetchall(); _c.commit()
    return (r[0] if r else None) if one else r
def kv_get(k, d=None):
    r = q("select v from kv where k=?", (k,), True); return json.loads(r["v"]) if r else d
def kv_set(k, v): q("insert or replace into kv values(?,?)", (k, json.dumps(v)))

_hb = {}
def beat(agent, every=15):
    import time
    t = time.time()
    if t - _hb.get(agent, 0) >= every:
        _hb[agent] = t; q("insert into heartbeats(agent,ts) values(?,?) on conflict(agent) do update set ts=excluded.ts", (agent, t))
