CHAIN_ID = {'solana': 1399811149, 'robinhood': 4663, 'base': 8453, 'bnb': 56}
FOMO_HOME = 'https://fomo.family'
def coin_url(address, chain='solana'):
    if chain not in CHAIN_ID: return FOMO_HOME
    return f'https://fomo.family/coin?address={address}&chainId={CHAIN_ID[chain]}'
def profile_url(handle):
    return f'https://fomo.family/profile/{handle.lstrip("@")}'
def wallet_url(addr):
    return f'https://solscan.io/account/{addr}'
