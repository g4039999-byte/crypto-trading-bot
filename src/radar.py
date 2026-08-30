import requests
import time

from scoring import calculate_score
from config import MIN_LIQUIDITY_USD, MIN_VOLUME_24H_USD

PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
TOKENS_URL = "https://api.dexscreener.com/tokens/v1/solana/{addresses}"


def main():
    profiles_response = requests.get(PROFILES_URL, timeout=10)
    profiles_response.raise_for_status()
    profiles = profiles_response.json()

    addresses = [
        item["tokenAddress"]
        for item in profiles
        if item.get("chainId") == "solana" and item.get("tokenAddress")
    ]

    print(f"Solana tokens discovered: {len(addresses)}")

    if not addresses:
        return

    url = TOKENS_URL.format(addresses=",".join(addresses[:30]))
    pairs_response = requests.get(url, timeout=10)
    pairs_response.raise_for_status()
    pairs = pairs_response.json()

    print(f"Market pairs returned: {len(pairs)}")

    passed = 0
    results = []

    for pair in pairs:
        base = pair.get("baseToken", {})
        liquidity = pair.get("liquidity", {}).get("usd")
        volume = pair.get("volume", {}).get("h24")
        txns = pair.get("txns", {}).get("h24", {})
        buys = txns.get("buys", 0) or 0
        sells = txns.get("sells", 0) or 0

        ok = (
            liquidity is not None
            and volume is not None
            and liquidity >= MIN_LIQUIDITY_USD
            and volume >= MIN_VOLUME_24H_USD
            and buys >= 0.8 * max(sells, 1)
        )

        passed += int(ok)

        score = calculate_score(pair)

        created = pair.get("pairCreatedAt")
        age_minutes = (
            (time.time() * 1000 - created) / 60000
            if created
            else None
        )

        results.append(
            (
                score,
                ok,
                base.get("symbol", "?"),
                age_minutes,
                liquidity,
                volume,
                buys,
                sells,
            )
        )

    results.sort(key=lambda item: item[0], reverse=True)

    print()
    print("=== RANKED RESULTS ===")

    for score, ok, symbol, age, liquidity, volume, buys, sells in results:
        age_text = f"{age:.1f}m" if age is not None else "N/A"
        status = "PASS" if ok else "REJECT"

        print(
            f"[{status}] {symbol} | "
            f"score={score}/100 | "
            f"age={age_text} | "
            f"liq=${liquidity} | "
            f"vol24h=${volume} | "
            f"buys={buys} | "
            f"sells={sells}"
        )

    print()
    print(f"Pairs passing first filter: {passed}")


if __name__ == "__main__":
    main()