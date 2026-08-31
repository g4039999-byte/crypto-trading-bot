"""Small, dependency-free helpers shared across the radar pipeline."""


def safe_get(obj, *keys, default=None):
    """Walk a chain of dict lookups without raising on missing or None
    values at any level.

    DexScreener sometimes returns a key with an explicit `null` instead of
    omitting it (e.g. {"liquidity": null}), which breaks the common
    `pair.get("liquidity", {}).get("usd")` pattern: the default only kicks
    in when the key is *absent*, not when its value is None, so that
    second .get() then crashes with AttributeError. safe_get handles both
    cases the same way.

    Example: safe_get(pair, "liquidity", "usd", default=0)
    """
    current = obj
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current
