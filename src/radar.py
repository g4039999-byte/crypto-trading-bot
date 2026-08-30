from src.observation import analyze_observation
import requests
import time

from config import MIN_LIQUIDITY_USD, MIN_VOLUME_24H_USD
from scoring import calculate_score
from momentum import calculate_momentum
from stage import classify_stage
from snapshot import save_snapshot

PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
TOKENS_URL = "https://api.dexscreener.com/tokens/v1/solana/{addresses}"


def main():
    profiles_response = requests.get(PROFILES_URL, timeout=10)
    profiles_response.raise_for_status()
    profiles = profiles_response.json()

    addresses = [
        item["tokenAddress"]
        for item in profiles
        if item.get("chainId") == "solana"
        and item.get("tokenAddress")
    ]

    print(f"Solana tokens discovered: {len(addresses)}")

    if not addresses:
        return

    url = TOKENS_URL.format(
        addresses=",".join(addresses[:30])
    )

    pairs_response = requests.get(url, timeout=10)
    pairs_response.raise_for_status()
    pairs = pairs_response.json()

    print(f"Market pairs returned: {len(pairs)}")

    passed = 0
    results = []

    for pair in pairs:
        base = pair.get("baseToken", {})
        symbol = base.get("symbol", "?")

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

        save_snapshot(base.get("address", "?"), pair)
        base_score = calculate_score(pair)
        momentum_score = calculate_momentum(pair)

        final_score = round(
            (base_score * 0.60) +
            (momentum_score * 0.40)
        )

        created = pair.get("pairCreatedAt")

        age_minutes = (
            (time.time() * 1000 - created) / 60000
            if created
            else None
        )

        stage = classify_stage(age_minutes)

        observation = analyze_observation(base.get("address", "?"))
        results.append(
            {
                "score": final_score,
                "ok": ok,
                "symbol": symbol,
                "stage": stage,
                "age": age_minutes,
                "liquidity": liquidity,
                "volume": volume,
                "buys": buys,
                "sells": sells,
                "address": base.get("address", "?"),
            }
        )

    results.sort(
        key=lambda item: item["score"],
        reverse=True,
    )

    print()
    print("=== FINAL RANKED RESULTS ===")

    for item in results:
        age_text = (
            f"{item['age']:.1f}m"
            if item["age"] is not None
            else "N/A"
        )

        status = "PASS" if item["ok"] else "REJECT"

        print(
            f"[{status}] "
            f"{item['symbol']} | "
            f"FINAL={item['score']}/100 | "
            f"{item['stage']} | "
            f"age={age_text} | "
            f"liq=${item['liquidity']} | "
            f"vol24h=${item['volume']} | "
            f"buys={item['buys']} | "
            f"sells={item['sells']}"
        )

    print()
    print(f"Pairs passing first filter: {passed}")


if __name__ == "__main__":
    main()