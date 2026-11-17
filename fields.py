"""Field map from raw samples in logs/raw/ (test mint Dfid5ax62osiWxSQS6gpZwRKtZAw32ktxuTPG47Upump, 2026-11-03).
None = key not present in the raw sample; never guessed."""
PUMPPORTAL_MIGRATION = {
    "mint": "mint", "signature": "signature", "txType": "txType",
    "pool": None,     # message has "pool":"pump-amm" = AMM NAME, not an address -> take pairAddress from DexScreener
    "creator": None,  # not in message -> RUGCHECK_REPORT["creator"]
}
DEXSCREENER_PAIR = {  # response is a list; choose item with dexId=="pumpswap"
    "pairAddress": "pairAddress", "dexId": "dexId", "priceUsd": "priceUsd",
    "liquidity_usd": "liquidity.usd",  # absent on the early "pumpfun" curve pair
    "marketCap": "marketCap",
    "volume_m5": "volume.m5", "volume_h1": "volume.h1", "volume_h24": "volume.h24",
    "txns_m5_buys": "txns.m5.buys", "txns_m5_sells": "txns.m5.sells",
    "txns_h1_buys": "txns.h1.buys", "txns_h1_sells": "txns.h1.sells",
    "priceChange_m5": "priceChange.m5", "priceChange_h1": "priceChange.h1", "priceChange_h24": "priceChange.h24",
    "pairCreatedAt": "pairCreatedAt",  # ms epoch
    "name": "baseToken.name", "symbol": "baseToken.symbol",
}
RUGCHECK_REPORT = {
    "creator": "creator", "creatorBalance": "creatorBalance",
    "markets": "markets", "market_pool": "markets[].pubkey", "market_type": "markets[].marketType",
    "lp_locked_pct": "markets[].lp.lpLockedPct",
    "lp_burned_pct": None,  # no burned-pct key in lp; lpLockedPct appears to include burned
    "totalHolders": "totalHolders", "topHolders": "topHolders",
}
RUGCHECK_GRAPH = {  # sample returned [] (empty list): no nodes/edges keys seen
    "nodes": None, "edges": None,
}
RPC_GETACCOUNTINFO = {
    "owner_program": "result.value.owner",
    "mintAuthority": "result.value.data.parsed.info.mintAuthority",
    "freezeAuthority": "result.value.data.parsed.info.freezeAuthority",
    "supply": "result.value.data.parsed.info.supply",
    "decimals": "result.value.data.parsed.info.decimals",
}
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
