"""Settings. STEP 10 defaults, nested by agent; load() also returns flat internal aliases.
rpc_url is read from env DESK_RPC_URL (never stored); logged/published only as [rpc]."""
import json, os, time
BASE = os.path.dirname(os.path.abspath(__file__))
SECRETS = "/home/box/.desk_secrets"
DEFAULTS = {
 "desk": {"mode": "PAPER", "paper_start_stack": 200.0, "stack": 200.0, "fomo_handle": "krytix102",
          "analyst_mode": "rules", "notifier": "telegram", "report_time": "08:00", "push_rejects": False,
          "publish_every_s": 360, "live_every_s": 5, "daily_loss_usd": 0, "cash_reserve_usd": 0, "legacy_pipeline": False},
 "scanner": {"min_liquidity_usd": 40000, "max_top10_pct": 25, "max_h1_change_pct": 80, "poll_s": 60,
             "min_pool_age_min": 10, "max_wait_min": 60, "runner_h24_pct": 300},
 "auditor": {"enabled": True, "max_dev_pct": 5, "max_top10_pct": 25, "min_lp_locked_pct": 99, "min_pool_age_min": 10,
             "max_bundled": 5, "max_delegated_accounts": 10, "retries": 3},
 "analyst": {"alive_volume_h1": 25000, "organic_buys_h1": 150, "bundled_penalty": 0.5, "bundled_penalty_from": 3,
             "flush_drop_m5_pct": 5, "chase_h1_pct": 80, "min_alive_6h": 0.70, "min_organic": 0.65, "max_chase": 0.30, "min_score": 70},
 "executor": {"stack_pct": 8, "max_pool_pct": 2, "min_ticket_usd": 47.50, "below_min": "bump",
              "recheck_liq_drop_pct": 20, "recheck_price_up_pct": 15, "confirm_window_min": 5, "ticket_usd": 0, "max_open": 0, "tier_enabled": False, "tier_base_usd": 100, "tier_strong_usd": 250, "tier_top_usd": 1000, "tier_strong_mom": 75, "tier_top_mom": 85, "tier_max_equity_pct": 25},
 "volume": {"enabled": True, "kill_below": 0.25, "penalty_below": 0.5, "penalty": 15},
 "wallets": {"enabled": True, "bonus": 8, "max_bonus": 16, "min_coins": 2, "min_win_rate": 0.5, "max_tracked": 25, "poll_s": 120},
 "social": {"enabled": True, "bad_penalty": 10},
 "learning": {"enabled": True, "every_trades": 10, "min_trades": 5},
 "prefilter": {"enabled": True, "max_dev_buy_pct": 10, "promote_progress": 50, "dump_pts": 20, "watch_max_min": 45, "poll_s": 20},
 "momentum": {"enabled": True, "min_score": 60, "max_from_high_pct": 25, "dump_pct": 12, "wick_pct": 40},
 "rate": {"rpc_rps": 8, "rugcheck_per_min": 50, "scan_workers": 6, "audit_workers": 3, "research_rpc_per_min": 30},
 "copy": {"enabled": True, "top_n": 10, "tp_pct": 15, "two_step": False, "tp2_pct": 20, "follow_sell": True, "max_open": 0, "min_buy_sol": 0.01, "size_pct": 2.5, "ticket_usd": 0, "hard_rug_only": True, "trail_after_tp": False, "refresh_h": 3, "candidates": 40, "min_closed": 4, "min_win_rate": 0.5, "max_trades_day": 120, "poll_s": 8, "daily_cap": 0, "big_buy_sol": 1.0, "med_mult": 2.0, "window_min": 30, "conviction_usd": 0},
 "risk": {"poll_s": 2.5, "stop_pct": 6, "trail_start_pct": 20, "trail_pct": 12, "timeout_h": 3, "timeout_min_gain_pct": 5, "confirm_window_min": 2, "tp1_pct": 0, "breakeven_after_tp": False, "flat_min": 0, "flat_pct": 3},
 "winners": {"enabled": True, "poll_s": 1200, "min_mult": 10, "min_mc_usd": 500000, "max_age_h": 24, "early_window_s": 30, "early_n": 20, "min_cluster_score": 60, "alert_min_score": 70, "max_analyze_per_cycle": 10, "launch_mc_usd": 5500, "top_n": 10, "deep_pages": 15},
 "gems": {"enabled": True, "poll_s": 120, "min_mc_usd": 5000, "max_mc_usd": 150000, "min_score": 70, "alert_min_score": 75, "alert_cooldown_h": 6, "require_pump_origin": True, "require_lp_safe": True, "lp_locked_min_pct": 99, "rug_liq_drop_pct": 80, "watch_every_s": 180},
}
LIMITS = {  # (min, max) for numeric settings
 "max_dev_buy_pct": (0, 100), "promote_progress": (1, 99), "dump_pts": (1, 100), "watch_max_min": (5, 240),
 "rpc_rps": (1, 200), "rugcheck_per_min": (5, 600), "scan_workers": (1, 16), "audit_workers": (1, 8), "research_rpc_per_min": (1, 120), "min_mult": (2, 1000), "min_mc_usd": (100, 1e9), "max_mc_usd": (100, 1e9), "max_age_h": (1, 168), "early_window_s": (5, 600), "early_n": (3, 100), "min_cluster_score": (0, 100), "alert_min_score": (0, 100), "max_analyze_per_cycle": (1, 20), "top_n": (1, 25), "deep_pages": (2, 80), "launch_mc_usd": (100, 1e6), "alert_cooldown_h": (1, 72), "rug_liq_drop_pct": (20, 99), "watch_every_s": (30, 3600),
 "paper_start_stack": (10, 1e7), "stack": (10, 1e7), "min_liquidity_usd": (1000, 1e7), "max_top10_pct": (1, 100), "max_h1_change_pct": (1, 10000),
 "poll_s": (5, 3600), "min_pool_age_min": (0, 1440), "max_wait_min": (1, 1440), "runner_h24_pct": (10, 1e5), "max_dev_pct": (0, 100),
 "min_lp_locked_pct": (0, 100), "max_bundled": (1, 1000), "max_delegated_accounts": (1, 1e5), "retries": (1, 10),
 "alive_volume_h1": (1, 1e9), "organic_buys_h1": (1, 1e6), "bundled_penalty": (0, 1), "bundled_penalty_from": (1, 1000), "flush_drop_m5_pct": (0, 100),
 "chase_h1_pct": (1, 10000), "min_alive_6h": (0, 1), "min_organic": (0, 1), "max_chase": (0, 1), "min_score": (0, 100),
 "stack_pct": (0.1, 100), "max_pool_pct": (0.01, 100), "min_ticket_usd": (0, 1e6), "recheck_liq_drop_pct": (0, 100), "recheck_price_up_pct": (0, 1000),
 "confirm_window_min": (0.5, 120), "stop_pct": (0.5, 90), "trail_start_pct": (0.5, 1000), "trail_pct": (0.5, 90), "timeout_h": (0.1, 72),
 "timeout_min_gain_pct": (-100, 1000), "publish_every_s": (60, 86400),
 "kill_below": (0, 0.9), "penalty_below": (0, 0.95), "penalty": (0, 50), "bonus": (0, 30), "max_bonus": (0, 40), "min_coins": (1, 50),
 "min_win_rate": (0, 1), "max_tracked": (1, 200), "bad_penalty": (0, 50), "every_trades": (1, 1000), "min_trades": (1, 1000),
}
CHOICES = {"mode": ("PAPER", "LIVE"), "analyst_mode": ("rules", "jev"), "below_min": ("bump", "skip"), "notifier": ("telegram",)}
ALIAS = {  # internal flat key -> (section, name)
 "mode":("desk","mode"),"paper_balance":("desk","paper_start_stack"),"stack":("desk","stack"),"fomo_handle":("desk","fomo_handle"),
 "analyst_mode":("desk","analyst_mode"),"publish_every_s":("desk","publish_every_s"),"live_every_s":("desk","live_every_s"),"push_rejects":("desk","push_rejects"),"daily_loss_usd":("desk","daily_loss_usd"),"cash_reserve_usd":("desk","cash_reserve_usd"),"death_liq_drop_pct":("risk","death_liq_drop_pct"),"mom_enabled":("momentum","enabled"),"mom_min_score":("momentum","min_score"),"mom_max_from_high_pct":("momentum","max_from_high_pct"),"mom_dump_pct":("momentum","dump_pct"),"mom_wick_pct":("momentum","wick_pct"),"desk_ticket_usd":("executor","ticket_usd"),"desk_max_open":("executor","max_open"),"desk_niepewny_mom":("executor","niepewny_mom"),"tier_enabled":("executor","tier_enabled"),"tier_base_usd":("executor","tier_base_usd"),"tier_strong_usd":("executor","tier_strong_usd"),"tier_top_usd":("executor","tier_top_usd"),"tier_strong_mom":("executor","tier_strong_mom"),"tier_top_mom":("executor","tier_top_mom"),"tier_max_equity_pct":("executor","tier_max_equity_pct"),"copy_trail_after_tp":("copy","trail_after_tp"),"tp1_pct":("risk","tp1_pct"),"breakeven_after_tp":("risk","breakeven_after_tp"),"flat_min":("risk","flat_min"),"flat_pct":("risk","flat_pct"),"report_time":("desk","report_time"),
 "scan_poll_s":("scanner","poll_s"),"min_liq_usd":("scanner","min_liquidity_usd"),"max_top10_pct":("scanner","max_top10_pct"),
 "max_h1_pct":("scanner","max_h1_change_pct"),"min_pool_age_min":("scanner","min_pool_age_min"),"waiting_max_min":("scanner","max_wait_min"),
 "runner_h24_pct":("scanner","runner_h24_pct"),
 "audit_enabled":("auditor","enabled"),"dev_max_pct":("auditor","max_dev_pct"),"audit_top10_pct":("auditor","max_top10_pct"),
 "lp_locked_min_pct":("auditor","min_lp_locked_pct"),"audit_pool_age_min":("auditor","min_pool_age_min"),"bundle_max":("auditor","max_bundled"),
 "delegate_max_accounts":("auditor","max_delegated_accounts"),"src_retries":("auditor","retries"),
 "rules_vol_h1":("analyst","alive_volume_h1"),"rules_buys_h1":("analyst","organic_buys_h1"),"rules_bundle_penalty":("analyst","bundled_penalty"),
 "rules_bundle_min":("analyst","bundled_penalty_from"),"rules_flush_pct":("analyst","flush_drop_m5_pct"),"rules_chase_h1":("analyst","chase_h1_pct"),
 "alive_min":("analyst","min_alive_6h"),"organic_min":("analyst","min_organic"),"chase_max":("analyst","max_chase"),"score_min":("analyst","min_score"),
 "size_pct":("executor","stack_pct"),"size_liq_pct":("executor","max_pool_pct"),"min_size_usd":("executor","min_ticket_usd"),"below_min":("executor","below_min"),
 "recheck_liq_drop_pct":("executor","recheck_liq_drop_pct"),"recheck_price_up_pct":("executor","recheck_price_up_pct"),"exec_confirm_min":("executor","confirm_window_min"),
 "vq_enabled":("volume","enabled"),"vq_kill":("volume","kill_below"),"vq_pen_below":("volume","penalty_below"),"vq_penalty":("volume","penalty"),
 "w_enabled":("wallets","enabled"),"w_bonus":("wallets","bonus"),"w_max_bonus":("wallets","max_bonus"),"w_min_coins":("wallets","min_coins"),
 "w_min_wr":("wallets","min_win_rate"),"w_max":("wallets","max_tracked"),"w_poll_s":("wallets","poll_s"),
 "soc_enabled":("social","enabled"),"soc_bad_penalty":("social","bad_penalty"),
 "learn_enabled":("learning","enabled"),"learn_every":("learning","every_trades"),"learn_min":("learning","min_trades"),
 "pre_enabled":("prefilter","enabled"),"pre_max_dev_buy_pct":("prefilter","max_dev_buy_pct"),"pre_promote_progress":("prefilter","promote_progress"),
 "pre_dump_pts":("prefilter","dump_pts"),"pre_watch_max_min":("prefilter","watch_max_min"),"pre_poll_s":("prefilter","poll_s"),
 "rpc_rps":("rate","rpc_rps"),"rc_per_min":("rate","rugcheck_per_min"),"scan_workers":("rate","scan_workers"),"audit_workers":("rate","audit_workers"),
 "copy_enabled":("copy","enabled"),"copy_top_n":("copy","top_n"),"copy_tp_pct":("copy","tp_pct"),"copy_two_step":("copy","two_step"),"copy_tp2_pct":("copy","tp2_pct"),
 "copy_follow_sell":("copy","follow_sell"),"copy_max_open":("copy","max_open"),"copy_ticket_usd": ("copy", "ticket_usd"), "copy_size_pct":("copy","size_pct"),"copy_hard_rug_only":("copy","hard_rug_only"),"copy_min_buy_sol":("copy","min_buy_sol"),"copy_refresh_h":("copy","refresh_h"),
 "copy_candidates":("copy","candidates"),"copy_min_closed":("copy","min_closed"),"copy_min_wr":("copy","min_win_rate"),"copy_max_trades_day":("copy","max_trades_day"),"copy_poll_s":("copy","poll_s"),"copy_daily_cap":("copy","daily_cap"),"copy_big_buy_sol":("copy","big_buy_sol"),"copy_med_mult":("copy","med_mult"),"copy_window_min":("copy","window_min"),"copy_conviction_usd":("copy","conviction_usd"),
 "risk_poll_s":("risk","poll_s"),"stop_pct":("risk","stop_pct"),"trail_on_pct":("risk","trail_start_pct"),"trail_pct":("risk","trail_pct"),
 "timeout_h":("risk","timeout_h"),"timeout_min_gain_pct":("risk","timeout_min_gain_pct"),"sold_reping_min":("risk","confirm_window_min"),
 "research_rpc_per_min":("rate","research_rpc_per_min"),
 "win_enabled":("winners","enabled"),"win_poll_s":("winners","poll_s"),"win_min_mult":("winners","min_mult"),"win_min_mc_usd":("winners","min_mc_usd"),
 "win_max_age_h":("winners","max_age_h"),"win_early_window_s":("winners","early_window_s"),"win_early_n":("winners","early_n"),
 "win_min_cluster_score":("winners","min_cluster_score"),"win_alert_min_score":("winners","alert_min_score"),
 "win_max_analyze":("winners","max_analyze_per_cycle"),"win_launch_mc_usd":("winners","launch_mc_usd"),"win_top_n":("winners","top_n"),"win_deep_pages":("winners","deep_pages"),"legacy_pipeline":("desk","legacy_pipeline"),
 "gems_enabled":("gems","enabled"),"gems_poll_s":("gems","poll_s"),"gems_min_mc_usd":("gems","min_mc_usd"),"gems_max_mc_usd":("gems","max_mc_usd"),
 "gems_min_score":("gems","min_score"),"gems_alert_min_score":("gems","alert_min_score"),"gems_alert_cooldown_h":("gems","alert_cooldown_h"),"gems_require_pump_origin":("gems","require_pump_origin"),"gems_require_lp_safe":("gems","require_lp_safe"),"gems_lp_locked_min_pct":("gems","lp_locked_min_pct"),"gems_rug_liq_drop_pct":("gems","rug_liq_drop_pct"),"gems_watch_every_s":("gems","watch_every_s"),
}
FIXED = {"dex_max_rpm": 250, "ws_silent_s": 120, "dex_429_wait_s": 30, "liq_drop_max_pct": 50, "src_retry_wait_s": 2,
         "state_max_chars": 3000, "jev_timeout_s": 10, "no_price_polls": 4, "risk_429_wait_s": 30}
