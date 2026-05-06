from __future__ import annotations

from typing import Any

import httpx


class HyperliquidClient:
    def __init__(self, base_url: str = "https://api.hyperliquid.xyz/info", timeout: float = 15.0):
        self.base_url = base_url
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def post(self, payload: dict[str, Any]) -> Any:
        response = self._client.post(self.base_url, json=payload)
        response.raise_for_status()
        return response.json()

    def fetch_perp_dexs(self) -> list[dict[str, Any]]:
        data = self.post({"type": "perpDexs"})
        if isinstance(data, dict) and "perpDexs" in data:
            return data["perpDexs"]
        return data

    def fetch_meta_and_asset_ctxs(self, dex: str) -> Any:
        return self.post({"type": "metaAndAssetCtxs", "dex": dex})

    def fetch_l2_book(self, coin: str, dex: str) -> dict[str, Any]:
        return self.post({"type": "l2Book", "coin": coin, "dex": dex})
