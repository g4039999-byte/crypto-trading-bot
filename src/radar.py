import requests

PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
TOKENS_URL = "https://api.dexscreener.com/tokens/v1/solana/{addresses}"


def get_latest_solana_tokens():
    response = requests.get(PROFILES_URL, timeout=10)
    response.raise_for_status()

    return [
        token
        for token in response.json()
        if token.get("chainId") == "solana"
        and token.get("tokenAddress")
    ]


def get_market_data(addresses):
    joined = ",".join(addresses)

    response = requests.get(
        TOKENS_URL.format(addresses=joined),
        timeout=10,
    )
    response.raise_for_status()

    return response.json()


def main():
    profiles = get_latest_solana_tokens()
    addresses = [token["tokenAddress"] for token in profiles[:30]]

    print(f"Solana tokens discovered: {len(addresses)}")

    if not addresses:
        return

    pairs = get_market_data(addresses)

    print(f"Market pairs returned: {len(pairs)}")
    print()

    for pair in pairs:
        base = pair.get("baseToken", {})
        txns = pair.get("txns", {}).get("h24", {})
        volume = pair.get("volume", {}).get("h24")
        price_change = pair.get("priceChange", {}).get("h24")
        liquidity = pair.get("liquidity", {}).get("usd")

        print(
            f"{base.get('symbol', '?')} | "
            f"price=${pair.get('priceUsd')} | "
            f"liq=${liquidity} | "
            f"vol24h=${volume} | "
            f"buys24h={txns.get('buys', 0)} | "
            f"sells24h={txns.get('sells', 0)} | "
            f"change24h={price_change}%"
        )


if __name__ == "__main__":
    main()