PATH = os.path.join(BASE, "settings.json")
def nested():
    n = json.loads(json.dumps(DEFAULTS))
    if os.path.exists(PATH):
        for sec, kv in json.load(open(PATH)).items():
            if sec in n and isinstance(kv, dict): n[sec].update(kv)
    return n
def load():
    n = nested(); s = dict(FIXED)
    for k, (sec, name) in ALIAS.items(): s[k] = n[sec][name]
    s["rpc_url"] = os.environ.get("DESK_RPC_URL") or "https://api.mainnet-beta.solana.com"
    s["_n"] = n; return s
def find(name):
    """'stop' / 'stop_pct' / 'risk.stop_pct' -> (section, key)."""
    name = name.strip().lower().replace(" ", "_")
    if "." in name: sec, k = name.split(".", 1); return (sec, k) if k in DEFAULTS.get(sec, {}) else None
    for cand in (name, name + "_pct"):
        hits = [(sec, cand) for sec in ("risk", "executor", "analyst", "scanner", "auditor", "desk", "volume", "wallets", "social", "learning", "prefilter", "rate", "copy", "winners", "gems", "momentum") if cand in DEFAULTS[sec]]
        if hits: return hits[0]
    return None
def set_value(sec, key, raw, who="telegram"):
    """Validate + persist + change_log. Returns (old, new) or raises ValueError."""
    n = nested(); old = n[sec][key]
    if isinstance(DEFAULTS[sec][key], bool): new = str(raw).lower() in ("1", "true", "on", "yes")
    elif isinstance(DEFAULTS[sec][key], (int, float)):
        new = abs(float(str(raw).strip().rstrip("%").replace("$", ""))) if key in ("stop_pct",) else float(str(raw).strip().rstrip("%").replace("$", ""))
        lo, hi = LIMITS.get(key, (-1e12, 1e12))
        if not lo <= new <= hi: raise ValueError(f"{key} must be between {lo:g} and {hi:g}")
        if isinstance(DEFAULTS[sec][key], int) and new == int(new): new = int(new)
    else:
        new = str(raw).strip(); ch = CHOICES.get(key)
        if key == "mode": new = new.upper()
        if ch and new not in ch: raise ValueError(f"{key} must be one of {', '.join(ch)}")
    cur = json.load(open(PATH)) if os.path.exists(PATH) else {}
    cur.setdefault(sec, {})[key] = new; json.dump(cur, open(PATH, "w"), indent=1)
    import db; db.q("insert into change_log(ts,section,key,old,new,who) values(?,?,?,?,?,?)",
                    (time.strftime("%Y-%m-%dT%H:%M:%S"), sec, key, json.dumps(old), json.dumps(new), who))
    return old, new
def save(s):  # legacy flat save (used by old code paths)
    for k, (sec, name) in ALIAS.items():
        if k in s and s[k] != nested()[sec][name]: set_value(sec, name, s[k], "code")
def secret(name):
    p = os.path.join(SECRETS, name)
    return open(p).read().strip() if os.path.exists(p) else os.environ.get(name)
def redact(text):
    u = os.environ.get("DESK_RPC_URL")
    return str(text).replace(u, "[rpc]") if u else str(text)
