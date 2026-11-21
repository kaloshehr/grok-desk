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
        # launchpad / PumpSwap name is not enough (Flame Ingo-style non-pump suffix)
        self.assertFalse(is_pump_origin(other, launchpad={"name": "Pump.Fun", "platform": "pump_fun"}))
        self.assertFalse(is_pump_origin(other, {"dexId": "pumpswap"}, "PumpSwap"))
        self.assertTrue(is_pump_origin(other, {"dexId": "pumpfun"}))  # still on the curve
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
        self.assertIsNone(blocked({"pump_origin": True, "lp_safe": True, "mint_revoked": True, "dev_pct": 2, "liq": 15000, "chg_m5": 24}))

    def test_new_quality_blocks(self):
        from agents.gems import blocked
        s = {"gems_require_pump_origin": True, "gems_require_lp_safe": True, "gems_min_liq_usd": 8000, "gems_max_dump_m5": -20, "gems_max_dump_h1": -30}
        ok = {"pump_origin": True, "lp_safe": True, "mint_revoked": True, "freeze_revoked": True, "liq": 15000, "chg_m5": 10, "dex": "pumpswap"}
        self.assertIsNone(blocked(ok, s))
        self.assertEqual(blocked({**ok, "liq": 4533}, s), "low liq")  # Chinese Inu-like
        self.assertEqual(blocked({**ok, "chg_m5": -78}, s), "already dumping")
        self.assertEqual(blocked({**ok, "already_exploded": True}, s), "already exploded")  # OFFICIALGTA-like
        self.assertEqual(blocked({**ok, "creator_rugs": 2}, s), "creator rugged")
        self.assertEqual(blocked({**ok, "pump_origin": False}, s), "not pump.fun origin")

    def test_pair_mc_and_best_pair(self):
        from util import pair_mc, best_pair
        # Dex MC lags price after a crash (Chinese Inu-like)
        p = {"priceUsd": "0.00000336", "marketCap": 6265, "fdv": 6265, "liquidity": {"usd": 4500}}
        mc, src = pair_mc(p, "Aaaapump")
        self.assertAlmostEqual(mc, 3360, delta=20)
        self.assertEqual(src, "price x 1B supply")
        # Dex MC agrees with price (OFFICIALGTA-like)
        p2 = {"priceUsd": "0.00014966", "marketCap": 149660, "fdv": 149660}
        mc, src = pair_mc(p2, "Bbbbpump")
        self.assertEqual(mc, 149660)
        self.assertEqual(src, "marketCap")
        dry_ps = {"dexId": "pumpswap", "chainId": "solana", "liquidity": {"usd": 50},
                  "quoteToken": {"address": "So11111111111111111111111111111111111111112"}}
        deep = {"dexId": "raydium", "chainId": "solana", "liquidity": {"usd": 20000},
                "quoteToken": {"address": "So11111111111111111111111111111111111111112"}}
        self.assertEqual(best_pair([dry_ps, deep])["dexId"], "raydium")
        fat_ps = dict(dry_ps, liquidity={"usd": 18000})
        self.assertEqual(best_pair([fat_ps, deep])["dexId"], "pumpswap")



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


