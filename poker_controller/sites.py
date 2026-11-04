"""Poker platforms the controller can play, keyed by PokerSuite enPlatform."""
from __future__ import annotations

EVENBET = 11
BLUFFPOKER = 18  # enPlatform.BluffPokerPlatform (17 is HHPoker)

PLATFORMS = {"EvenBet": EVENBET, "BluffPokerPlatform": BLUFFPOKER}
# Where PDecision must save the hands of each platform (casino, accepted enPlatform names), as /api/health reports
# them. BluffPoker PCs registered before BluffPokerPlatform existed report RunYourOwnPokerSite.
HAND_HISTORY = {EVENBET: ("Evenbet", ("EvenBet",)),
                BLUFFPOKER: ("BluffPoker", ("BluffPokerPlatform", "RunYourOwnPokerSite"))}


def platform_id(name: str | int | None) -> int:
    """enPlatform value of a registered platform name (scheduler.Site.Platform, dbo.CustomerMachine.Platform)."""
    if name is None or name == "":
        return EVENBET
    if isinstance(name, int):
        if name not in HAND_HISTORY:
            raise ValueError(f"Unsupported platform {name}")
        return name
    for key, value in PLATFORMS.items():
        if key.casefold() == str(name).strip().casefold():
            return value
    raise ValueError(f"Unsupported platform {name!r}; supported: {', '.join(PLATFORMS)}")


def table_key(value) -> int | str:
    """The key a table is stored under: EvenBet's numeric ids as int, any other id (BluffPoker UUIDs) as text."""
    text = str(value).strip()
    return int(text) if text.isdigit() else text


def create_client(platform: int, username: str, *, endpoint_url: str | None = None):
    """A logged-out client for one bot account on the given platform."""
    if platform == BLUFFPOKER:
        from .bluffpoker import BluffPokerClient, BluffPokerSettings
        return BluffPokerClient(BluffPokerSettings.load(username, hub_url=endpoint_url))
    if platform == EVENBET:
        from .client import EvenBetClient, Settings
        return EvenBetClient(Settings(username=username))
    raise ValueError(f"Unsupported platform {platform}")
