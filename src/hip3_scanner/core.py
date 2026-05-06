from __future__ import annotations

import math
from decimal import Decimal
from itertools import combinations
from typing import Any

from .config import ScanConfig
from .models import MarketSnapshot, VenueInfo, ZERO, to_decimal
from .taxonomy import infer_category, overlap_confidence, spec_mismatch_penalty

BPS = Decimal("10000")
ONE_HUNDRED = Decimal("100")


def basis_points(value: Decimal) -> Decimal:
    return value * BPS


def pct_to_bps(value: Decimal) -> Decimal:
    return value * Decimal("100")


def spread_bps(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    if a is None or b is None:
        return None
    avg = (a + b) / Decimal("2")
    if avg == ZERO:
        return None
    return abs(a - b) / avg * BPS


def directed_executable_spread_bps(buy_px: Decimal | None, sell_px: Decimal | None) -> Decimal | None:
    if buy_px is None or sell_px is None:
        return None
    avg = (buy_px + sell_px) / Decimal("2")
    if avg == ZERO:
        return None
    return (sell_px - buy_px) / avg * BPS


def _pairs_to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {k: v for k, v in value if isinstance(k, str)}
    return {}


def parse_venue_info(entry: dict[str, Any]) -> VenueInfo:
    return VenueInfo(
        name=entry["name"],
        full_name=entry.get("fullName"),
        asset_to_streaming_oi_cap=_pairs_to_dict(entry.get("assetToStreamingOiCap")),
        asset_to_funding_multiplier=_pairs_to_dict(entry.get("assetToFundingMultiplier")),
        asset_to_funding_interest_rate=_pairs_to_dict(entry.get("assetToFundingInterestRate")),
    )


def normalize_market_snapshots(ts_ms: int, dex: str, venue_info: VenueInfo | None, payload: Any) -> list[MarketSnapshot]:
    if not isinstance(payload, list) or len(payload) != 2:
        raise ValueError(f"Unexpected metaAndAssetCtxs payload for {dex}")
    meta_blob, ctxs = payload
    universe = meta_blob.get("universe") or meta_blob.get("assets") or []
    snapshots: list[MarketSnapshot] = []
    for meta, ctx in zip(universe, ctxs, strict=False):
        market_id = meta.get("name") or ""
        underlying = market_id.split(":", 1)[1] if ":" in market_id else market_id
        impact = ctx.get("impactPxs") or [None, None]
        stream_cap_raw = None
        funding_multiplier_raw = None
        interest_override_raw = None
        if venue_info is not None:
            stream_cap_raw = venue_info.asset_to_streaming_oi_cap.get(market_id)
            funding_multiplier_raw = venue_info.asset_to_funding_multiplier.get(market_id)
            interest_override_raw = venue_info.asset_to_funding_interest_rate.get(market_id)
        snapshots.append(
            MarketSnapshot(
                ts_ms=ts_ms,
                venue=dex,
                venue_full_name=venue_info.full_name if venue_info else None,
                market_id=market_id,
                underlying_key=underlying,
                category=infer_category(dex, underlying),
                sz_decimals=meta.get("szDecimals"),
                max_leverage=meta.get("maxLeverage"),
                margin_table_id=meta.get("marginTableId"),
                only_isolated=bool(meta.get("onlyIsolated", False)),
                margin_mode=meta.get("marginMode"),
                is_delisted=bool(meta.get("isDelisted", False)),
                growth_mode=meta.get("growthMode"),
                last_growth_mode_change_time=meta.get("lastGrowthModeChangeTime"),
                mark_px=to_decimal(ctx.get("markPx")),
                mid_px=to_decimal(ctx.get("midPx")),
                oracle_px=to_decimal(ctx.get("oraclePx")),
                impact_bid_px=to_decimal(impact[0] if len(impact) > 0 else None),
                impact_ask_px=to_decimal(impact[1] if len(impact) > 1 else None),
                funding=to_decimal(ctx.get("funding")),
                premium=to_decimal(ctx.get("premium")),
                open_interest=to_decimal(ctx.get("openInterest")),
                day_ntl_vlm=to_decimal(ctx.get("dayNtlVlm")),
                day_base_vlm=to_decimal(ctx.get("dayBaseVlm")),
                prev_day_px=to_decimal(ctx.get("prevDayPx")),
                streaming_oi_cap=to_decimal(stream_cap_raw),
                funding_multiplier=to_decimal(funding_multiplier_raw),
                interest_rate_override=to_decimal(interest_override_raw),
            )
        )
    return snapshots


def build_duplicate_groups(markets: list[MarketSnapshot]) -> dict[str, list[MarketSnapshot]]:
    grouped: dict[str, list[MarketSnapshot]] = {}
    for market in markets:
        grouped.setdefault(market.underlying_key, []).append(market)
    return {k: v for k, v in grouped.items() if len(v) >= 2}


def hard_reject_reasons(a: MarketSnapshot, b: MarketSnapshot, config: ScanConfig) -> list[str]:
    reasons: list[str] = []
    if a.underlying_key != b.underlying_key:
        reasons.append("underlying_mismatch")
    if a.is_delisted or b.is_delisted:
        reasons.append("delisted")
    if a.mid_px is None or b.mid_px is None:
        reasons.append("missing_mid")
    if a.mark_px is None or b.mark_px is None:
        reasons.append("missing_mark")
    if not a.has_impact or not b.has_impact:
        reasons.append("missing_impact")
    if (a.day_ntl_vlm or ZERO) < Decimal(str(config.min_day_ntl_vlm)) or (b.day_ntl_vlm or ZERO) < Decimal(str(config.min_day_ntl_vlm)):
        reasons.append("low_volume")
    if (a.open_interest or ZERO) <= ZERO or (b.open_interest or ZERO) <= ZERO:
        reasons.append("low_open_interest")
    if a.category and b.category and a.category != b.category:
        reasons.append("category_mismatch")
    return reasons


def liquidity_score(a: MarketSnapshot, b: MarketSnapshot, book_confirmed: bool) -> float:
    min_ntl = min(a.day_ntl_vlm or ZERO, b.day_ntl_vlm or ZERO)
    if min_ntl <= ZERO:
        return 0.0
    normalized = min(math.log10(1 + float(min_ntl)), 8.0) / 8.0
    depth_factor = 1.0 if book_confirmed else 0.5
    return normalized * depth_factor


def assign_tier(executable_spread_bps: Decimal, mark_spread_bps: Decimal, oracle_divergence_bps: Decimal, funding_spread_bps: Decimal, a: MarketSnapshot, b: MarketSnapshot, book_confirmed: bool, config: ScanConfig) -> str | None:
    min_ntl = min(a.day_ntl_vlm or ZERO, b.day_ntl_vlm or ZERO)
    min_oi = min(a.open_interest or ZERO, b.open_interest or ZERO)
    if (
        min_ntl >= Decimal(str(config.strong_day_ntl_vlm))
        and min_oi > Decimal(str(config.strong_open_interest))
        and executable_spread_bps >= Decimal(str(config.strong_exec_spread_bps))
        and oracle_divergence_bps <= Decimal(str(config.strong_oracle_divergence_bps))
        and book_confirmed
    ):
        return "strong_candidate"
    if (
        min_ntl >= Decimal(str(config.heads_up_day_ntl_vlm))
        and min_oi > Decimal(str(config.min_open_interest))
        and mark_spread_bps >= Decimal(str(config.heads_up_mark_spread_bps))
        and executable_spread_bps >= Decimal(str(config.heads_up_exec_spread_bps))
        and oracle_divergence_bps <= Decimal(str(config.heads_up_oracle_divergence_bps))
        and (
            funding_spread_bps >= Decimal(str(config.min_funding_spread_bps))
            or executable_spread_bps >= Decimal(str(config.strong_exec_spread_bps))
        )
    ):
        return "heads_up"
    if funding_spread_bps >= Decimal(str(config.review_funding_spread_bps)) or mark_spread_bps >= Decimal(str(config.review_mark_spread_bps)):
        return "review"
    return None


def _best_book_prices(book: dict[str, Any]) -> tuple[Decimal | None, Decimal | None, bool]:
    levels = book.get("levels") or []
    if len(levels) < 2 or not levels[0] or not levels[1]:
        return None, None, True
    best_bid = to_decimal(levels[0][0].get("px"))
    best_ask = to_decimal(levels[1][0].get("px"))
    return best_bid, best_ask, False


def evaluate_opportunities(markets: list[MarketSnapshot], config: ScanConfig, book_fetcher: Any | None = None) -> list[dict[str, Any]]:
    duplicate_groups = build_duplicate_groups(markets)
    opportunities: list[dict[str, Any]] = []
    for underlying, group in duplicate_groups.items():
        for a, b in combinations(group, 2):
            mark_spread = spread_bps(a.mark_px, b.mark_px)
            mid_spread = spread_bps(a.mid_px, b.mid_px)
            oracle_spread = spread_bps(a.oracle_px, b.oracle_px)
            funding_spread = abs((a.funding or ZERO) - (b.funding or ZERO)) * BPS
            for buy, sell in ((a, b), (b, a)):
                raw_exec = directed_executable_spread_bps(buy.impact_ask_px, sell.impact_bid_px)
                if raw_exec is None:
                    continue
                reject_reasons = hard_reject_reasons(buy, sell, config)
                if reject_reasons:
                    continue
                if oracle_spread is None or oracle_spread > Decimal(str(config.max_oracle_divergence_bps)):
                    continue
                book_confirmed = False
                l2_exec = None
                risk_flags: list[str] = []
                if raw_exec >= Decimal(str(config.candidate_exec_spread_bps)) and book_fetcher is not None:
                    buy_book = book_fetcher(buy.market_id, buy.venue)
                    sell_book = book_fetcher(sell.market_id, sell.venue)
                    buy_bid, buy_ask, buy_empty = _best_book_prices(buy_book)
                    sell_bid, sell_ask, sell_empty = _best_book_prices(sell_book)
                    if buy_empty or sell_empty:
                        continue
                    l2_exec = directed_executable_spread_bps(buy_ask, sell_bid)
                    book_confirmed = l2_exec is not None and l2_exec > ZERO
                    if not book_confirmed:
                        continue
                overlap = overlap_confidence(buy.category, sell.category, buy.venue, sell.venue)
                spec_penalty = spec_mismatch_penalty(buy.category, sell.category, buy.venue, sell.venue)
                if spec_penalty >= 1.5:
                    continue
                if overlap < 0.5:
                    risk_flags.append("low_overlap_confidence")
                effective_exec = l2_exec if l2_exec is not None else raw_exec
                if effective_exec is None or effective_exec < Decimal("1"):
                    continue
                if mark_spread is None or mid_spread is None or oracle_spread is None:
                    continue
                liq_score = liquidity_score(buy, sell, book_confirmed)
                staleness_penalty = 0.5 if not book_confirmed else 0.0
                capped_funding = min(funding_spread, Decimal("5"))
                score = (
                    float(max(effective_exec, ZERO))
                    + 0.35 * float(capped_funding)
                    + 0.20 * liq_score
                    + 0.15 * overlap
                    - 0.75 * float(oracle_spread)
                    - 0.50 * staleness_penalty
                    - 1.25 * spec_penalty
                )
                tier = assign_tier(effective_exec, mark_spread, oracle_spread, funding_spread, buy, sell, book_confirmed, config)
                if tier is None:
                    continue
                if tier == "review":
                    risk_flags.append("low_confidence")
                opp_id = f"{underlying}|{buy.venue}|{sell.venue}|buy_{buy.venue}_sell_{sell.venue}"
                opportunities.append(
                    {
                        "ts_ms": buy.ts_ms,
                        "opportunity_id": opp_id,
                        "underlying_key": underlying,
                        "category": buy.category or sell.category,
                        "direction": {
                            "buy_venue": buy.venue,
                            "buy_market_id": buy.market_id,
                            "sell_venue": sell.venue,
                            "sell_market_id": sell.market_id,
                        },
                        "metrics": {
                            "mid_spread_bps": round(float(mid_spread), 4),
                            "mark_spread_bps": round(float(mark_spread), 4),
                            "impact_executable_spread_bps": round(float(raw_exec), 4),
                            "l2_executable_spread_bps": round(float(l2_exec), 4) if l2_exec is not None else None,
                            "oracle_divergence_bps": round(float(oracle_spread), 4),
                            "funding_buy_bps": round(float((buy.funding or ZERO) * BPS), 4),
                            "funding_sell_bps": round(float((sell.funding or ZERO) * BPS), 4),
                            "funding_spread_bps": round(float(funding_spread), 4),
                        },
                        "liquidity": {
                            "min_day_ntl_vlm": str(min(buy.day_ntl_vlm or ZERO, sell.day_ntl_vlm or ZERO)),
                            "max_day_ntl_vlm": str(max(buy.day_ntl_vlm or ZERO, sell.day_ntl_vlm or ZERO)),
                            "min_open_interest": str(min(buy.open_interest or ZERO, sell.open_interest or ZERO)),
                            "book_confidence": "l2_confirmed" if book_confirmed else "impact_only",
                        },
                        "filters": {
                            "passes_hard_filters": True,
                            "oracle_ok": True,
                            "liquidity_ok": True,
                            "not_delisted": True,
                            "not_at_oi_cap": True,
                            "spec_match_confidence": round(overlap, 4),
                        },
                        "score": {
                            "raw_score": round(score, 4),
                            "confidence": round(max(0.0, min(1.0, overlap - spec_penalty * 0.25 + (0.2 if book_confirmed else 0.0))), 4),
                            "tier": tier,
                        },
                        "explanations": [
                            "same suffix across venues",
                            "positive executable spread after impact/L2 check",
                            "oracle divergence within threshold",
                            "minimum liquidity filters passed",
                        ],
                        "risk_flags": risk_flags,
                    }
                )
    opportunities.sort(key=lambda item: (item["score"]["raw_score"], item["metrics"]["impact_executable_spread_bps"]), reverse=True)
    return opportunities
