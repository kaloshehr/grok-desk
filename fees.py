"""FOMO Solana fee per side. Source: help.fomo.family/en/articles/14436214 (checked 2026-11-03).
Lowest route: <5 -> 0.10; 5-47.50 -> 2%; 47.50-190 -> 0.95; >=190 -> 0.50%.
Other routes: 0.50% min 0.95. PAPER uses the WORSE of the two (full rate, route not known)."""
def lowest_route(usd):
    if usd < 5: return 0.10
    if usd < 47.50: return usd * 0.02
    if usd < 190: return 0.95
    return usd * 0.005
def other_route(usd): return max(0.95, usd * 0.005)
def fee(usd): return round(max(lowest_route(usd), other_route(usd)), 4)
