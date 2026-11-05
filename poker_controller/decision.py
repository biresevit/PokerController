from __future__ import annotations

import httpx

from .sites import EVENBET, HAND_HISTORY


class PDecision:
    def __init__(self, url="http://127.0.0.1:5005", api_key="", client: httpx.AsyncClient | None = None):
        self.http = client or httpx.AsyncClient(base_url=url, timeout=45,
            headers={"X-Api-Key": api_key} if api_key else {})

    async def health(self) -> dict:
        response = await self.http.get("/api/health")
        response.raise_for_status()
        body = response.json()
        if body.get("status") != "ready":
            raise RuntimeError(f"PDecision isn't ready: {body}")
        return body

    async def require_hand_history(self, platform: int = EVENBET) -> dict:
        """PDecision must save this platform's hands under its own casino and platform, uploaded to Azure."""
        casino, names = HAND_HISTORY[platform]
        body = await self.health()
        # A PDecision serving several poker sites lists them all; an older one reports its only casino.
        served = body.get("casinos") or [{"casino": body.get("handHistoryCasino"), "platform": body.get("handHistoryPlatform")}]
        if not any(c.get("casino") == casino and c.get("platform") in names for c in served):
            raise RuntimeError(f"PDecision hand-history destination must be casino {casino} and platform {names[0]}")
        if not body.get("handHistoryUploadToAzure"):
            raise RuntimeError("PDecision hand-history Azure upload is disabled")
        return body

    async def require_evenbet_hand_history(self) -> dict:
        return await self.require_hand_history(EVENBET)

    async def decide(self, hand_state: dict) -> dict:
        response = await self.http.post("/api/decision", json=hand_state)
        response.raise_for_status()
        return response.json()

    async def record(self, hand_state: dict):
        response = await self.http.post("/api/handhistory", json=hand_state)
        response.raise_for_status()
        return bool(response.json().get("accepted"))

    async def complete(self, hand_state: dict) -> bool:
        response = await self.http.post("/api/handhistory/complete", json=hand_state)
        response.raise_for_status()
        return bool(response.json().get("accepted"))

    async def close(self):
        await self.http.aclose()
