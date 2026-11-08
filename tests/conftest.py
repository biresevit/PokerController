import json
from pathlib import Path

import pytest


@pytest.fixture
def state():
    return json.loads((Path(__file__).parent / "fixtures/hero_turn.json").read_text())


@pytest.fixture(autouse=True)
def token_dir(tmp_path, monkeypatch):
    """Saved logins go to a temporary folder, never to the PokerSuite folder."""
    from poker_controller import token_store
    monkeypatch.setattr(token_store, "DIRECTORY", tmp_path / "tokens")
    return tmp_path / "tokens"


@pytest.fixture(autouse=True)
def no_human_pacing(monkeypatch):
    """Bot tests act at once; tests/test_pacing.py covers the waits themselves."""
    from poker_controller import bot
    monkeypatch.setattr(bot, "extra_seconds", lambda *a, **k: 0.0)
