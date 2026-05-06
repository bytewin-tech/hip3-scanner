from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_VENUES = ["xyz", "flx", "vntl", "hyna", "km", "abcd", "cash", "para"]


@dataclass(slots=True)
class ScanConfig:
    venues: list[str] = field(default_factory=lambda: DEFAULT_VENUES.copy())
    poll_interval_seconds: int = 10
    top_n: int = 20
    output_dir: str = "./output"
    min_day_ntl_vlm: int = 25_000
    heads_up_day_ntl_vlm: int = 100_000
    strong_day_ntl_vlm: int = 500_000
    min_open_interest: int = 100
    strong_open_interest: int = 500
    candidate_exec_spread_bps: float = 1.0
    heads_up_exec_spread_bps: float = 1.5
    strong_exec_spread_bps: float = 3.0
    heads_up_mark_spread_bps: float = 4.0
    review_mark_spread_bps: float = 8.0
    max_oracle_divergence_bps: float = 15.0
    heads_up_oracle_divergence_bps: float = 8.0
    strong_oracle_divergence_bps: float = 5.0
    min_funding_spread_bps: float = 0.5
    review_funding_spread_bps: float = 2.0
    paper_trader_enabled: bool = False
    paper_initial_equity_usd: float = 1000.0
    paper_state_path: str = "./output/paper_trader_state.json"
    paper_per_trade_notional_usd: float = 250.0
    paper_max_open_positions: int = 3
    paper_min_entry_score: float = 3.0
    paper_min_entry_confidence: float = 0.55
    paper_min_entry_exec_spread_bps: float = 2.0
    paper_allowed_entry_tiers: list[str] = field(default_factory=lambda: ["heads_up", "strong_candidate"])
    paper_close_exec_spread_bps: float = 0.5
    paper_close_score_below: float = 1.0
    paper_max_holding_scans: int = 6
    paper_stop_loss_pct: float = -0.02
    # Safety / circuit breaker config
    paper_max_drawdown_pct: float = -0.15  # pause if equity falls 15% below peak
    paper_volatility_spike_bps: float = 25.0  # halt entries if median spread > 25 bps across venues
    paper_api_stale_threshold_seconds: int = 120  # halt if no fresh data in 2 minutes
    paper_cooldown_scans: int = 10  # how many scans to skip after a safety pause
    # Live trading (real money)
    live_enabled: bool = False
    live_state_path: str = "./output/live_trader_state.json"
    live_dry_run: bool = True
    live_require_human_approval: bool = False
    # Hyperliquid API keys (set in .env)
    hl_wallet_address: str = ""
    hl_secret_key_b64: str = ""
    hl_base_url: str = "https://api.hyperliquid.xyz"

    @classmethod
    def from_sources(cls, config_path: str | None = None) -> "ScanConfig":
        data: dict[str, object] = {}
        if config_path:
            data.update(json.loads(Path(config_path).read_text()))
        env_venues = os.getenv("HIP3_SCAN_VENUES")
        if env_venues:
            data["venues"] = [v.strip() for v in env_venues.split(",") if v.strip()]
        if os.getenv("HIP3_SCAN_INTERVAL_SECONDS"):
            data["poll_interval_seconds"] = int(os.environ["HIP3_SCAN_INTERVAL_SECONDS"])
        if os.getenv("HIP3_SCAN_OUTPUT_DIR"):
            data["output_dir"] = os.environ["HIP3_SCAN_OUTPUT_DIR"]
        if os.getenv("HIP3_SCAN_TOP_N"):
            data["top_n"] = int(os.environ["HIP3_SCAN_TOP_N"])
        if os.getenv("HIP3_PAPER_TRADER_ENABLED"):
            data["paper_trader_enabled"] = os.environ["HIP3_PAPER_TRADER_ENABLED"].lower() in {"1", "true", "yes", "on"}
        if os.getenv("HIP3_PAPER_STATE_PATH"):
            data["paper_state_path"] = os.environ["HIP3_PAPER_STATE_PATH"]
        # Live trading
        if os.getenv("HIP3_LIVE_ENABLED"):
            data["live_enabled"] = os.environ["HIP3_LIVE_ENABLED"].lower() in {"1", "true", "yes", "on"}
        if os.getenv("HIP3_LIVE_STATE_PATH"):
            data["live_state_path"] = os.environ["HIP3_LIVE_STATE_PATH"]
        if os.getenv("HIP3_LIVE_DRY_RUN"):
            data["live_dry_run"] = os.environ["HIP3_LIVE_DRY_RUN"].lower() not in {"0", "false", "no"}
        if os.getenv("HIP3_LIVE_REQUIRE_APPROVAL"):
            data["live_require_human_approval"] = os.environ["HIP3_LIVE_REQUIRE_APPROVAL"].lower() in {"1", "true", "yes", "on"}
        if os.getenv("HIP3_HL_WALLET_ADDRESS"):
            data["hl_wallet_address"] = os.environ["HIP3_HL_WALLET_ADDRESS"]
        if os.getenv("HIP3_HL_SECRET_KEY_B64"):
            data["hl_secret_key_b64"] = os.environ["HIP3_HL_SECRET_KEY_B64"]
        if os.getenv("HIP3_HL_BASE_URL"):
            data["hl_base_url"] = os.environ["HIP3_HL_BASE_URL"]
        return cls(**data)
