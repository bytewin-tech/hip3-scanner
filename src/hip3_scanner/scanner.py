from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .client import HyperliquidClient
from .config import ScanConfig
from .core import evaluate_opportunities, normalize_market_snapshots, parse_venue_info
from .paper import PaperTrader, build_paper_trader_config


class ScannerService:
    def __init__(self, config: ScanConfig, client: HyperliquidClient | None = None):
        self.config = config
        self.client = client or HyperliquidClient()
        self.paper_trader = PaperTrader(build_paper_trader_config(config)) if config.paper_trader_enabled else None

    def close(self) -> None:
        self.client.close()

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
    lines = [
        f"scan_ts={result['ts_iso']} markets={result['markets_scanned']} duplicate_underlyings={result['duplicate_underlyings']} opps={len(result['opportunities'])}",
        "rank | underlying | direction | tier | exec_bps | funding_bps | oracle_bps | score",
        "-----|------------|-----------|------|----------|-------------|------------|------",
    ]
    paper = result.get("paper_portfolio")
    if paper:
        lines.extend(
            [
                (
                    "paper | start=$%.2f cash=$%.2f equity=$%.2f open=%d realized=$%.2f unrealized=$%.2f total=$%.2f"
                    % (
                        paper["starting_equity_usd"],
                        paper["cash_usd"],
                        paper["equity_usd"],
                        paper["open_positions"],
                        paper["realized_pnl_usd"],
                        paper["unrealized_pnl_usd"],
                        paper["total_pnl_usd"],
                    )
                ),
                "",
            ]
        )
    for idx, opp in enumerate(result["opportunities"], start=1):
        direction = f"buy {opp['direction']['buy_venue']} / sell {opp['direction']['sell_venue']}"
        exec_bps = opp["metrics"]["l2_executable_spread_bps"] or opp["metrics"]["impact_executable_spread_bps"]
        lines.append(
            f"{idx:>4} | {opp['underlying_key']:<10} | {direction:<19} | {opp['score']['tier']:<16} | {exec_bps:>8} | {opp['metrics']['funding_spread_bps']:>11} | {opp['metrics']['oracle_divergence_bps']:>10} | {opp['score']['raw_score']:>6}"
        )
    return "\n".join(lines)
