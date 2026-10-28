from __future__ import annotations

import httpx


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

    async def require_evenbet_hand_history(self) -> dict:
        body = await self.health()
        if body.get("handHistoryCasino") != "Evenbet" or body.get("handHistoryPlatform") != "EvenBet":
            raise RuntimeError("PDecision hand-history destination must be casino Evenbet and platform EvenBet")
        if not body.get("handHistoryUploadToAzure"):
            raise RuntimeError("PDecision hand-history Azure upload is disabled")
        return body

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
