import asyncio
from pathlib import Path
from types import SimpleNamespace

from poker_controller.worker import SITE, run_sites


class FakePilot:
    started = []
    failures = {}

    def __init__(self, args):
        self.args = args

    async def run_cloud_worker(self):
        site = self.args.site_id
        FakePilot.started.append((site, Path(self.args.output).name, SITE.get()))
        if FakePilot.failures.get(site):
            FakePilot.failures[site] -= 1
            raise RuntimeError("Machine has a live worker")
        while not self.args.stop_event.is_set() and not Path(self.args.stop_file).exists():
            await asyncio.sleep(.01)


async def test_one_worker_plays_every_site_of_its_pc_and_follows_changes(tmp_path):
    FakePilot.started, FakePilot.failures = [], {1004: 1}
    sites = [1, 1004]
    settings = SimpleNamespace(site_id=1, machine_id=7, output=str(tmp_path / "run"))

    def machine_sites(machine):
        assert machine == 7
        return list(sites)

    async def stop_later():
        while len(FakePilot.started) < 3:
            await asyncio.sleep(.01)
        sites.remove(1004)
        await asyncio.sleep(.3)
        (tmp_path / "run" / "stop").write_text("")

    (tmp_path / "run").mkdir()
    await asyncio.wait_for(asyncio.gather(run_sites(settings, FakePilot, machine_sites, poll=.05, restart=.05), stop_later()), 10)
    # site 1 writes to the run folder itself; site 1004 fails once and is started again, then is taken off the PC
    assert FakePilot.started[0] == (1, "run", "[site 1] ")
    assert [s for s in FakePilot.started if s[0] == 1004] == [(1004, "site-1004", "[site 1004] ")] * 2
    assert [s[0] for s in FakePilot.started].count(1) == 1


async def test_the_worker_plays_its_own_site_when_the_site_list_cannot_be_read(tmp_path):
    FakePilot.started, FakePilot.failures = [], {}
    settings = SimpleNamespace(site_id=5, machine_id=7, output=str(tmp_path))

    def machine_sites(machine):
        raise OSError("database unreachable")

    async def stop_later():
        while not FakePilot.started:
            await asyncio.sleep(.01)
        (tmp_path / "stop").write_text("")

    await asyncio.wait_for(asyncio.gather(run_sites(settings, FakePilot, machine_sites, poll=.05), stop_later()), 10)
    assert [s[0] for s in FakePilot.started] == [5]
