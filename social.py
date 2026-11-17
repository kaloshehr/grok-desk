"""Social check: socials from DexScreener pair info; X via public fxtwitter mirror (no user credentials);
Telegram via public t.me preview page; website reachability. Every source fails soft."""
import re, time, requests, util
log = util.logger("analyst")
UA = {"User-Agent": "Mozilla/5.0 (desk)"}
def _x_handle(u):
    m = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})(?:[/?#]|$)", u or "")
    return m.group(1) if m and m.group(1).lower() not in ("i", "home", "search", "intent", "share") else None
def x_info(h):
    try:
        r = requests.get(f"https://api.fxtwitter.com/{h}", headers=UA, timeout=8); j = r.json()
        if j.get("code") != 200: return {"handle": h, "ok": None, "note": "profile not readable via public mirror"}
        u = j["user"]; joined = u.get("joined")
        age_d = None
        if joined:
            try: age_d = int((time.time() - time.mktime(time.strptime(joined, "%a %b %d %H:%M:%S +0000 %Y"))) / 86400)
            except Exception: pass
        return {"handle": h, "ok": True, "followers": u.get("followers"), "tweets": u.get("tweets"), "age_days": age_d}
    except Exception as e: return {"handle": h, "ok": None, "note": f"unavailable ({type(e).__name__})"}
def tg_info(u):
    m = re.search(r"t\.me/([A-Za-z0-9_]{4,})", u or "")
    if not m: return None
    name = m.group(1)
    try:
        h = requests.get(f"https://t.me/{name}", headers=UA, timeout=8).text
        x = re.search(r'tgme_page_extra">([^<]+)', h)
        if not x: return {"name": name, "ok": False, "note": "no public group/channel"}
        num = re.match(r"([\d\s]+)\s+(members|subscribers)", x.group(1))
        n = int(num.group(1).replace(" ", "")) if num else None
        on = re.search(r"([\d\s]+) online", x.group(1))
        return {"name": name, "ok": True, "members": n, "online": int(on.group(1).replace(" ", "")) if on else None}
    except Exception as e: return {"name": name, "ok": None, "note": f"unavailable ({type(e).__name__})"}
def site_info(u):
    try:
        r = requests.get(u, headers=UA, timeout=8, allow_redirects=True)
        return {"url": u, "ok": r.status_code < 400, "status": r.status_code}
    except Exception as e: return {"url": u, "ok": False, "note": type(e).__name__}
def check(pair):
    info = (pair or {}).get("info") or {}
    soc = info.get("socials") or []; webs = info.get("websites") or []
    out = {"x": None, "telegram": None, "website": None, "listed": bool(soc or webs)}
    xs = next((s["url"] for s in soc if s.get("type") in ("twitter", "x")), None)
    if xs and _x_handle(xs): out["x"] = x_info(_x_handle(xs))
    ts = next((s["url"] for s in soc if s.get("type") == "telegram"), None)
    if ts: out["telegram"] = tg_info(ts)
    if webs: out["website"] = site_info(webs[0].get("url"))
    return out
