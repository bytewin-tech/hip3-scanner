from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .client import HyperliquidClient
from .config import ScanConfig
from .core import evaluate_opportunities, normalize_market_snapshots, parse_venue_info
from .exchanges.hyperliquid import HyperliquidAdapter
from .live_trader import LiveTrader, build_live_trader_config
from .paper import PaperTrader, build_paper_trader_config


class ScannerService:
    def __init__(self, config: ScanConfig, client: HyperliquidClient | None = None):
        self.config = config
        self.client = client or HyperliquidClient()
        self.paper_trader = PaperTrader(build_paper_trader_config(config)) if config.paper_trader_enabled else None
        self.live_trader: LiveTrader | None = None
        if config.live_enabled:
            adapter = HyperliquidAdapter(
                base_url=config.hl_base_url,
                wallet_address=config.hl_wallet_address,
                private_key=config.hl_private_key,
                per_trade_notional=config.paper_per_trade_notional_usd,
                dry_run=config.live_dry_run,
            )
            self.live_trader = LiveTrader(
                build_live_trader_config(config, dry_run=config.live_dry_run),
                adapter,
            )

    def close(self) -> None:
        self.client.close()
        if self.live_trader is not None:
            self.live_trader.adapter.close()

    def _fetch_book(self, market_id: str, venue: str) -> dict[str, Any]:
        return self.client.fetch_l2_book(market_id, venue)

    def run_once(self) -> dict[str, Any]:
        ts_ms = int(time.time() * 1000)
        venue_entries = {
            entry["name"]: parse_venue_info(entry)
            for entry in self.client.fetch_perp_dexs()
            if isinstance(entry, dict) and entry.get("name")
        }
        normalized = []
        for dex in self.config.venues:
            payload = self.client.fetch_meta_and_asset_ctxs(dex)
            normalized.extend(normalize_market_snapshots(ts_ms, dex, venue_entries.get(dex), payload))
        opportunities = evaluate_opportunities(normalized, self.config, book_fetcher=self._fetch_book)
        result = {
            "ts_ms": ts_ms,
            "ts_iso": datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(),
            "markets_scanned": len(normalized),
            "duplicate_underlyings": len({m.underlying_key for m in normalized if sum(1 for x in normalized if x.underlying_key == m.underlying_key) >= 2}),
            "opportunities": opportunities[: self.config.top_n],
        }
        if self.paper_trader is not None:
            result["paper_portfolio"] = self.paper_trader.update(result)
        if self.live_trader is not None:
            result["live_portfolio"] = self.live_trader.update(result)
        self._write_jsonl(result)
        return result

    def _write_jsonl(self, result: dict[str, Any]) -> None:
        output_dir = Path(self.config.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        day = datetime.fromtimestamp(result["ts_ms"] / 1000, tz=timezone.utc).strftime("%Y%m%d")
        path = output_dir / f"opportunities_{day}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            for opp in result["opportunities"]:
                fh.write(json.dumps(opp) + "\n")


def format_console(result: dict[str, Any]) -> str:
    lines = []
    if result.get("paper_portfolio") or result.get("live_portfolio"):
        paper = result.get("paper_portfolio")
        live = result.get("live_portfolio")
        parts = []
        if paper:
            parts.append(
                f"paper | start=$%.2f cash=$%.2f equity=$%.2f open=%d realized=$%.2f unrealized=$%.2f total=$%.2f"
                % (
                    paper["starting_equity_usd"],
                    paper["cash_usd"],
                    paper["equity_usd"],
                    paper["open_positions"],
                    paper["realized_pnl_usd"],
                    paper["unrealized_pnl_usd"],
                    paper["total_pnl_usd"],
                )
            )
        if live:
            mode = "DRY_RUN" if live.get("dry_run") else "LIVE"
            paused = " [PAUSED]" if live.get("safety_paused") else ""
            parts.append(
                f"live  | {mode} | equity=$%.2f open=%d realized=$%.2f unrealized=$%.2f total=$%.2f{paused}"
                % (
                    live["equity_usd"],
                    live["open_positions"],
                    live["realized_pnl_usd"],
                    live["unrealized_pnl_usd"],
                    live["total_pnl_usd"],
                )
            )
        lines.extend(parts)
        lines.append("")

    lines.append(
        f"scan_ts={result['ts_iso']} markets={result['markets_scanned']} duplicate_underlyings={result['duplicate_underlyings']} opps={len(result['opportunities'])}"
    )
    lines.append(
        "  # | Underlying | Direction            | Tier               | Exec bps | Funding bps | Oracle bps | Score"
    )
    for idx, opp in enumerate(result["opportunities"], start=1):
        direction = f"buy {opp['direction']['buy_venue']} / sell {opp['direction']['sell_venue']}"
        exec_bps = opp["metrics"]["l2_executable_spread_bps"] or opp["metrics"]["impact_executable_spread_bps"]
        lines.append(
            f"{idx:>3} | {opp['underlying_key']:<10} | {direction:<20} | {opp['score']['tier']:<16} | {exec_bps:>9} | {opp['metrics']['funding_spread_bps']:>12} | {opp['metrics']['oracle_divergence_bps']:>11} | {opp['score']['raw_score']:>6}"
        )
    return "\n".join(lines)
