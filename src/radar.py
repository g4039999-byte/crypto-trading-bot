import requests
from config import MIN_LIQUIDITY_USD, MIN_VOLUME_24H_USD
URL="https://api.dexscreener.com/token-profiles/latest/v1"
def main():
    profiles=requests.get(URL,timeout=10).json()
    addresses=[x["tokenAddress"] for x in profiles if x.get("chainId")=="solana" and x.get("tokenAddress")]
    print(f"Solana tokens discovered: {len(addresses)}")
    if not addresses:return
    url=f"https://api.dexscreener.com/tokens/v1/solana/{",".join(addresses[:30])}"
    pairs=requests.get(url,timeout=10).json()
    print(f"Market pairs returned: {len(pairs)}")
    passed=0
    for p in pairs:
        b=p.get("baseToken",{}); l=p.get("liquidity",{}).get("usd"); v=p.get("volume",{}).get("h24"); t=p.get("txns",{}).get("h24",{}); buys=t.get("buys",0) or 0; sells=t.get("sells",0) or 0
        ok=l is not None and v is not None and l>=MIN_LIQUIDITY_USD and v>=MIN_VOLUME_24H_USD and buys>=0.8*max(sells,1)
        passed+=ok
        print(f"[{"PASS" if ok else "REJECT"}] {b.get("symbol","?")} | liq=${l} | vol24h=${v} | buys={buys} | sells={sells}")
    print(f"Pairs passing first filter: {passed}")
if __name__=="__main__":main()