class CreatorWatch(unittest.TestCase):
    def test_hits_for_creator(self):
        from agents.winners import hits_for_creator
        rec = {"mint": "Aaaapump", "name": "iAgent / SOL", "role": "creator", "via": "creator", "ath_mc": 1_777_721}
        idx = {"CREATOR1": [rec], "FUNDER1": [{"mint": "Aaaapump", "name": "iAgent / SOL", "role": "funder", "via": "funder", "ath_mc": 1_777_721}]}
        h = hits_for_creator("CREATOR1", idx)
        self.assertEqual(h[0][1], "created")
        self.assertEqual(h[0][0], "CREATOR1")
        h = hits_for_creator("NEWWALLET", idx, funder="FUNDER1")
        self.assertEqual(h[0][1], "funded")
        self.assertEqual(h[0][0], "FUNDER1")
        h = hits_for_creator("FUNDER1", idx)
        self.assertEqual(h[0][1], "created")
        self.assertFalse(hits_for_creator("nobody", idx))

    def test_format_creator_alert(self):
        from agents.winners import format_creator_alert
        import links
        mint = "HF2RBPrfNBk9TxggL2cYZFEKSgirzh5HKCngLENTpump"
        recs = [{"mint": "44DZsPNJUtUyTkELAFYdwCpe82sEt12HZKrMgK1Spump", "name": "iAgent / SOL", "ath_mc": 1_777_721, "via": "creator", "role": "creator"}]
        hits = [("ZXezTa6K1KEtbZwodqu6ScPWraVVdu1Y1AjotR5se5p", "created", recs)]
        body = format_creator_alert("Cool Dog", mint, hits, {"symbol": "LOOSY", "mc": 12000})
        self.assertIn("PAPER · $1M wallet: Cool Dog (LOOSY)", body)
        self.assertIn("ZXez…se5p · creator of iAgent / SOL (ATH $1,777,721) created this", body)
        self.assertIn("mint: " + mint, body)
        self.assertIn("MC $12,000", body)
        self.assertIn(links.coin_url(mint), body)
        self.assertNotIn("ref=", body)
        self.assertNotIn("utm_", body)
        self.assertTrue(body.strip().endswith(links.coin_url(mint)))
        funded = format_creator_alert("X", mint, [("FunderAddr11111111111111111111111111111", "funded", recs)], {"mc": 1900})
        self.assertIn("Fund…1111 · creator of iAgent / SOL (ATH $1,777,721) funded this coin's creator", funded)
        self.assertIn("MC $1,900", funded)
        buy_recs = [{**recs[0], "role": "cluster holder", "via": "holder"}]
        buy = format_creator_alert("Y", mint, [("ZXezTa6K1KEtbZwodqu6ScPWraVVdu1Y1AjotR5se5p", "bought", buy_recs)])
        self.assertIn("cluster holder of iAgent / SOL (ATH $1,777,721) bought this", buy)

    def test_already_alerted_memory(self):
        from agents import winners
        from unittest.mock import patch
        winners._alerted.clear()
        mint = "DedupMint111111111111111111111111111111pump"
        wallet = "Wallet1111111111111111111111111111111111"
        with patch.object(winners.db, "kv_get", return_value=None):
            self.assertFalse(winners.already_alerted(mint, wallet))
            winners._alerted[winners._alert_key(mint, wallet)] = __import__("time").time()
            self.assertTrue(winners.already_alerted(mint, wallet))
            self.assertFalse(winners.already_alerted(mint, "Wallet2222222222222222222222222222222222"))
        winners._alerted.clear()

    def test_watch_days_default(self):
        import config
        s = config.load()
        self.assertEqual(int(s.get("win_watch_days", 7)), 7)
        self.assertEqual(int(s.get("win_min_ath_mc_usd", 0)), 1_000_000)

    def test_crossed_1m_and_quotes(self):
        from agents.winners import crossed_1m, is_quote_mint, is_ignored_addr, cluster_from_links
        s = {"win_watch_days": 7, "win_min_ath_mc_usd": 1_000_000}
        now = 1_700_000_000
        self.assertTrue(crossed_1m({"mint": "Aaaapump", "mc": 2_000_000, "launched": now - 2 * 86400, "name": "FIONA"}, s, now=now))
        self.assertFalse(crossed_1m({"mint": "Trollxxxx", "mc": 20_000_000, "launched": now - 579 * 86400, "change": 5}, s, now=now))
        self.assertTrue(crossed_1m({"mint": "Newishxx", "mc": 1_500_000, "launched": now - 30 * 86400, "changes": {"h24": 200}}, s, now=now))
        self.assertFalse(crossed_1m({"mint": "Smallpump", "mc": 200_000, "launched": now - 86400}, s, now=now))
        self.assertTrue(is_quote_mint("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"))
        self.assertTrue(is_quote_mint("So11111111111111111111111111111111111111112", symbol="SOL"))
        self.assertTrue(is_ignored_addr("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"))
        self.assertTrue(is_ignored_addr("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"))
        groups = cluster_from_links(["A", "B", "C", "D"], {"A": "F", "B": "F"}, pairs=[("C", "D")])
        sets = [set(g) for g in groups]
        self.assertTrue(any({"A", "B"} <= g for g in sets))
        self.assertTrue(any({"C", "D"} <= g for g in sets))

    def test_send_wallet_alert_no_quality_gate(self):
        from agents import winners
        from unittest.mock import patch
        mint = "NewDeadCurve" + __import__("uuid").uuid4().hex[:16] + "pump"
        recs = [{"mint": "SrcMillion11111111111111111111111111pump", "name": "FIONA", "ath_mc": 3_300_317, "role": "funder", "via": "funder"}]
        hits = [("FunderAddr11111111111111111111111111111", "funded", recs)]
        sent = []
        winners._alerted.clear()
        def fake_send(text, buttons=None, chat=None, kind=None):
            sent.append(text)
        with patch.object(winners.notifier, "send", fake_send), \
             patch.object(winners, "enrich_live_mc", lambda m, extra=None: dict(extra or {}, mc=1900, dex="pumpfun", liq=0)), \
             patch.object(winners, "source_mints", lambda s=None: set()):
            ok = winners.send_creator_alert("DeadCurve", mint, hits, {"mc": 1900, "liq": 0, "dex": "pumpfun"}, s={})
        self.assertTrue(ok)
        self.assertEqual(len(sent), 1)
        self.assertIn("MC $1,900", sent[0])
        self.assertIn("funder of FIONA", sent[0])
        # source coin itself is never alerted
        winners._alerted.clear()
        src = recs[0]["mint"]
        with patch.object(winners.notifier, "send", fake_send), \
             patch.object(winners, "enrich_live_mc", lambda m, extra=None: dict(extra or {})), \
             patch.object(winners, "source_mints", lambda s=None: {src}):
            self.assertFalse(winners.send_creator_alert("FIONA", src, hits, s={}))
        self.assertEqual(len(sent), 1)
        winners._alerted.clear()


