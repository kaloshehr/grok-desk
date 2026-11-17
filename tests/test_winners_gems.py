"""Isolated scoring tests. No Telegram, no RPC, no live desk loop."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

class WinnersScoring(unittest.TestCase):
    def test_is_pump(self):
        from agents.winners import is_pump
        self.assertTrue(is_pump("Dfid5ax62osiWxSQS6gpZwRKtZAw32ktxuTPG47Upump"))
        self.assertTrue(is_pump("Abc123", {"dexId": "pumpswap"}))
        self.assertTrue(is_pump("Abc123", None, "My Pump Token"))
        self.assertFalse(is_pump("EPjFWdd5AufqSSqeM2qJxNZoBmBFNdjYddw4ZQwKYsjv", {"dexId": "raydium"}, "USDC"))

    def test_is_winner(self):
        from agents.winners import is_winner
        s = {"win_max_age_h": 24, "win_min_mult": 10, "win_min_mc_usd": 500000, "win_launch_mc_usd": 5500}
        ok, m = is_winner(55000, None, 6, s)  # 10x from 5.5k
        self.assertTrue(ok); self.assertGreaterEqual(m, 10)
        ok, m = is_winner(600000, 50, 8, s)  # MC gate
        self.assertTrue(ok)
        ok, _ = is_winner(20000, 50, 6, s)
        self.assertFalse(ok)
        ok, _ = is_winner(2_000_000, 2000, 48, s)  # too old
        self.assertFalse(ok)
        ok, m = is_winner(20000, 900, 3, s)  # 10x via price change
        self.assertTrue(ok); self.assertGreaterEqual(m, 10)

    def test_cluster_score(self):
        from agents.winners import cluster_score
        good = cluster_score(wins=4, rugs=0, launches=2, snipes=4, avg_hold_s=600)
        dump = cluster_score(wins=1, rugs=5, launches=6, snipes=0, avg_hold_s=10)
        single = cluster_score(wins=1, rugs=0, launches=1, snipes=0, avg_hold_s=None)
        self.assertGreaterEqual(good, 70)
        self.assertLess(dump, 40)
        self.assertLess(single, good)
        self.assertTrue(0 <= dump <= 100 and 0 <= good <= 100)

class GemsScoring(unittest.TestCase):
    def test_in_range_breakout(self):
        from agents.gems import gem_score
        sc, why = gem_score({
            "mc": 25000, "min_mc": 5000, "max_mc": 150000,
            "vol_acc": 3.2, "buy_share": 0.8, "chg_m5": 12, "chg_h1": 40,
            "liq": 8000, "curve_prog": 62, "mint_revoked": True, "freeze_revoked": True,
            "top10": 22, "dev_pct": 3, "bundles": 1,
        })
        self.assertGreaterEqual(sc, 75)
        self.assertTrue(any("vol accel" in w for w in why))

    def test_out_of_range_and_penalties(self):
        from agents.gems import gem_score
        sc, why = gem_score({"mc": 4000, "min_mc": 5000, "max_mc": 150000})
        self.assertEqual(sc, 0)
        sc, why = gem_score({
            "mc": 80000, "min_mc": 5000, "max_mc": 150000,
            "vol_acc": 0.4, "buy_share": 0.3, "chg_h1": 500,
            "liq": 400, "mint_revoked": False, "freeze_revoked": False,
            "top10": 70, "dev_pct": 20, "bundles": 8,
        })
        self.assertLess(sc, 40)
        self.assertTrue(any("mint live" in w for w in why))

    def test_cluster_bonus(self):
        from agents.gems import gem_score
        base, _ = gem_score({"mc": 20000, "min_mc": 5000, "max_mc": 150000, "vol_acc": 1.6, "buy_share": 0.65})
        plus, why = gem_score({"mc": 20000, "min_mc": 5000, "max_mc": 150000, "vol_acc": 1.6, "buy_share": 0.65, "cluster_score": 80})
        self.assertEqual(plus - base, 15)
        self.assertTrue(any("cluster" in w for w in why))

    def test_high_dev_capped(self):
        from agents.gems import gem_score
        sc, why = gem_score({
            "mc": 15727, "min_mc": 5000, "max_mc": 150000,
            "vol_acc": 12, "buy_share": 0.82, "chg_m5": 18, "chg_h1": 40,
            "liq": 8000, "just_graduated": True, "mint_revoked": True, "freeze_revoked": True,
            "top10": 91, "dev_pct": 91, "bundles": 0,
        })
        self.assertLessEqual(sc, 45)
        self.assertTrue(any("dev 91%" in w for w in why))


class SafetyRules(unittest.TestCase):
    def test_pump_origin_and_graduation(self):
        from agents.gems import is_pump_origin, is_true_graduation
        pump = "GPHYkVzPBjqt3yDaB1SeLgi2mX21WzccPvGJa76cpump"
        other = "HSXiByeLx2TUspPzL8GuF3XbTBiv2UKaALw4USdBn9Ce"
        self.assertTrue(is_pump_origin(pump))
        self.assertFalse(is_pump_origin(other))
        self.assertTrue(is_pump_origin(other, launchpad={"name": "Pump.Fun", "platform": "pump_fun"}))
        now = 1_000_000
        p = {"dexId": "pumpswap", "pairCreatedAt": (now - 600) * 1000}
        self.assertTrue(is_true_graduation(pump, p, now=now))
        self.assertFalse(is_true_graduation(other, p, now=now))  # not pump origin
        self.assertFalse(is_true_graduation(pump, {"dexId": "meteora", "pairCreatedAt": (now-60)*1000}, now=now))
        self.assertFalse(is_true_graduation(pump, {"dexId": "pumpswap", "pairCreatedAt": (now-4000)*1000}, now=now))

    def test_lp_safety(self):
        from agents.gems import lp_safety
        pump = "xxxxpump"
        safe, why, _ = lp_safety(pump, {"dexId": "pumpswap", "pairAddress": "P"},
                                 {"markets": [{"pubkey": "P", "marketType": "pump_fun_amm", "lp": {"lpLockedPct": 100}}]})
        self.assertTrue(safe)
        unsafe, why, locked = lp_safety("nope", {"dexId": "meteora", "labels": ["DYN2"], "pairAddress": "M"},
                                        {"markets": [{"pubkey": "M", "marketType": "meteora_damm_v2", "lp": {"lpLockedPct": 0}}]})
        self.assertFalse(unsafe)
        self.assertIn("Meteora", why)
        # unlocked pumpswap that is NOT a pump.fun mint
        fake, why, _ = lp_safety("notpump", {"dexId": "pumpswap", "pairAddress": "X"},
                                 {"markets": [{"pubkey": "X", "marketType": "pump_fun_amm", "lp": {"lpLockedPct": 0}}]})
        self.assertFalse(fake)
        # meteora but verifiably locked
        locked_ok, _, pct = lp_safety("nope", {"dexId": "meteora", "labels": ["DYN2"], "pairAddress": "L"},
                                      {"markets": [{"pubkey": "L", "marketType": "meteora_damm_v2", "lp": {"lpLockedPct": 100}}]})
        self.assertTrue(locked_ok)
        # still on the pump.fun curve is not "LP locked", even if rugcheck reports 100
        curve, why, _ = lp_safety("xxxxpump", {"dexId": "pumpfun"},
                                  {"markets": [{"lp": {"lpLockedPct": 100}}]})
        self.assertIsNone(curve)
        self.assertIn("curve", why.lower())

    def test_score_caps_unlocked_and_rugs(self):
        from agents.gems import gem_score, blocked
        # Larp-like: looks hot but Meteora unlocked, not pump origin
        sc, why = gem_score({
            "mc": 14962, "min_mc": 5000, "max_mc": 150000,
            "vol_acc": 12, "buy_share": 0.64, "chg_m5": 12, "chg_h1": 40,
            "liq": 26909, "just_graduated": False, "mint_revoked": True, "freeze_revoked": True,
            "top10": 3, "pump_origin": False, "lp_safe": False, "lp_why": "Meteora unlocked LP",
        })
        self.assertLessEqual(sc, 40)
        self.assertTrue(any("Meteora" in w or "not pump" in w for w in why))
        self.assertEqual(blocked({"pump_origin": False, "lp_safe": False, "mint_revoked": True}), "not pump.fun origin")
        self.assertEqual(blocked({"pump_origin": True, "lp_safe": False, "mint_revoked": True}), "LP not safe")
        self.assertIsNone(blocked({"pump_origin": True, "lp_safe": True, "mint_revoked": True, "dev_pct": 2}))
        sc, why = gem_score({
            "mc": 15000, "min_mc": 5000, "max_mc": 150000, "liq": 8000,
            "pump_origin": True, "lp_safe": True, "lp_why": "PumpSwap canonical",
            "just_graduated": True, "mint_revoked": True, "freeze_revoked": True, "rugged": True,
        })
        self.assertLessEqual(sc, 10)
        self.assertTrue(any("rugged" in w for w in why))

    def test_youtube_like_still_passes(self):
        from agents.gems import gem_score, blocked
        sc, why = gem_score({
            "mc": 45000, "min_mc": 5000, "max_mc": 150000,
            "chg_m5": 24, "chg_h1": 40, "liq": 15000, "vol_acc": 0.9, "buy_share": 0.54,
            "just_graduated": True, "mint_revoked": True, "freeze_revoked": True,
            "dev_pct": 2, "top10": 36, "pump_origin": True, "lp_safe": True, "lp_why": "PumpSwap canonical",
        })
        self.assertGreaterEqual(sc, 75)
        self.assertIsNone(blocked({"pump_origin": True, "lp_safe": True, "mint_revoked": True, "dev_pct": 2}))



class TopTen(unittest.TestCase):
    def test_rank_key(self):
        from agents.winners import rank_key
        rows = [
            {"name": "a", "max_mult": 10, "ath_mc": 2_000_000},
            {"name": "b", "max_mult": 300, "ath_mc": 1_000_000},
            {"name": "c", "max_mult": 300, "ath_mc": 1_700_000},
        ]
        ranked = sorted(rows, key=rank_key, reverse=True)
        self.assertEqual([r["name"] for r in ranked], ["c", "b", "a"])

if __name__ == "__main__":
    unittest.main()
