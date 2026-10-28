import json
from pathlib import Path

import pytest


@pytest.fixture
def state():
    return json.loads((Path(__file__).parent / "fixtures/hero_turn.json").read_text())