class BubbleMap(unittest.TestCase):
    def test_holders_from_report_drops_pools(self):
        from agents.winners import holders_from_report, pool_addrs_from_report, is_ignored_addr
        pool = "Bfiq9eaLcMjpEKcgm16GSzzGdedCpcFhRAFvivRP4QkY"
        wallet = "9SB8yCeS6aCohhGvrXcmZcV8iziFEa84767UCPLu5HsW"
        rep = {
            "topHolders": [
                {"owner": pool, "pct": 91.9},
                {"owner": wallet, "pct": 2.0},
                {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "pct": 0.1},
            ],
            "markets": [{"pubkey": pool, "lp": {"lpMint": "CyzrkP3ZMnthJdqE9YZrK41Gf4UNzaMFVifBmE4wBZo1"}}],
        }
        self.assertIn(pool, pool_addrs_from_report(rep))
        self.assertEqual(holders_from_report(rep), [wallet])
        self.assertTrue(is_ignored_addr("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"))

    def test_owners_from_das(self):
        from agents.winners import _owners_from_das
        res = {"token_accounts": [
            {"owner": "WalletA111111111111111111111111111111111", "amount": 10},
            {"owner": "WalletA111111111111111111111111111111111", "amount": 1},
            {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "amount": 2},
            {"owner": "WalletB111111111111111111111111111111111", "amount": 3},
        ]}
        self.assertEqual(_owners_from_das(res), [
            "WalletA111111111111111111111111111111111",
            "WalletB111111111111111111111111111111111",
        ])

    def test_map_order_prefers_pump_with_liq(self):
        from agents.winners import map_skip_reason, map_sort_key
        self.assertEqual(map_skip_reason("CptDkxR8rV68ngdh2JgFdSodu2KQSmyNEyyin9RdWZp1", 0, "TTWO / SOL", "TTWO"), "zero-liq")
        self.assertIsNone(map_skip_reason("B34nXKzCgsJb9ChLuSHPRpXLE3WFSWyeT8Cqkzzmpump", 20000))
        self.assertIsNone(map_skip_reason("B34nXKzCgsJb9ChLuSHPRpXLE3WFSWyeT8Cqkzzmpump", 0))
        coins = [
            {"mint": "TTWOxxxxnotpumpxxxxxxxxxxxxxxxxxxxxxx", "name": "TTWO", "ath_mc": 20_000_000},
            {"mint": "Aaaapump", "name": "FIONA", "ath_mc": 3_000_000},
            {"mint": "Bbbbpump", "name": "RINGS", "ath_mc": 2_800_000},
            {"mint": "Ccccpump", "name": "LOOSY", "ath_mc": 2_500_000},
        ]
        liq = {"Aaaapump": 50000, "Ccccpump": 2000, "TTWOxxxxnotpumpxxxxxxxxxxxxxxxxxxxxxx": 0}
        ordered = sorted(coins, key=lambda c: map_sort_key(c, liq.get(c["mint"])))
        self.assertEqual([c["name"] for c in ordered], ["FIONA", "LOOSY", "RINGS", "TTWO"])

    def test_rescore_is_batched(self):
        import inspect
        from agents.gems import rescore_existing
        sig = inspect.signature(rescore_existing)
        self.assertIn("limit", sig.parameters)
        self.assertIn("budget_s", sig.parameters)


if __name__ == "__main__":
    unittest.main()
