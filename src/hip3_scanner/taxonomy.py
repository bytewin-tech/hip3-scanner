from __future__ import annotations

CATEGORY_MAP = {
    "AAPL": "stocks",
    "AMZN": "stocks",
    "BABA": "stocks",
    "BTC": "crypto",
    "COIN": "stocks",
    "COPPER": "commodities",
    "CRCL": "stocks",
    "ETH": "crypto",
    "EUR": "fx",
    "EWY": "etf",
    "GOOGL": "stocks",
    "GOLD": "commodities",
    "HOOD": "stocks",
    "INTC": "stocks",
    "META": "stocks",
    "MSFT": "stocks",
    "MU": "stocks",
    "NVDA": "stocks",
    "PALLADIUM": "commodities",
    "PLATINUM": "commodities",
    "PLTR": "stocks",
    "SILVER": "commodities",
    "TSLA": "stocks",
    "USA500": "indices",
    "WHEAT": "commodities",
    "XMR": "crypto",
}

SUSPICIOUS_VENUES = {"vntl", "para"}
SUSPICIOUS_CATEGORIES = {"preipo", "indices", "basket", "thematic"}


def infer_category(venue: str, underlying: str) -> str | None:
    if venue == "vntl":
        return "preipo"
    if venue == "para":
        return "thematic"
    return CATEGORY_MAP.get(underlying)


def overlap_confidence(category_a: str | None, category_b: str | None, venue_a: str, venue_b: str) -> float:
    if category_a and category_b and category_a == category_b and category_a not in SUSPICIOUS_CATEGORIES:
        return 1.0
    if category_a is None or category_b is None:
        return 0.6
    if venue_a in SUSPICIOUS_VENUES or venue_b in SUSPICIOUS_VENUES:
        return 0.2
    return 0.2


def spec_mismatch_penalty(category_a: str | None, category_b: str | None, venue_a: str, venue_b: str) -> float:
    if category_a and category_b and category_a != category_b:
        return 1.5
    if category_a in SUSPICIOUS_CATEGORIES or category_b in SUSPICIOUS_CATEGORIES:
        return 1.0
    if venue_a in SUSPICIOUS_VENUES or venue_b in SUSPICIOUS_VENUES:
        return 0.8
    return 0.0
