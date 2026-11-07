"""Local PokerController worker for commands planned by the Azure Function."""
import argparse
import asyncio
import contextvars
import logging
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

log = logging.getLogger(__name__)
# The poker site a log line belongs to, on a PC that plays several.
SITE = contextvars.ContextVar("site", default="")
SITE_POLL_SECONDS = 60
# A site that stops is started again quickly, so a crash costs few seconds of play and the seats are likely still
# there. A site that keeps failing (a blocked or rate-limited login) waits twice as long each time, up to five
# minutes: retrying every 30 s spent a login each time and kept the PC's IP rate-limited (site 1004, 2026-11-07).
RESTART_SECONDS = 5
MAX_RESTART_SECONDS = 300
HEALTHY_RUN_SECONDS = 600


def restart_delay(failures: int, restart: float = RESTART_SECONDS, max_restart: float = MAX_RESTART_SECONDS) -> float:
    """Seconds before the next start of a site that failed `failures` times in a row."""
    return min(max_restart, restart * 2 ** (max(failures, 1) - 1))


class SiteFilter(logging.Filter):
    def filter(self, record):
        record.site = SITE.get()
        return True


async def run_sites(settings, pilot_factory, machine_sites, *, poll=SITE_POLL_SECONDS, restart=RESTART_SECONDS,
                    max_restart=MAX_RESTART_SECONDS, healthy=HEALTHY_RUN_SECONDS):
    """One Pilot per poker site the PC plays, started when a site is added to the PC, stopped when it is taken off.

    The worker's own site (--site-id) is played also when the site list can't be read. A Pilot that fails is started
    again after `restart` seconds, doubling after each failure that follows a short run (up to `max_restart`). The first Pilot writes to the output folder itself (status.json, read by the
    updater), the others to a site-<id> folder in it; the stop marker in the output folder stops them all.
    """
    root = Path(settings.output)
    stop_file = root / "stop"
    runs, retry_at, failures, started_at = {}, {}, {}, {}

    async def play(site, args):
        SITE.set(f"[site {site}] ")
        await pilot_factory(args).run_cloud_worker()

    def start(site):
        output = root if not runs else root / f"site-{site}"
        args = SimpleNamespace(**{**vars(settings), "site_id": site, "output": str(output),
                                  "stop_file": str(stop_file), "stop_event": asyncio.Event()})
        runs[site] = (asyncio.create_task(play(site, args)), args.stop_event)
        started_at[site] = time.monotonic()
        log.info("Playing poker site %s on machine %s", site, settings.machine_id)

    next_poll = 0.0
    while not stop_file.exists():
        if time.monotonic() >= next_poll:
            try:
                wanted = await asyncio.to_thread(machine_sites, settings.machine_id) or [settings.site_id]
            except Exception as exc:
                log.warning("Could not read the poker sites of machine %s: %s", settings.machine_id, exc)
                wanted = [site for site, (task, _) in runs.items() if not task.done()] or [settings.site_id]
            for site, (task, event) in runs.items():
                if site not in wanted and not event.is_set():
                    log.info("Poker site %s was taken off machine %s; stopping it", site, settings.machine_id)
                    event.set()
            next_poll = time.monotonic() + poll
        for site in wanted:
            run = runs.get(site)
            if run and run[0].done() and not run[1].is_set() and site not in retry_at:
                error = run[0].exception() if not run[0].cancelled() else None
                if time.monotonic() - started_at.get(site, 0) >= healthy:
                    failures[site] = 0
                failures[site] = failures.get(site, 0) + 1
                delay = restart_delay(failures[site], restart, max_restart)
                log.warning("Poker site %s stopped (%s); starting it again in %s s", site, error or "ended", delay)
                retry_at[site] = time.monotonic() + delay
            if (not run or run[0].done()) and time.monotonic() >= retry_at.get(site, 0):
                retry_at.pop(site, None)
                start(site)
        await asyncio.sleep(1)
    await asyncio.gather(*(task for task, _ in runs.values()), return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-id",type=int,default=int(os.environ.get("POKER_SITE_ID","1")))
    parser.add_argument("--machine-id",type=int,default=int(os.environ.get("POKER_MACHINE_ID","1")))
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--output", default="artifacts/azure-worker")
    parser.add_argument("--leave-on-exit", action="store_true",
        help="Leave all poker tables before stopping. By default seats are preserved for restart recovery.")
    args = parser.parse_args()
    scheduler_root = Path(__file__).resolve().parents[2] / "ScheduleController"
    if scheduler_root.is_dir():
        sys.path.insert(0, str(scheduler_root))
    from schedule_controller.pilot import Pilot
    from schedule_controller.pilot_repository import machine_sites
    from poker_controller.system_metrics import sample as machine_metrics,POKER_CONTROLLER_VERSION
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    if (output / "stop").exists():
        parser.error("Choose a fresh output directory; this one has a stop marker")
    handlers = [logging.FileHandler(output / "worker.log", encoding="utf-8"), logging.StreamHandler()]
    for handler in handlers:
        handler.addFilter(SiteFilter())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(site)s%(message)s", handlers=handlers, force=True)
    for name in ("httpx", "httpcore", "websockets"):
        logging.getLogger(name).setLevel(logging.WARNING)
    from poker_controller.diagnostics import start_collector
    collector=start_collector(args.machine_id)
    settings = SimpleNamespace(controller_version=POKER_CONTROLLER_VERSION,site_id=args.site_id,machine_id=args.machine_id,seconds=0, seed=args.seed, output=str(output), target=0,
        leave_on_exit=args.leave_on_exit, telemetry_provider=machine_metrics)
    try:
        asyncio.run(run_sites(settings, Pilot, machine_sites))
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("PokerController worker stopped by operator")
    finally:
        if collector and collector.poll() is None:collector.terminate()


if __name__ == "__main__":
    main()
