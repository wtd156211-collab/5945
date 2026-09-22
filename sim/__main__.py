"""Command line interface.

Examples:
    python -m sim run samples/hub.json -o out/hub
    python -m sim run samples/line.json -o out/line --stop-at 43200 --snapshot-at 43200
    python -m sim run --resume out/line/snapshot.json -o out/line
"""

import argparse
import json
import os
import sys
import time

from .engine import Engine, seconds_to_us
from .eventlog import EventLogger
from .model import load_scenario
from .report import build_report


def _build_parser():
    parser = argparse.ArgumentParser(prog="sim", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run a scenario (optionally resuming from a snapshot)")
    run.add_argument("scenario", nargs="?", help="path to the scenario JSON file")
    run.add_argument("--resume", metavar="SNAPSHOT",
                     help="resume from a snapshot JSON file instead of a fresh scenario")
    run.add_argument("-o", "--out-dir", default="out", help="output directory (default: out)")
    run.add_argument("--seed", type=int, default=None,
                     help="override the scenario seed (fresh runs only)")
    run.add_argument("--snapshot-at", type=float, default=None, metavar="SECONDS",
                     help="write a snapshot when virtual time passes this point")
    run.add_argument("--stop-at", type=float, default=None, metavar="SECONDS",
                     help="stop the run at this virtual time (default: scenario horizon)")
    return parser


def _cmd_run(args):
    if args.resume:
        if args.seed is not None:
            raise SystemExit("--seed cannot be combined with --resume; "
                             "the RNG state comes from the snapshot")
        with open(args.resume, "r", encoding="utf-8") as fp:
            snapshot = json.load(fp)
    elif args.scenario:
        scenario = load_scenario(args.scenario)
    else:
        raise SystemExit("run requires a scenario file or --resume SNAPSHOT")

    os.makedirs(args.out_dir, exist_ok=True)
    log_path = os.path.join(args.out_dir, "events.csv")
    report_path = os.path.join(args.out_dir, "report.json")
    snapshot_path = os.path.join(args.out_dir, "snapshot.json")

    if args.resume:
        fresh_log = not os.path.exists(log_path) or os.path.getsize(log_path) == 0
        log_fp = open(log_path, "a", encoding="utf-8", newline="")
        logger = EventLogger(log_fp)
        if fresh_log:
            logger.write_header()
        engine = Engine.from_snapshot(snapshot, logger)
    else:
        seed = args.seed if args.seed is not None else scenario["seed"]
        log_fp = open(log_path, "w", encoding="utf-8", newline="")
        logger = EventLogger(log_fp)
        logger.write_header()
        engine = Engine(scenario, seed, logger)

    snapshot_sink = None
    if args.snapshot_at is not None:
        def snapshot_sink(snap):
            with open(snapshot_path, "w", encoding="utf-8") as fp:
                json.dump(snap, fp)
                fp.write("\n")

    stop_at_us = seconds_to_us(args.stop_at) if args.stop_at is not None else None
    snapshot_at_us = (seconds_to_us(args.snapshot_at)
                      if args.snapshot_at is not None else None)

    started = time.perf_counter()
    end_us = engine.run(stop_at_us=stop_at_us, snapshot_at_us=snapshot_at_us,
                        snapshot_sink=snapshot_sink)
    wall = time.perf_counter() - started
    log_fp.close()

    report = build_report(engine, end_us)
    with open(report_path, "w", encoding="utf-8") as fp:
        json.dump(report, fp, indent=2)
        fp.write("\n")

    rate = engine.events_processed / wall if wall > 0 else float("inf")
    print("scenario=%s events=%d wall=%.3fs rate=%.0f events/s -> %s"
          % (report["scenario"], engine.events_processed, wall, rate, args.out_dir))
    return 0


def main(argv=None):
    args = _build_parser().parse_args(argv)
    if args.command == "run":
        return _cmd_run(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
