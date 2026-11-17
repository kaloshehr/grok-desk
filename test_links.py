from links import coin_url, profile_url, FOMO_HOME, CHAIN_ID
M = "Dfid5ax62osiWxSQS6gpZwRKtZAw32ktxuTPG47Upump"
t = [coin_url(M) == f"https://fomo.family/coin?address={M}&chainId=1399811149",
     coin_url(M, "solana") == coin_url(M), coin_url(M, "base").endswith("chainId=8453"),
     coin_url(M, "bnb").endswith("chainId=56"), coin_url(M, "robinhood").endswith("chainId=4663"),
     coin_url(M, "eth") == FOMO_HOME, profile_url("krytix102") == "https://fomo.family/profile/krytix102",
     profile_url("@krytix102") == "https://fomo.family/profile/krytix102"]
assert all(t), t; print(f"test_links: {len(t)}/{len(t)} OK")
