"""Global entry guard: daily loss limit (Warsaw day) and cash reserve. Exits are never blocked."""
import time, datetime, db, notifier
try:
    from zoneinfo import ZoneInfo; TZ = ZoneInfo("Europe/Warsaw")
except Exception: TZ = None
def day_start():
    n = datetime.datetime.now(TZ); return n.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
def day_pnl():
    r = db.q("select coalesce(sum(pnl),0) p from positions where status='closed' and closed_at>=?", (day_start(),), True); return float(r["p"] or 0)
def block(s, size, free):
    """-> reason string if a new position must not open, else None."""
    lim = s.get("daily_loss_usd") or 0
    if lim:
        p = day_pnl()
        if p <= -lim:
            k = f"dll_alert:{time.strftime('%Y-%m-%d')}"
            if not db.kv_get(k):
                db.kv_set(k, 1)
                notifier.send(("PAPER · " if s["mode"] == "PAPER" else "") + f"Dzienny limit straty: {p:.2f}$ ≤ -{lim:.0f}$. Nie otwieram nowych pozycji do północy, wyjścia działają dalej.")
            return f"daily loss limit ({p:.2f}$ ≤ -{lim:.0f}$)"
    res = s.get("cash_reserve_usd") or 0
    if free - size < res: return f"no cash: free ${free:.2f} - ticket ${size:.2f} < reserve ${res:.0f}"
    return None
