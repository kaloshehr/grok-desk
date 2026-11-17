"""Fake / wash volume detection from the last ~300 pool trades (GeckoTerminal) + DexScreener pair stats.
Returns quality 0..1 (1 = organic) and the signals behind it."""
import collections
def quality(tr, pair):
    g = lambda *k: (lambda v: v if v is not None else 0)(__import__("util").get(pair, ".".join(k)))
    sig = {}; pen = 0.0
    n = len(tr)
    if n < 20: return None, {"note": f"only {n} trades"}
    makers = collections.Counter(t["wallet"] for t in tr if t["wallet"])
    uniq = len(makers); sig["trades"] = n; sig["unique_wallets"] = uniq
    ratio = uniq / n; sig["unique_ratio"] = round(ratio, 2)
    if ratio < 0.15: pen += 0.35
    elif ratio < 0.3: pen += 0.2
    top5 = sum(c for _, c in makers.most_common(5)) / n; sig["top5_wallet_share"] = round(top5, 2)
    if top5 > 0.5: pen += 0.25
    elif top5 > 0.3: pen += 0.1
    sizes = collections.Counter(round(t["usd"], 1) for t in tr if t["usd"] >= 5)
    rep = (sizes.most_common(1)[0][1] / n) if sizes else 0; sig["same_size_share"] = round(rep, 2)
    if rep > 0.15: pen += 0.2
    elif rep > 0.08: pen += 0.1
    both = sum(1 for w in makers if any(t["wallet"] == w and t["kind"] == "buy" for t in tr) and any(t["wallet"] == w and t["kind"] == "sell" for t in tr))
    rt = both / uniq if uniq else 0; sig["roundtrip_wallets"] = round(rt, 2)
    if rt > 0.5: pen += 0.15
    liq = g("liquidity", "usd"); vol = g("volume", "h1")
    vl = vol / liq if liq else 0; sig["vol_h1_to_liq"] = round(vl, 1)
    if vl > 20: pen += 0.2
    elif vl > 8: pen += 0.1
    return round(max(0.0, 1 - pen), 2), sig
