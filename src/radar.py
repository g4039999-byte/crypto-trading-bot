import requests


URL = "https://api.dexscreener.com/token-profiles/latest/v1"


def get_latest_solana_tokens():
    response = requests.get(URL, timeout=10)
    response.raise_for_status()

    profiles = response.json()

    return [
        token
        for token in profiles
        if token.get("chainId") == "solana"
    ]


def main():
    tokens = get_latest_solana_tokens()

    print(f"Solana tokens found: {len(tokens)}")

    for token in tokens:
        print(
            f"- {token.get('tokenAddress')} | "
            f"{token.get('url')}"
        )


if __name__ == "__main__":
    main()
