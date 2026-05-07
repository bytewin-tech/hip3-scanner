from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4


BPS_DIVISOR = Decimal("10000")


@dataclass(slots=True)
class PaperTraderConfig:
    enabled: bool
    initial_equity_usd: Decimal
    state_path: str
    per_trade_notional_usd: Decimal
    max_open_positions: int
    min_entry_score: float
    min_entry_confidence: float
    min_entry_exec_spread_bps: Decimal
    allowed_entry_tiers: tuple[str, ...]
    close_exec_spread_bps: Decimal
    close_score_below: float
    max_holding_scans: int
    stop_loss_pct: Decimal
    # Safety / circuit breakers
    max_drawdown_pct: Decimal
    volatility_spike_bps: Decimal
    api_stale_threshold_seconds: int
    cooldown_scans: int


class PaperTrader:
    def __init__(self, config: PaperTraderConfig):
        self.config = config
        self.state_path = Path(config.state_path)

    def update(self, result: dict[str, Any]) -> dict[str, Any]:
        state = self._load_state()
        state["last_scan_ts_ms"] = result["ts_ms"]
        state["last_scan_ts_iso"] = result["ts_iso"]
        opportunity_map = {opp["opportunity_id"]: opp for opp in result.get("opportunities", [])}

        self._mark_positions(state, opportunity_map, result)
        self._close_positions(state, opportunity_map, result)
        self._recompute_totals(state)

        # Safety checks — run after PnL recompute but before opening new positions
        self._check_api_health(state, result)
        self._check_volatility_spike(state, result.get("opportunities", []))
        self._check_circuit_breaker(state)

        # Only open new positions if not paused
        if not state.get("safety_paused"):
            self._open_positions(state, result.get("opportunities", []), result)
        else:
            # Decrement cooldown counter during pause
            if state.get("cooldown_remaining", 0) > 0:
                state["cooldown_remaining"] -= 1
            # Auto-resume when cooldown expires (for pauses not handled by individual check methods)
            if state.get("cooldown_remaining", 0) <= 0 and state.get("safety_paused"):
                state["safety_paused"] = False
                state["safety_pause_reason"] = None
                state["safety_pause_ts_iso"] = None

        self._save_state(state)
        return self._build_summary(state)

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
            # Circuit breaker state
            "safety_paused": False,
            "safety_pause_reason": None,
            "safety_pause_ts_iso": None,
            "cooldown_remaining": 0,
            "api_stale_count": 0,
            "consecutive_volatility_spikes": 0,
        }

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._default_state()
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _save_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")

    def _mark_positions(self, state: dict[str, Any], opportunity_map: dict[str, dict[str, Any]], result: dict[str, Any]) -> None:
        for position in state["positions"]:
            if position["status"] != "open":
                continue
            opp = opportunity_map.get(position["opportunity_id"])
            current_exec = self._current_exec_spread_bps(opp)
            if current_exec is None:
                current_exec = position["last_exec_spread_bps"]

            # PnL is based on MARK SPREAD convergence (the real arb signal),
            # not execution spread (a per-scan cost that barely changes).
            # Spread converges when the venue price gap shrinks → we profit.
            current_mark = self._metric_or_default(opp, "mark_spread_bps", position.get("last_mark_spread_bps", 0))
            spread_pnl = self._spread_pnl(
                position.get("entry_mark_spread_bps", position["entry_spread_bps"]),
                current_mark,
                position["notional_usd"],
            )

            position["last_scan_ts_ms"] = result["ts_ms"]
            position["last_scan_ts_iso"] = result["ts_iso"]
            position["last_exec_spread_bps"] = current_exec
            position["last_mark_spread_bps"] = current_mark
            position["last_funding_spread_bps"] = self._metric_or_default(opp, "funding_spread_bps", position["last_funding_spread_bps"])
            position["last_score"] = opp["score"]["raw_score"] if opp else position["last_score"]
            position["last_confidence"] = opp["score"]["confidence"] if opp else position["last_confidence"]
            position["last_tier"] = opp["score"]["tier"] if opp else position["last_tier"]
            position["unrealized_pnl_usd"] = spread_pnl
            position["holding_scans"] += 1
            position["signal_present"] = opp is not None

    def _close_positions(self, state: dict[str, Any], opportunity_map: dict[str, dict[str, Any]], result: dict[str, Any]) -> None:
        still_open: list[dict[str, Any]] = []
        for position in state["positions"]:
            close_reason = self._close_reason(position, opportunity_map.get(position["opportunity_id"]))
            if close_reason is None:
                still_open.append(position)
                continue
            closed = deepcopy(position)
            closed["status"] = "closed"
            closed["close_reason"] = close_reason
            closed["closed_ts_ms"] = result["ts_ms"]
            closed["closed_ts_iso"] = result["ts_iso"]
            closed["realized_pnl_usd"] = closed["unrealized_pnl_usd"]
            state["cash_usd"] = round(state["cash_usd"] + closed["notional_usd"] + closed["realized_pnl_usd"], 6)
            state["realized_pnl_usd"] = round(state["realized_pnl_usd"] + closed["realized_pnl_usd"], 6)
            state["closed_positions"].append(closed)
        state["positions"] = still_open

    def _open_positions(self, state: dict[str, Any], opportunities: list[dict[str, Any]], result: dict[str, Any]) -> None:
        open_underlyings = {position["underlying_key"] for position in state["positions"] if position["status"] == "open"}
        open_ids = {position["opportunity_id"] for position in state["positions"] if position["status"] == "open"}
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
            position = {
                "position_id": str(uuid4()),
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
                "entry_mark_spread_bps": opp["metrics"]["mark_spread_bps"],
                "entry_funding_spread_bps": opp["metrics"]["funding_spread_bps"],
                "last_exec_spread_bps": self._current_exec_spread_bps(opp),
                "last_mark_spread_bps": opp["metrics"]["mark_spread_bps"],
                "last_funding_spread_bps": opp["metrics"]["funding_spread_bps"],
                "notional_usd": round(notional, 6),
                "holding_scans": 0,
                "unrealized_pnl_usd": 0.0,
                "last_score": opp["score"]["raw_score"],
                "last_confidence": opp["score"]["confidence"],
                "last_tier": opp["score"]["tier"],
                "signal_present": True,
            }
            state["positions"].append(position)
            state["cash_usd"] = round(state["cash_usd"] - notional, 6)
            open_underlyings.add(opp["underlying_key"])
            open_ids.add(opp["opportunity_id"])
            open_count += 1

    def _close_reason(self, position: dict[str, Any], opp: dict[str, Any] | None) -> str | None:
        entry_mark = Decimal(str(position.get("entry_mark_spread_bps", position.get("entry_spread_bps", 0))))
        current_mark = Decimal(str(position.get("last_mark_spread_bps", 0)))
        last_exec = Decimal(str(position["last_exec_spread_bps"]))
        notional = Decimal(str(position["notional_usd"]))
        pnl_pct = Decimal(str(position["unrealized_pnl_usd"])) / notional if notional else Decimal("0")
        if not position.get("signal_present", True):
            return "signal_missing"
        # Close when mark spread has converged (venue prices aligning = arb captured)
        # Converged means: current mark spread is close to zero, or has shrunk significantly
        if entry_mark > 0 and current_mark <= entry_mark * Decimal("0.3"):
            return "spread_converged"
        if last_exec <= self.config.close_exec_spread_bps:
            return "exec_cost_too_high"
        if float(position["last_score"]) < self.config.close_score_below:
            return "score_deteriorated"
        if position["holding_scans"] >= self.config.max_holding_scans:
            return "max_holding_scans"
        if pnl_pct <= self.config.stop_loss_pct:
            return "stop_loss"
        if opp is not None and opp["score"]["tier"] not in self.config.allowed_entry_tiers:
            return "tier_deteriorated"
        return None

    def _check_circuit_breaker(self, state: dict[str, Any]) -> None:
        """Equity drawdown circuit breaker — pause if equity falls below max_drawdown_pct from peak."""
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
                # Auto-resume after cooldown if equity has recovered
                if drawdown_pct > threshold:
                    state["safety_paused"] = False
                    state["safety_pause_reason"] = None
                    state["safety_pause_ts_iso"] = None

    def _check_volatility_spike(self, state: dict[str, Any], opportunities: list[dict[str, Any]]) -> None:
        """Market-wide volatility circuit breaker — require N consecutive spike scans before pausing."""
        if not opportunities:
            return
        exec_spreads = [
            self._current_exec_spread_bps(opp)
            for opp in opportunities
            if self._current_exec_spread_bps(opp) is not None
        ]
        if not exec_spreads:
            return
        median_spread = sorted(exec_spreads)[len(exec_spreads) // 2]
        if median_spread > float(self.config.volatility_spike_bps):
            state["consecutive_volatility_spikes"] = state.get("consecutive_volatility_spikes", 0) + 1
            if state["consecutive_volatility_spikes"] >= 2 and not state.get("safety_paused"):
                state["safety_paused"] = True
                state["safety_pause_reason"] = f"volatility_spike_{median_spread:.1f}bps"
                state["safety_pause_ts_iso"] = datetime.now(tz=timezone.utc).isoformat()
                state["cooldown_remaining"] = self.config.cooldown_scans
        else:
            state["consecutive_volatility_spikes"] = 0
            # Resume from volatility pause after cooldown if spreads normalize
            if state.get("safety_paused") and state.get("safety_pause_reason", "").startswith("volatility_spike_"):
                if state["cooldown_remaining"] > 0:
                    state["cooldown_remaining"] -= 1
                if state["cooldown_remaining"] <= 0:
                    state["safety_paused"] = False
                    state["safety_pause_reason"] = None
                    state["safety_pause_ts_iso"] = None

    def _check_api_health(self, state: dict[str, Any], result: dict[str, Any]) -> None:
        """API health check — pause if data feed is stale or scan returned no opportunities repeatedly."""
        ts_ms = result.get("ts_ms")
        if ts_ms is None:
            state["api_stale_count"] = state.get("api_stale_count", 0) + 1
        else:
            from datetime import datetime as dt
            now_ms = int(dt.now(tz=timezone.utc).timestamp() * 1000)
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

    def _eligible_entry(self, opp: dict[str, Any]) -> bool:
        exec_spread = self._current_exec_spread_bps(opp)
        if exec_spread is None:
            return False
        exec_bps = Decimal(str(exec_spread))
        mark_spread = Decimal(str(opp["metrics"]["mark_spread_bps"]))
        # Minimum net arb buffer: mark spread must exceed exec spread by at least 1.5×.
        # e.g. if exec costs 5bps, need ≥7.5bps mark spread just to break even on costs.
        # Only then does spread convergence → profit.
        NET_ARB_RATIO = Decimal("1.5")
        if mark_spread < exec_bps * NET_ARB_RATIO:
            return False
        return (
            opp["score"]["tier"] in self.config.allowed_entry_tiers
            and float(opp["score"]["raw_score"]) >= self.config.min_entry_score
            and float(opp["score"]["confidence"]) >= self.config.min_entry_confidence
            and exec_bps >= self.config.min_entry_exec_spread_bps
        )

    def _recompute_totals(self, state: dict[str, Any]) -> None:
        state["unrealized_pnl_usd"] = round(sum(position["unrealized_pnl_usd"] for position in state["positions"] if position["status"] == "open"), 6)
        equity = round(state["cash_usd"] + sum(position["notional_usd"] for position in state["positions"] if position["status"] == "open") + state["unrealized_pnl_usd"], 6)
        state["equity_usd"] = equity
        # Track peak equity for drawdown circuit breaker
        if equity > state.get("peak_equity_usd", equity):
            state["peak_equity_usd"] = equity

    def _build_summary(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "enabled": True,
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
            # Safety status
            "safety_paused": state.get("safety_paused", False),
            "safety_pause_reason": state.get("safety_pause_reason"),
            "safety_pause_ts_iso": state.get("safety_pause_ts_iso"),
            "cooldown_remaining": state.get("cooldown_remaining", 0),
            "api_stale_count": state.get("api_stale_count", 0),
            "consecutive_volatility_spikes": state.get("consecutive_volatility_spikes", 0),
        }

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


def build_paper_trader_config(scan_config: Any) -> PaperTraderConfig:
    return PaperTraderConfig(
        enabled=bool(scan_config.paper_trader_enabled),
        initial_equity_usd=Decimal(str(scan_config.paper_initial_equity_usd)),
        state_path=scan_config.paper_state_path,
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
    )


def state_timestamp() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
