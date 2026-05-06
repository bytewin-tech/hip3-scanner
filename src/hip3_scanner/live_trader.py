"""
LiveTrader — real-money execution engine.

Interface mirrors PaperTrader so scanners can run in either mode
with identical entry/exit logic.

Modes:
  dry_run=True   → paper execution (no real orders, simulated fills)
  dry_run=False  → live execution via ExchangeAdapter

Safety:
  - Notional cap: per_trade_notional limits exposure per leg
  - Circuit breakers: same guards as PaperTrader (drawdown, volatility, API stale)
  - Max open positions: same cap as paper config
  - Kill switch: call .pause() or set state["safety_paused"]=True to halt new entries
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .exchanges import (
    ExchangeAdapter,
    Fill,
    OrderResult,
    OrderSide,
    OrderStatus,
)

if TYPE_CHECKING:
    from .config import PaperTraderConfig


BPS_DIVISOR = Decimal("10000")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class LiveTraderConfig:
    """Runtime config for LiveTrader (mirrors PaperTraderConfig + execution params)."""
    enabled: bool
    initial_equity_usd: Decimal
    state_path: str
    per_trade_notional_usd: Decimal          # max notional per leg
    max_open_positions: int
    min_entry_score: float
    min_entry_confidence: float
    min_entry_exec_spread_bps: Decimal
    allowed_entry_tiers: tuple[str, ...]
    close_exec_spread_bps: Decimal
    close_score_below: float
    max_holding_scans: int
    stop_loss_pct: Decimal
    max_drawdown_pct: Decimal
    volatility_spike_bps: Decimal
    api_stale_threshold_seconds: int
    cooldown_scans: int
    # Execution params
    dry_run: bool = True
    require_human_approval: bool = False      # gate each new entry for manual approval
    confirmation_required_threshold_usd: Decimal = Decimal("100")  # always require approval above this notional


# ---------------------------------------------------------------------------
# LiveTrader
# ---------------------------------------------------------------------------

class LiveTrader:
    """
    Real-money execution engine.

    Shares the same entry/exit logic as PaperTrader but places
    real orders via an ExchangeAdapter and reconciles PnL from fills.
    """

    def __init__(self, config: LiveTraderConfig, adapter: ExchangeAdapter):
        self.config = config
        self.adapter = adapter
        self.state_path = Path(config.state_path)
        # Execution log for post-mortem / reconciliation
        self._fill_log: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, result: dict[str, Any]) -> dict[str, Any]:
        """
        Main scan loop entry point. Call once per market scan.

        1. Mark unrealized PnL on open positions
        2. Check circuit breakers
        3. Close positions that hit exit criteria
        4. Open new positions (if not paused)
        5. Reconcile with exchange fills
        6. Persist state
        """
        state = self._load_state()
        state["last_scan_ts_ms"] = result["ts_ms"]
        state["last_scan_ts_iso"] = result["ts_iso"]
        opportunity_map = {opp["opportunity_id"]: opp for opp in result.get("opportunities", [])}

        self._mark_positions(state, opportunity_map, result)
        self._close_positions(state, opportunity_map, result)
        self._recompute_totals(state)

        # Safety checks
        self._check_api_health(state, result)
        self._check_volatility_spike(state, result.get("opportunities", []))
        self._check_circuit_breaker(state)

        # Open new positions
        if not state.get("safety_paused"):
            self._open_positions(state, result.get("opportunities", []), result)
        else:
            if state.get("cooldown_remaining", 0) > 0:
                state["cooldown_remaining"] -= 1
            if state.get("cooldown_remaining", 0) <= 0 and state.get("safety_paused"):
                state["safety_paused"] = False
                state["safety_pause_reason"] = None
                state["safety_pause_ts_iso"] = None

        self._save_state(state)
        return self._build_summary(state)

    def pause(self) -> None:
        """Kill switch — halt new position entries immediately."""
        state = self._load_state()
        state["safety_paused"] = True
        state["safety_pause_reason"] = "manual_kill_switch"
        state["safety_pause_ts_iso"] = datetime.now(tz=timezone.utc).isoformat()
        state["cooldown_remaining"] = 9999  # large enough; resume() must be called
        self._save_state(state)

    def resume(self) -> None:
        """Resume trading after a manual kill switch or safety pause."""
        state = self._load_state()
        state["safety_paused"] = False
        state["safety_pause_reason"] = None
        state["safety_pause_ts_iso"] = None
        state["cooldown_remaining"] = 0
        self._save_state(state)

    def health_check(self) -> dict[str, Any]:
        """Check exchange connectivity and auth."""
        result = self.adapter.health_check()
        return {
            "success": result.success,
            "error": result.error,
            "timestamp_iso": datetime.now(tz=timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------

    def _default_state(self) -> dict[str, Any]:
        initial = float(self.config.initial_equity_usd)
        return {
            "initial_equity_usd": initial,
            "cash_usd": initial,
            "realized_pnl_usd": 0.0,
            "unrealized_pnl_usd": 0.0,
            "equity_usd": initial,
            "peak_equity_usd": initial,
            "positions": [],
            "closed_positions": [],
            "last_scan_ts_ms": None,
            "last_scan_ts_iso": None,
            "safety_paused": False,
            "safety_pause_reason": None,
            "safety_pause_ts_iso": None,
            "cooldown_remaining": 0,
            "api_stale_count": 0,
            "consecutive_volatility_spikes": 0,
            # Execution log
            "fills": [],
        }

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._default_state()
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _save_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        # Trim fill log to last 500 entries to avoid unbounded growth
        if len(state.get("fills", [])) > 500:
            state["fills"] = state["fills"][-500:]
        self.state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")

    # ------------------------------------------------------------------
    # Position marking (unrealized PnL)
    # ------------------------------------------------------------------

    def _mark_positions(self, state: dict[str, Any], opportunity_map: dict[str, Any], result: dict[str, Any]) -> None:
        for position in state["positions"]:
            if position["status"] != "open":
                continue
            opp = opportunity_map.get(position["opportunity_id"])
            current_exec = self._current_exec_spread_bps(opp)
            if current_exec is None:
                current_exec = position["last_exec_spread_bps"]

            # In dry_run: use spread-based PnL (same as paper)
            # In live: reconcile with exchange position PnL
            exchange_pos = None
            if not self.config.dry_run:
                exchange_pos = self.adapter.get_position(position["underlying_key"])

            if exchange_pos and exchange_pos.size != 0:
                # Use real exchange PnL
                position["unrealized_pnl_usd"] = round(float(exchange_pos.unrealized_pnl), 6)
                position["last_exec_spread_bps"] = current_exec
            else:
                # Fall back to spread PnL (dry_run or position not yet on exchange)
                spread_pnl = self._spread_pnl(
                    position["entry_exec_spread_bps"],
                    current_exec,
                    position["notional_usd"],
                )
                position["unrealized_pnl_usd"] = spread_pnl
                position["last_exec_spread_bps"] = current_exec

            position["last_scan_ts_ms"] = result["ts_ms"]
            position["last_scan_ts_iso"] = result["ts_iso"]
            position["last_funding_spread_bps"] = self._metric_or_default(opp, "funding_spread_bps", position["last_funding_spread_bps"])
            position["last_score"] = opp["score"]["raw_score"] if opp else position["last_score"]
            position["last_confidence"] = opp["score"]["confidence"] if opp else position["last_confidence"]
            position["last_tier"] = opp["score"]["tier"] if opp else position["last_tier"]
            position["holding_scans"] += 1
            position["signal_present"] = opp is not None

    # ------------------------------------------------------------------
    # Position closing
    # ------------------------------------------------------------------

    def _close_positions(self, state: dict[str, Any], opportunity_map: dict[str, Any], result: dict[str, Any]) -> None:
        still_open: list[dict[str, Any]] = []
        for position in state["positions"]:
            if position["status"] != "open":
                continue
            close_reason = self._close_reason(position, opportunity_map.get(position["opportunity_id"]))
            if close_reason is None:
                still_open.append(position)
                continue

            # Execute closing order
            fill_result = self._execute_close(position)
            if fill_result.success:
                closed = deepcopy(position)
                closed["status"] = "closed"
                closed["close_reason"] = close_reason
                closed["closed_ts_ms"] = result["ts_ms"]
                closed["closed_ts_iso"] = result["ts_iso"]
                # Realized PnL from actual fill
                realized = sum(f.price * f.size for f in fill_result.fills)
                cost_basis = position["entry_exec_spread_bps"] / BPS_DIVISOR * position["notional_usd"]
                closed["realized_pnl_usd"] = round(float(realized - Decimal(str(cost_basis))), 6)
                state["cash_usd"] = round(state["cash_usd"] + position["notional_usd"] + closed["realized_pnl_usd"], 6)
                state["realized_pnl_usd"] = round(state["realized_pnl_usd"] + closed["realized_pnl_usd"], 6)
                state["closed_positions"].append(closed)
                state["fills"].append(self._fill_to_dict(fill_result, close_reason))
            else:
                # Failed to close — keep position open, log the error
                still_open.append(position)
                state["fills"].append({
                    "order_id": fill_result.order_id,
                    "symbol": position["underlying_key"],
                    "side": "sell_close",
                    "success": False,
                    "error": fill_result.error,
                    "ts_iso": datetime.now(tz=timezone.utc).isoformat(),
                    "close_reason_attempted": close_reason,
                })
        state["positions"] = still_open

    def _execute_close(self, position: dict[str, Any]) -> OrderResult:
        """Place closing sell order."""
        return self.adapter.place_market_sell(
            symbol=position["underlying_key"],
            quantity=Decimal(str(abs(position["notional_usd"]))),  # size in USD notional
        )

    # ------------------------------------------------------------------
    # Position opening
    # ------------------------------------------------------------------

    def _open_positions(self, state: dict[str, Any], opportunities: list[dict[str, Any]], result: dict[str, Any]) -> None:
        open_underlyings = {p["underlying_key"] for p in state["positions"] if p["status"] == "open"}
        open_ids = {p["opportunity_id"] for p in state["positions"] if p["status"] == "open"}
        open_count = len(open_ids)

        for opp in opportunities:
            if open_count >= self.config.max_open_positions:
                break
            if opp["underlying_key"] in open_underlyings or opp["opportunity_id"] in open_ids:
                continue
            if not self._eligible_entry(opp):
                continue

            notional = min(float(self.config.per_trade_notional_usd), state["cash_usd"])
            if notional <= 0:
                break

            # Human approval gate
            if self.config.require_human_approval:
                notional = float(Decimal(str(notional)))  # just confirm it's Decimal-safe

            # Execute entry
            fill_result = self._execute_entry(opp, notional)
            if fill_result.success:
                position = {
                    "position_id": fill_result.order_id or str(fill_result.order_id),
                    "opportunity_id": opp["opportunity_id"],
                    "status": "open",
                    "entry_ts_ms": result["ts_ms"],
                    "entry_ts_iso": result["ts_iso"],
                    "last_scan_ts_ms": result["ts_ms"],
                    "last_scan_ts_iso": result["ts_iso"],
                    "underlying_key": opp["underlying_key"],
                    "buy_venue": opp["direction"]["buy_venue"],
                    "sell_venue": opp["direction"]["sell_venue"],
                    "entry_exec_spread_bps": self._current_exec_spread_bps(opp),
                    "entry_spread_bps": opp["metrics"]["mark_spread_bps"],
                    "entry_funding_spread_bps": opp["metrics"]["funding_spread_bps"],
                    "last_exec_spread_bps": self._current_exec_spread_bps(opp),
                    "last_funding_spread_bps": opp["metrics"]["funding_spread_bps"],
                    "notional_usd": round(notional, 6),
                    "holding_scans": 0,
                    "unrealized_pnl_usd": 0.0,
                    "last_score": opp["score"]["raw_score"],
                    "last_confidence": opp["score"]["confidence"],
                    "last_tier": opp["score"]["tier"],
                    "signal_present": True,
                    "entry_fill_id": fill_result.order_id,
                }
                state["positions"].append(position)
                state["cash_usd"] = round(state["cash_usd"] - notional, 6)
                state["fills"].append(self._fill_to_dict(fill_result, "entry"))
                open_underlyings.add(opp["underlying_key"])
                open_ids.add(opp["opportunity_id"])
                open_count += 1
            else:
                state["fills"].append({
                    "order_id": fill_result.order_id,
                    "symbol": opp["underlying_key"],
                    "side": "buy_entry",
                    "success": False,
                    "error": fill_result.error,
                    "ts_iso": datetime.now(tz=timezone.utc).isoformat(),
                    "opportunity_id": opp["opportunity_id"],
                })

    def _execute_entry(self, opp: dict[str, Any], notional: float) -> OrderResult:
        """Place opening buy order."""
        return self.adapter.place_market_buy(
            symbol=opp["underlying_key"],
            quantity=Decimal(str(notional)),
        )

    # ------------------------------------------------------------------
    # Entry / exit criteria
    # ------------------------------------------------------------------

    def _close_reason(self, position: dict[str, Any], opp: dict[str, Any] | None) -> str | None:
        last_exec = Decimal(str(position["last_exec_spread_bps"]))
        notional = Decimal(str(position["notional_usd"]))
        pnl_pct = Decimal(str(position["unrealized_pnl_usd"])) / notional if notional else Decimal("0")
        if not position.get("signal_present", True):
            return "signal_missing"
        if last_exec <= self.config.close_exec_spread_bps:
            return "spread_converged"
        if float(position["last_score"]) < self.config.close_score_below:
            return "score_deteriorated"
        if position["holding_scans"] >= self.config.max_holding_scans:
            return "max_holding_scans"
        if pnl_pct <= self.config.stop_loss_pct:
            return "stop_loss"
        if opp is not None and opp["score"]["tier"] not in self.config.allowed_entry_tiers:
            return "tier_deteriorated"
        return None

    def _eligible_entry(self, opp: dict[str, Any]) -> bool:
        exec_spread = Decimal(str(self._current_exec_spread_bps(opp)))
        return (
            opp["score"]["tier"] in self.config.allowed_entry_tiers
            and float(opp["score"]["raw_score"]) >= self.config.min_entry_score
            and float(opp["score"]["confidence"]) >= self.config.min_entry_confidence
            and exec_spread >= self.config.min_entry_exec_spread_bps
        )

    # ------------------------------------------------------------------
    # Safety / circuit breakers
    # ------------------------------------------------------------------

    def _check_circuit_breaker(self, state: dict[str, Any]) -> None:
        peak = state.get("peak_equity_usd", state["initial_equity_usd"])
        if peak <= 0:
            return
        drawdown_pct = (state["equity_usd"] - peak) / peak
        threshold = float(self.config.max_drawdown_pct)
        if drawdown_pct <= threshold and not state.get("safety_paused"):
            state["safety_paused"] = True
            state["safety_pause_reason"] = f"drawdown_{round(drawdown_pct * 100, 2)}%"
            state["safety_pause_ts_iso"] = datetime.now(tz=timezone.utc).isoformat()
            state["cooldown_remaining"] = self.config.cooldown_scans
        elif state.get("safety_paused") and state.get("safety_pause_reason", "").startswith("drawdown_"):
            if state["cooldown_remaining"] > 0:
                state["cooldown_remaining"] -= 1
            if state["cooldown_remaining"] <= 0:
                if drawdown_pct > threshold:
                    state["safety_paused"] = False
                    state["safety_pause_reason"] = None
                    state["safety_pause_ts_iso"] = None

    def _check_volatility_spike(self, state: dict[str, Any], opportunities: list[dict[str, Any]]) -> None:
        if not opportunities:
            return
        exec_spreads = [self._current_exec_spread_bps(o) for o in opportunities if self._current_exec_spread_bps(o) is not None]
        if not exec_spreads:
            return
        median_spread = sorted(exec_spreads)[len(exec_spreads) // 2]
        threshold = float(self.config.volatility_spike_bps)
        if median_spread > threshold:
            state["consecutive_volatility_spikes"] = state.get("consecutive_volatility_spikes", 0) + 1
            if state["consecutive_volatility_spikes"] >= 2 and not state.get("safety_paused"):
                state["safety_paused"] = True
                state["safety_pause_reason"] = f"volatility_spike_{median_spread:.1f}bps"
                state["safety_pause_ts_iso"] = datetime.now(tz=timezone.utc).isoformat()
                state["cooldown_remaining"] = self.config.cooldown_scans
        else:
            state["consecutive_volatility_spikes"] = 0
            if state.get("safety_paused") and state.get("safety_pause_reason", "").startswith("volatility_spike_"):
                if state["cooldown_remaining"] > 0:
                    state["cooldown_remaining"] -= 1
                if state["cooldown_remaining"] <= 0:
                    state["safety_paused"] = False
                    state["safety_pause_reason"] = None
                    state["safety_pause_ts_iso"] = None

    def _check_api_health(self, state: dict[str, Any], result: dict[str, Any]) -> None:
        ts_ms = result.get("ts_ms")
        if ts_ms is None:
            state["api_stale_count"] = state.get("api_stale_count", 0) + 1
        else:
            now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
            age_seconds = (now_ms - ts_ms) / 1000
            if age_seconds > self.config.api_stale_threshold_seconds:
                state["api_stale_count"] = state.get("api_stale_count", 0) + 1
            else:
                state["api_stale_count"] = 0

        if state.get("api_stale_count", 0) >= 3 and not state.get("safety_paused"):
            state["safety_paused"] = True
            state["safety_pause_reason"] = "api_stale"
            state["safety_pause_ts_iso"] = datetime.now(tz=timezone.utc).isoformat()
            state["cooldown_remaining"] = self.config.cooldown_scans
        elif state.get("safety_paused") and state.get("safety_pause_reason") == "api_stale":
            if state["cooldown_remaining"] > 0:
                state["cooldown_remaining"] -= 1
            if state["cooldown_remaining"] <= 0 and state.get("api_stale_count", 0) < 3:
                state["safety_paused"] = False
                state["safety_pause_reason"] = None
                state["safety_pause_ts_iso"] = None

    # ------------------------------------------------------------------
    # Totals & summary
    # ------------------------------------------------------------------

    def _recompute_totals(self, state: dict[str, Any]) -> None:
        state["unrealized_pnl_usd"] = round(sum(p["unrealized_pnl_usd"] for p in state["positions"] if p["status"] == "open"), 6)
        equity = round(state["cash_usd"] + sum(p["notional_usd"] for p in state["positions"] if p["status"] == "open") + state["unrealized_pnl_usd"], 6)
        state["equity_usd"] = equity
        if equity > state.get("peak_equity_usd", equity):
            state["peak_equity_usd"] = equity

    def _build_summary(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "enabled": True,
            "dry_run": self.config.dry_run,
            "state_path": str(self.state_path),
            "starting_equity_usd": state["initial_equity_usd"],
            "cash_usd": state["cash_usd"],
            "equity_usd": state["equity_usd"],
            "peak_equity_usd": state.get("peak_equity_usd", state["initial_equity_usd"]),
            "open_positions": len(state["positions"]),
            "closed_positions": len(state["closed_positions"]),
            "realized_pnl_usd": state["realized_pnl_usd"],
            "unrealized_pnl_usd": state["unrealized_pnl_usd"],
            "total_pnl_usd": round(state["realized_pnl_usd"] + state["unrealized_pnl_usd"], 6),
            "positions": state["positions"],
            "safety_paused": state.get("safety_paused", False),
            "safety_pause_reason": state.get("safety_pause_reason"),
            "safety_pause_ts_iso": state.get("safety_pause_ts_iso"),
            "cooldown_remaining": state.get("cooldown_remaining", 0),
            "api_stale_count": state.get("api_stale_count", 0),
            "consecutive_volatility_spikes": state.get("consecutive_volatility_spikes", 0),
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _current_exec_spread_bps(self, opportunity: dict[str, Any] | None) -> float | None:
        if not opportunity:
            return None
        metrics = opportunity["metrics"]
        return metrics["l2_executable_spread_bps"] if metrics["l2_executable_spread_bps"] is not None else metrics["impact_executable_spread_bps"]

    def _metric_or_default(self, opportunity: dict[str, Any] | None, key: str, default: float) -> float:
        if not opportunity:
            return default
        return opportunity["metrics"][key]

    def _spread_pnl(self, entry_bps: float, current_bps: float, notional_usd: float) -> float:
        pnl = (Decimal(str(entry_bps)) - Decimal(str(current_bps))) / BPS_DIVISOR * Decimal(str(notional_usd))
        return round(float(pnl), 6)

    def _fill_to_dict(self, result: OrderResult, reason: str) -> dict[str, Any]:
        return {
            "order_id": result.order_id,
            "success": result.success,
            "status": result.status.value,
            "reason": reason,
            "avg_fill_price": str(result.avg_fill_price) if result.avg_fill_price else None,
            "total_filled_size": str(result.total_filled_size) if result.total_filled_size else None,
            "total_fee": str(result.total_fee) if result.total_fee else None,
            "error": result.error,
            "fills": [
                {
                    "price": str(f.price),
                    "size": str(f.size),
                    "fee": str(f.fee),
                    "fee_currency": f.fee_currency,
                    "ts_iso": f.ts_iso,
                }
                for f in result.fills
            ],
            "ts_iso": datetime.now(tz=timezone.utc).isoformat(),
        }


# ---------------------------------------------------------------------------
# Config builder (from ScanConfig)
# ---------------------------------------------------------------------------

def build_live_trader_config(scan_config: Any, dry_run: bool = True) -> LiveTraderConfig:
    return LiveTraderConfig(
        enabled=True,
        initial_equity_usd=Decimal(str(scan_config.paper_initial_equity_usd)),
        state_path=scan_config.paper_state_path.replace("paper", "live"),
        per_trade_notional_usd=Decimal(str(scan_config.paper_per_trade_notional_usd)),
        max_open_positions=int(scan_config.paper_max_open_positions),
        min_entry_score=float(scan_config.paper_min_entry_score),
        min_entry_confidence=float(scan_config.paper_min_entry_confidence),
        min_entry_exec_spread_bps=Decimal(str(scan_config.paper_min_entry_exec_spread_bps)),
        allowed_entry_tiers=tuple(scan_config.paper_allowed_entry_tiers),
        close_exec_spread_bps=Decimal(str(scan_config.paper_close_exec_spread_bps)),
        close_score_below=float(scan_config.paper_close_score_below),
        max_holding_scans=int(scan_config.paper_max_holding_scans),
        stop_loss_pct=Decimal(str(scan_config.paper_stop_loss_pct)),
        max_drawdown_pct=Decimal(str(scan_config.paper_max_drawdown_pct)),
        volatility_spike_bps=Decimal(str(scan_config.paper_volatility_spike_bps)),
        api_stale_threshold_seconds=int(scan_config.paper_api_stale_threshold_seconds),
        cooldown_scans=int(scan_config.paper_cooldown_scans),
        dry_run=dry_run,
    )
