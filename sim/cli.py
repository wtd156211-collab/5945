"""Command line interface: ``python -m sim run|resume ...``."""
import argparse
import json
import sys
import time

from .engine import Engine, load_snapshot, save_snapshot
from .log import LogWriter
from .report import build_report
from .scenario import load_scenario


def _open_log(path, mode):
    if path is None:
        return None, None
    fh = open(path, mode, encoding="utf-8")
    return LogWriter(fh), fh


def _write_report(report, path):
    text = json.dumps(report, indent=2) + "\n"
    if path is None:
        sys.stdout.write(text)
    else:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)


def _print_timing(out, events, wall):
    out.write("events=%d wall_time=%.3fs\n" % (events, wall))


def cmd_run(scenario_path, report_path=None, log_path=None, seed=None,
            snapshot_at=None, snapshot_out=None, out=None):
    out = out if out is not None else sys.stdout
    scenario = load_scenario(scenario_path)
    log, log_fh = _open_log(log_path, "w")
    try:
        engine = Engine(scenario, seed=seed, log=log)
        started = time.perf_counter()
        engine.run(stop_at=snapshot_at)
        wall = time.perf_counter() - started
    finally:
        if log_fh is not None:
            log_fh.close()
    if snapshot_out is not None:
        save_snapshot(engine, snapshot_out)
        out.write("snapshot=%s clock=%.6g events=%d\n"
                  % (snapshot_out, engine.clock, engine.events_processed))
        _print_timing(out, engine.events_processed, wall)
        return 0
    report = build_report(engine)
    _write_report(report, report_path)
    _print_timing(out, engine.events_processed, wall)
    return 0


def cmd_resume(snapshot_path, report_path=None, log_path=None, out=None):
    out = out if out is not None else sys.stdout
    data = load_snapshot(snapshot_path)
    log, log_fh = _open_log(log_path, "a")
    try:
        engine = Engine.from_snapshot(data, log=log)
        started = time.perf_counter()
        engine.run()
        wall = time.perf_counter() - started
    finally:
        if log_fh is not None:
            log_fh.close()
    report = build_report(engine)
    _write_report(report, report_path)
    _print_timing(out, engine.events_processed, wall)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="sim", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a scenario (optionally stop at a snapshot point)")
    run.add_argument("scenario")
    run.add_argument("--seed", type=int, default=None, help="override the scenario seed")
    run.add_argument("--report", default=None, help="report output path (default: stdout)")
    run.add_argument("--log", default=None, help="event log output path (JSON Lines)")
    run.add_argument("--snapshot-at", type=float, default=None,
                     help="stop after processing all events at or before this virtual time")
    run.add_argument("--snapshot-out", default=None,
                     help="write a snapshot when stopping (requires --snapshot-at)")

    resume = sub.add_parser("resume", help="resume from a snapshot file")
    resume.add_argument("snapshot")
    resume.add_argument("--report", default=None, help="report output path (default: stdout)")
    resume.add_argument("--log", default=None,
                        help="event log path; appended to, so point it at the part-1 log")

    args = parser.parse_args(argv)
    if args.command == "run":
        if args.snapshot_out is not None and args.snapshot_at is None:
            parser.error("--snapshot-out requires --snapshot-at")
        return cmd_run(args.scenario, report_path=args.report, log_path=args.log,
                       seed=args.seed, snapshot_at=args.snapshot_at,
                       snapshot_out=args.snapshot_out)
    return cmd_resume(args.snapshot, report_path=args.report, log_path=args.log)


if __name__ == "__main__":
    sys.exit(main())
