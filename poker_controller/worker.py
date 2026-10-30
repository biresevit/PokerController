"""Local PokerController worker for commands planned by the Azure Function."""
import argparse
import asyncio
import logging
import os
from pathlib import Path
import sys
from types import SimpleNamespace


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
    from poker_controller.system_metrics import sample as machine_metrics,POKER_CONTROLLER_VERSION
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    if (output / "stop").exists():
        parser.error("Choose a fresh output directory; this one has a stop marker")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(output / "worker.log", encoding="utf-8"), logging.StreamHandler()])
    for name in ("httpx", "httpcore", "websockets"):
        logging.getLogger(name).setLevel(logging.WARNING)
    settings = SimpleNamespace(controller_version=POKER_CONTROLLER_VERSION,site_id=args.site_id,machine_id=args.machine_id,seconds=0, seed=args.seed, output=str(output), target=0,
        leave_on_exit=args.leave_on_exit, telemetry_provider=machine_metrics)
    try:
        asyncio.run(Pilot(settings).run_cloud_worker())
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("PokerController worker stopped by operator")


if __name__ == "__main__":
    main()
