import asyncio
import httpx
import pytest
from poker_controller import decision as module
from poker_controller.decision import PDecision


def run(coro):
    return asyncio.run(coro)


def client(handler):
    return PDecision(client=httpx.AsyncClient(base_url="http://pdecision", transport=httpx.MockTransport(handler)))


def test_decision_waits_while_pdecision_is_swapped(monkeypatch):
    real_sleep = asyncio.sleep
    async def no_wait(_):
        await real_sleep(0)
    monkeypatch.setattr(module.asyncio, "sleep", no_wait)
    calls = []
    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("refused", request=request)
        if len(calls) == 2:
            return httpx.Response(503, json={"status": "initializing"})
        return httpx.Response(200, json={"Action": 2})
    assert run(client(handler).decide({}))["Action"] == 2
    assert len(calls) == 3


def test_ambiguous_failures_are_not_retried(monkeypatch):
    calls = []
    def handler(request):
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=request)
    with pytest.raises(httpx.ReadTimeout):
        run(client(handler).decide({}))
    assert len(calls) == 1


def test_gives_up_after_the_retry_window(monkeypatch):
    monkeypatch.setattr(module, "UNAVAILABLE_RETRY_SECONDS", 0)
    def handler(request):
        return httpx.Response(503, json={"status": "initializing"})
    with pytest.raises(httpx.HTTPStatusError):
        run(client(handler).decide({}))


def test_once_unreachable_later_actions_fold_at_once(monkeypatch):
    monkeypatch.setattr(module, "UNAVAILABLE_RETRY_SECONDS", 0)
    calls, up = [], {"value": False}
    def handler(request):
        calls.append(1)
        if not up["value"]:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"Action": 2})
    pdecision = client(handler)
    with pytest.raises(httpx.ConnectError):
        run(pdecision.decide({}))
    assert pdecision.unavailable
    monkeypatch.setattr(module, "UNAVAILABLE_RETRY_SECONDS", 3600)  # would wait an hour if it retried
    calls.clear()
    with pytest.raises(httpx.ConnectError):
        run(pdecision.decide({}))
    assert len(calls) == 1  # a single attempt: the bot checks/folds at once
    up["value"] = True
    assert run(pdecision.decide({}))["Action"] == 2
    assert not pdecision.unavailable


def test_join_waits_for_a_starting_pdecision(monkeypatch):
    real_sleep = asyncio.sleep
    async def no_wait(_):
        await real_sleep(0)
    monkeypatch.setattr(module.asyncio, "sleep", no_wait)
    calls = []
    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("refused", request=request)
        if len(calls) == 2:
            return httpx.Response(503, json={"status": "initializing"})
        if len(calls) == 3:
            return httpx.Response(200, json={"status": "initializing"})
        return httpx.Response(200, json={"status": "ready", "casinos": [{"casino": "Evenbet", "platform": "EvenBet"}],
                                         "handHistoryUploadToAzure": True})
    assert run(client(handler).wait_ready())["status"] == "ready"
    assert len(calls) == 4
