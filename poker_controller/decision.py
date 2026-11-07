from __future__ import annotations

import asyncio
import logging
import time

import httpx

log = logging.getLogger(__name__)

# While PDecision is swapped for a new version (or restarts) its port refuses connections or answers 503 for a few
# seconds. Requests that never reached a working PDecision are retried for this long; anything else (a timeout or
# a dropped connection after the request was sent) is not, because PDecision may already have processed it.
UNAVAILABLE_RETRY_SECONDS = 12

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

    async def post(self, path: str, body: dict, retry_seconds: float | None = None) -> httpx.Response:
        deadline = time.monotonic() + (UNAVAILABLE_RETRY_SECONDS if retry_seconds is None else retry_seconds)
        delay = 0.25
        while True:
            try:
                response = await self.http.post(path, json=body)
                if response.status_code != 503 or time.monotonic() >= deadline:
                    response.raise_for_status()
                    return response
                reason = "not ready (503)"
            except httpx.ConnectError as exc:
                if time.monotonic() >= deadline:
                    raise
                reason = f"unreachable ({type(exc).__name__})"
            log.warning("PDecision %s for %s; retrying", reason, path)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 1.0)

    async def decide(self, hand_state: dict) -> dict:
        return (await self.post("/api/decision", hand_state)).json()

    async def record(self, hand_state: dict):
        return bool((await self.post("/api/handhistory", hand_state)).json().get("accepted"))

    async def complete(self, hand_state: dict) -> bool:
        return bool((await self.post("/api/handhistory/complete", hand_state)).json().get("accepted"))

    async def close(self):
        await self.http.aclose()
