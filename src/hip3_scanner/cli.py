from __future__ import annotations

import argparse
import sys
import time

from .config import ScanConfig
from .scanner import ScannerService, format_console


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hyperliquid HIP-3 cross-DEX scanner")
    parser.add_argument("command", choices=["once", "loop"], help="Run a single scan or loop forever")
    parser.add_argument("--config", dest="config_path", help="Path to JSON config")
    parser.add_argument("--interval", type=int, help="Loop interval seconds")
    parser.add_argument("--top", type=int, help="Max opportunities to print/store")
    parser.add_argument("--paper", action="store_true", help="Enable $1,000 paper trader / PnL simulation")
    parser.add_argument("--paper-state", help="Override paper trader state path")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    config = ScanConfig.from_sources(args.config_path)
    if args.interval is not None:
        config.poll_interval_seconds = args.interval
    if args.top is not None:
        config.top_n = args.top
    if args.paper:
        config.paper_trader_enabled = True
    if args.paper_state:
        config.paper_state_path = args.paper_state
    service = ScannerService(config)
    try:
        if args.command == "once":
            result = service.run_once()
            print(format_console(result))
            return 0
        while True:
            result = service.run_once()
            print(format_console(result))
            print("-" * 100)
            sys.stdout.flush()
            time.sleep(config.poll_interval_seconds)
    except KeyboardInterrupt:
        return 130
    finally:
        service.close()


if __name__ == "__main__":
    raise SystemExit(main())
