from __future__ import annotations

from decimal import Decimal

from hip3_scanner.config import ScanConfig
from hip3_scanner.core import (
    build_duplicate_groups,
    directed_executable_spread_bps,
    evaluate_opportunities,
    normalize_market_snapshots,
    parse_venue_info,
    spread_bps,
)
from hip3_scanner.paper import PaperTrader, build_paper_trader_config
from hip3_scanner.scanner import format_console


def test_spread_functions():
    assert spread_bps(Decimal("100"), Decimal("101")) == Decimal("99.50248756218905472636815920")
    assert directed_executable_spread_bps(Decimal("100"), Decimal("101")) == Decimal("99.50248756218905472636815920")


def test_normalize_market_snapshots_and_duplicates():
    venue = parse_venue_info({"name": "xyz", "fullName": "XYZ", "assetToStreamingOiCap": {"xyz:AAPL": "1000"}, "assetToFundingMultiplier": {"xyz:AAPL": "0.5"}, "assetToFundingInterestRate": {"xyz:AAPL": "0.001"}})
    payload = [
        {"universe": [{"name": "xyz:AAPL", "onlyIsolated": True, "isDelisted": False}]},
        [{"markPx": "100", "midPx": "100.1", "oraclePx": "100", "impactPxs": ["99.9", "100.2"], "funding": "0.0001", "openInterest": "200", "dayNtlVlm": "100000", "dayBaseVlm": "500", "prevDayPx": "99"}],
    ]
    snapshots = normalize_market_snapshots(1, "xyz", venue, payload)
    assert snapshots[0].underlying_key == "AAPL"
    assert snapshots[0].streaming_oi_cap == Decimal("1000")
    groups = build_duplicate_groups(snapshots + [snapshots[0]])
    assert "AAPL" in groups


def test_evaluate_opportunities_with_l2_confirmation():
    venue = parse_venue_info({"name": "xyz", "fullName": "XYZ", "assetToStreamingOiCap": {}, "assetToFundingMultiplier": {}, "assetToFundingInterestRate": {}})
    venue2 = parse_venue_info({"name": "km", "fullName": "KM", "assetToStreamingOiCap": {}, "assetToFundingMultiplier": {}, "assetToFundingInterestRate": {}})
    payload_a = [
        {"universe": [{"name": "xyz:TSLA", "onlyIsolated": True, "isDelisted": False}]},
        [{"markPx": "100", "midPx": "100", "oraclePx": "100", "impactPxs": ["99.7", "99.8"], "funding": "0.0001", "openInterest": "1000", "dayNtlVlm": "700000", "dayBaseVlm": "500", "prevDayPx": "99"}],
    ]
    payload_b = [
        {"universe": [{"name": "km:TSLA", "onlyIsolated": True, "isDelisted": False}]},
        [{"markPx": "100.05", "midPx": "100.05", "oraclePx": "100.02", "impactPxs": ["100.3", "100.4"], "funding": "0.0006", "openInterest": "1200", "dayNtlVlm": "900000", "dayBaseVlm": "700", "prevDayPx": "100"}],
    ]
    markets = normalize_market_snapshots(1, "xyz", venue, payload_a) + normalize_market_snapshots(1, "km", venue2, payload_b)

    def fetcher(market_id: str, venue_name: str):
        if market_id == "xyz:TSLA":
            return {"levels": [[{"px": "99.7", "sz": "10", "n": 1}], [{"px": "99.8", "sz": "10", "n": 1}]]}
        return {"levels": [[{"px": "100.3", "sz": "10", "n": 1}], [{"px": "100.4", "sz": "10", "n": 1}]]}

    opps = evaluate_opportunities(markets, ScanConfig(), book_fetcher=fetcher)
    assert opps
    assert opps[0]["underlying_key"] == "TSLA"
    assert opps[0]["score"]["tier"] in {"heads_up", "strong_candidate", "review"}


def _paper_opportunity(exec_bps: float, score: float = 4.2, confidence: float = 0.8, tier: str = "strong_candidate"):
    return {
        "opportunity_id": "TSLA|xyz|km|buy_xyz_sell_km",
        "underlying_key": "TSLA",
        "direction": {
            "buy_venue": "xyz",
            "buy_market_id": "xyz:TSLA",
            "sell_venue": "km",
            "sell_market_id": "km:TSLA",
        },
        "metrics": {
            "mark_spread_bps": exec_bps + 1.0,
            "impact_executable_spread_bps": exec_bps,
            "l2_executable_spread_bps": exec_bps,
            "funding_spread_bps": 1.4,
            "oracle_divergence_bps": 1.0,
        },
        "score": {"raw_score": score, "confidence": confidence, "tier": tier},
    }


def test_paper_trader_opens_marks_and_closes(tmp_path):
    config = ScanConfig(paper_trader_enabled=True, paper_state_path=str(tmp_path / "paper_state.json"))
    trader = PaperTrader(build_paper_trader_config(config))

    opened = trader.update({"ts_ms": 1, "ts_iso": "2026-01-01T00:00:00+00:00", "opportunities": [_paper_opportunity(4.0)]})
    assert opened["open_positions"] == 1
    assert opened["cash_usd"] == 750.0
    assert opened["equity_usd"] == 1000.0

    marked = trader.update({"ts_ms": 2, "ts_iso": "2026-01-01T00:01:00+00:00", "opportunities": [_paper_opportunity(1.5)]})
    assert marked["open_positions"] == 1
    assert marked["unrealized_pnl_usd"] == 0.0625
    assert marked["equity_usd"] == 1000.0625

    closed = trader.update({"ts_ms": 3, "ts_iso": "2026-01-01T00:02:00+00:00", "opportunities": [_paper_opportunity(0.4)]})
    assert closed["open_positions"] == 0
    assert closed["realized_pnl_usd"] == 0.09
    assert closed["cash_usd"] == 1000.09
    assert closed["equity_usd"] == 1000.09


def test_paper_trader_state_persists_and_console_shows_summary(tmp_path):
    state_path = tmp_path / "paper_state.json"
    config = ScanConfig(paper_trader_enabled=True, paper_state_path=str(state_path))
    first = PaperTrader(build_paper_trader_config(config))
    summary = first.update({"ts_ms": 10, "ts_iso": "2026-01-01T00:00:00+00:00", "opportunities": [_paper_opportunity(3.2, tier="heads_up")]})
    assert state_path.exists()
    assert summary["open_positions"] == 1

    second = PaperTrader(build_paper_trader_config(config))
    persisted = second.update({"ts_ms": 11, "ts_iso": "2026-01-01T00:01:00+00:00", "opportunities": [_paper_opportunity(3.1, tier="heads_up")]})
    assert persisted["open_positions"] == 1
    assert persisted["cash_usd"] == 750.0

    rendered = format_console(
        {
            "ts_iso": "2026-01-01T00:01:00+00:00",
            "markets_scanned": 2,
            "duplicate_underlyings": 1,
            "opportunities": [_paper_opportunity(3.1, tier="heads_up")],
            "paper_portfolio": persisted,
        }
    )
    assert "paper | start=$1000.00 cash=$750.00" in rendered
