from pathlib import Path

import pytest

from poker_controller.import_profiles import PROFILE_IDS, plan, read_profiles


def test_import_plan_updates_only_changed_profiles_and_adds_missing(tmp_path: Path):
    csv_path = tmp_path / "profiles.csv"
    csv_path.write_text("nickname,profile\nAlice,Strong Tight\nbob,Passive Fish\nnew,Maniac\n",
                        encoding="utf-8")
    rows = read_profiles(csv_path)
    inserts, updates, unchanged = plan(rows, [("ALICE", 5), ("Bob", 4), ("unrelated", 5)])
    assert inserts == [("new", PROFILE_IDS["Maniac"])]
    assert updates == [("ALICE", PROFILE_IDS["Strong Tight"])]
    assert unchanged == 1


@pytest.mark.parametrize("content", [
    "nickname,profile\nAlice,Unknown\n",
    "nickname,profile\nAlice,Maniac\nalice,Strong Tight\n",
    "nickname,profile\nAlice\n",
    "profile,nickname\nManiac,Alice\n",
])
def test_import_rejects_bad_rows_before_database_write(tmp_path: Path, content: str):
    csv_path = tmp_path / "profiles.csv"
    csv_path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        read_profiles(csv_path)
