"""Rules-mode 'AI' report: DOBRY / ZŁY / NIEPEWNY + 3-5 Polish bullets. Same inputs every time -> same verdict."""
def _k(n):
    return f"{n/1e6:.1f}M" if n >= 1e6 else f"{n/1e3:.1f}K" if n >= 1e3 else str(n)
def build(score, vq, vsig, soc, backers, s):
    pts = 0; hard_bad = False; good = []; bad = []; meh = []
    if vq is None: meh.append("Wolumen: za mało transakcji, żeby ocenić, czy jest prawdziwy")
    elif vq < s["vq_kill"]: hard_bad = True; bad.append(f"Wolumen wygląda na sztuczny (jakość {vq:.2f}: {vsig.get('unique_wallets')} portfeli na {vsig.get('trades')} transakcji)")
    elif vq < s["vq_pen_below"]: pts -= 2; bad.append(f"Podejrzany wolumen (jakość {vq:.2f}, top5 portfeli = {int(100*vsig.get('top5_wallet_share',0))}% transakcji)")
    elif vq >= 0.7: pts += 1; good.append(f"Wolumen organiczny (jakość {vq:.2f}, {vsig.get('unique_wallets')} różnych portfeli na {vsig.get('trades')} transakcji)")
    else: meh.append(f"Wolumen przeciętny (jakość {vq:.2f})")
    soc = soc or {}
    if soc and not soc.get("listed"): pts -= 2; bad.append("Brak jakichkolwiek linków społeczności (X, Telegram, strona)")
    x = soc.get("x")
    if x:
        if x.get("ok"):
            f = x.get("followers") or 0
            age = f", konto ma {x['age_days']} dni" if x.get("age_days") is not None else ""
            if f >= 1000: pts += 1; good.append(f"X @{x['handle']}: {_k(f)} obserwujących{age}")
            elif f < 100: pts -= 1; bad.append(f"X @{x['handle']}: tylko {f} obserwujących{age}")
            else: meh.append(f"X @{x['handle']}: {f} obserwujących{age}")
        else: meh.append(f"X @{x['handle']}: nie udało się odczytać profilu publicznie")
    elif soc.get("listed"): meh.append("Brak konta X")
    t = soc.get("telegram")
    if t:
        if t.get("ok") and t.get("members") is not None:
            m = t["members"]
            if m >= 500: pts += 1; good.append(f"Telegram: {_k(m)} członków")
            elif m < 50: pts -= 1; bad.append(f"Telegram: tylko {m} członków")
            else: meh.append(f"Telegram: {m} członków")
        else: pts -= 1 if t.get("ok") is False else 0; (bad if t.get("ok") is False else meh).append(f"Telegram t.me/{t['name']}: grupa nie istnieje lub niepubliczna" if t.get("ok") is False else "Telegram: nie do odczytania")
    w = soc.get("website")
    if w:
        if w.get("ok"): pts += 1; good.append("Strona internetowa działa")
        else: pts -= 1; bad.append("Strona internetowa nie działa")
    if backers: pts += min(2, len(backers)); good.append(f"{len(backers)} śledzony(ch) smart-portfel(i) kupiło w ostatnich 6h")
    if score is not None:
        if score >= 75: pts += 1; good.append(f"Analityk: wysoki wynik {score}/100")
        elif score < 55: pts -= 1; bad.append(f"Analityk: niski wynik {score}/100")
    verdict = "ZŁY" if hard_bad or pts <= -1 else "DOBRY" if pts >= 3 and not bad else "NIEPEWNY"
    order = (bad + good + meh) if verdict == "ZŁY" else (good + bad + meh)
    bullets = order[:5]
    if len(bullets) < 3: bullets += ["Audytor: wszystkie 8 warstw anty-rug OK", "Tryb PAPER: to sygnał, nie porada inwestycyjna"][:3 - len(bullets)]
    return {"verdict": verdict, "points": pts, "bullets": bullets, "mode": "rules"}
