import io
import json
import os
import random
import shutil
import tempfile
import unittest

from sim.cli import cmd_resume, cmd_run
from sim.engine import (
    EVENT_PRIORITY,
    SNAPSHOT_FORMAT,
    SNAPSHOT_VERSION,
    Engine,
    load_snapshot,
    save_snapshot,
)
from sim.log import LogWriter
from sim.report import build_report, quantile
from sim.scenario import load_scenario, validate_scenario

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(REPO_ROOT, "samples", "hub.json")
LINE = os.path.join(REPO_ROOT, "samples", "line.json")

KNOWN_EVENTS = {
    "arrival", "enqueue", "service_start", "service_end",
    "rework", "depart", "downtime_start", "downtime_end",
}


def tiny_scenario(**overrides):
    scenario = {
        "name": "tiny",
        "seed": 11,
        "horizon_seconds": 200,
        "arrival": {"process": "poisson", "rate_per_hour": 3600,
                    "start_seconds": 0, "stop_seconds": 20},
        "stations": [
            {"id": "a", "servers": 1,
             "service": {"distribution": "constant", "seconds": 5}, "next": None},
        ],
        "downtime": [],
    }
    scenario.update(overrides)
    return scenario


class TempDirMixin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="simtest-")
        self.addCleanup(shutil.rmtree, self.tmp)

    def path(self, name):
        return os.path.join(self.tmp, name)

    def run_to_end(self, scenario_path, tag, seed=None):
        report, log = self.path(tag + ".json"), self.path(tag + ".log")
        cmd_run(scenario_path, report_path=report, log_path=log, seed=seed,
                out=io.StringIO())
        return report, log

    def run_split(self, scenario_path, snapshot_at, tag):
        snap = self.path(tag + ".snap.json")
        report, log = self.path(tag + ".json"), self.path(tag + ".log")
        cmd_run(scenario_path, log_path=log, snapshot_at=snapshot_at,
                snapshot_out=snap, out=io.StringIO())
        cmd_resume(snap, report_path=report, log_path=log, out=io.StringIO())
        return report, log


class TestDeterminism(TempDirMixin):
    def test_same_seed_byte_identical(self):
        report_a, log_a = self.run_to_end(HUB, "a")
        report_b, log_b = self.run_to_end(HUB, "b")
        with open(report_a, "rb") as fh:
            bytes_a = fh.read()
        with open(report_b, "rb") as fh:
            self.assertEqual(bytes_a, fh.read())
        with open(log_a, "rb") as fh:
            bytes_a = fh.read()
        with open(log_b, "rb") as fh:
            self.assertEqual(bytes_a, fh.read())

    def test_global_random_state_does_not_leak(self):
        random.seed(12345)
        report_a, log_a = self.run_to_end(HUB, "ga")
        random.seed(99999)
        for _ in range(100):
            random.random()
        report_b, log_b = self.run_to_end(HUB, "gb")
        with open(report_a, "rb") as fa, open(report_b, "rb") as fb:
            self.assertEqual(fa.read(), fb.read())
        with open(log_a, "rb") as fa, open(log_b, "rb") as fb:
            self.assertEqual(fa.read(), fb.read())

    def test_seed_override_changes_and_repeats(self):
        _, log_a = self.run_to_end(HUB, "sa", seed=1)
        _, log_b = self.run_to_end(HUB, "sb", seed=2)
        _, log_c = self.run_to_end(HUB, "sc", seed=1)
        with open(log_a, "rb") as fa, open(log_b, "rb") as fb, open(log_c, "rb") as fc:
            bytes_a, bytes_b, bytes_c = fa.read(), fb.read(), fc.read()
        self.assertNotEqual(bytes_a, bytes_b)
        self.assertEqual(bytes_a, bytes_c)

    def test_injected_rng_is_used(self):
        scenario = load_scenario(HUB)
        engine_a = Engine(scenario, rng=random.Random(42))
        engine_b = Engine(scenario, rng=random.Random(42))
        engine_a.run()
        engine_b.run()
        self.assertEqual(build_report(engine_a), build_report(engine_b))


class TestResumeConsistency(TempDirMixin):
    def assert_split_equals_full(self, scenario_path, snapshot_at):
        full_report, full_log = self.run_to_end(scenario_path, "full")
        part_report, part_log = self.run_split(scenario_path, snapshot_at, "part")
        with open(full_report, "rb") as fa, open(part_report, "rb") as fb:
            self.assertEqual(fa.read(), fb.read(), "reports differ after resume")
        with open(full_log, "rb") as fa, open(part_log, "rb") as fb:
            self.assertEqual(fa.read(), fb.read(), "event logs differ after resume")

    def test_hub_resume_matches_full_run(self):
        self.assert_split_equals_full(HUB, 10000.0)

    def test_hub_resume_at_busy_instant(self):
        self.assert_split_equals_full(HUB, 14400.0)

    def test_line_resume_matches_full_run(self):
        self.assert_split_equals_full(LINE, 30000.0)

    def test_line_resume_inside_downtime(self):
        # 28800 < 30000 < 30600: weld is down at the snapshot point.
        self.assert_split_equals_full(LINE, 30000.0)


class TestSnapshotFormat(TempDirMixin):
    def make_snapshot(self):
        scenario = load_scenario(HUB)
        engine = Engine(scenario)
        engine.run(stop_at=5000.0)
        path = self.path("snap.json")
        save_snapshot(engine, path)
        return engine, path

    def test_snapshot_schema(self):
        _, path = self.make_snapshot()
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["format"], SNAPSHOT_FORMAT)
        self.assertEqual(data["version"], SNAPSHOT_VERSION)
        for key in ("scenario", "seed", "clock", "seq", "log_seq", "next_job_id",
                    "events_processed", "rng_state", "event_heap", "stations",
                    "jobs", "stats"):
            self.assertIn(key, data, "snapshot missing key %r" % key)
        for entry in data["event_heap"]:
            self.assertEqual(len(entry), 4)
            self.assertIsInstance(entry[0], float)
            self.assertIn(entry[3][0], EVENT_PRIORITY)
        for sid, st in data["stations"].items():
            for key in ("busy", "down", "down_since", "queue"):
                self.assertIn(key, st)

    def test_snapshot_roundtrip_is_lossless(self):
        engine, path = self.make_snapshot()
        with open(path, "rb") as fh:
            original_bytes = fh.read()
        restored = Engine.from_snapshot(load_snapshot(path))
        again = self.path("snap2.json")
        save_snapshot(restored, again)
        with open(again, "rb") as fh:
            self.assertEqual(original_bytes, fh.read())

    def test_snapshot_captures_rng_stream(self):
        engine, path = self.make_snapshot()
        restored = Engine.from_snapshot(load_snapshot(path))
        self.assertEqual([engine.rng.random() for _ in range(5)],
                         [restored.rng.random() for _ in range(5)])

    def test_resume_rejects_foreign_seed(self):
        # Seed is part of the snapshot; resuming cannot override it.
        _, path = self.make_snapshot()
        data = load_snapshot(path)
        engine = Engine.from_snapshot(data)
        scenario_seed = load_scenario(HUB)["seed"]
        self.assertEqual(engine.seed, scenario_seed)


class TestEventLogFormat(TempDirMixin):
    def test_log_lines_schema_and_order(self):
        _, log_path = self.run_to_end(HUB, "fmt")
        last_time = 0.0
        count = 0
        with open(log_path, "r", encoding="utf-8") as fh:
            for expected_seq, line in enumerate(fh):
                record = json.loads(line)
                self.assertEqual(record["seq"], expected_seq)
                self.assertIn("time", record)
                self.assertIn(record["event"], KNOWN_EVENTS)
                self.assertGreaterEqual(record["time"], last_time)
                last_time = record["time"]
                count += 1
        self.assertGreater(count, 0)

    def test_tie_break_priority_in_heap(self):
        engine = Engine(tiny_scenario())
        engine.heap = []
        engine.seq = 0
        # Push in scrambled order; pop order must follow EVENT_PRIORITY then FIFO seq.
        engine._push(50.0, "arrival")
        engine._push(50.0, "downtime_end", ("a",))
        engine._push(50.0, "service_end", ("a", 1))
        engine._push(50.0, "downtime_start", ("a",))
        engine._push(50.0, "service_end", ("a", 2))
        order = [engine.heap and __import__("heapq").heappop(engine.heap)[3][0]
                 for _ in range(5)]
        self.assertEqual(order, ["downtime_start", "service_end", "service_end",
                                 "downtime_end", "arrival"])


class TestSemantics(TempDirMixin):
    def write_scenario(self, scenario, name="scenario.json"):
        path = self.path(name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(scenario, fh)
        return path

    def read_log(self, path):
        with open(path, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh]

    def test_downtime_blocks_starts_but_not_running_jobs(self):
        scenario = tiny_scenario(
            horizon_seconds=400,
            stations=[{"id": "a", "servers": 1,
                       "service": {"distribution": "constant", "seconds": 60},
                       "next": None}],
            downtime=[{"station": "a", "start_seconds": 30, "duration_seconds": 100}],
        )
        _, log_path = self.run_to_end(self.write_scenario(scenario), "dt")
        records = self.read_log(log_path)
        starts = [r["time"] for r in records if r["event"] == "service_start"]
        ends = [r["time"] for r in records if r["event"] == "service_end"]
        # No service may start inside the downtime window [30, 130).
        self.assertTrue(all(t < 30 or t >= 130 for t in starts), starts)
        # The job running when downtime began finishes on schedule, mid-downtime.
        self.assertTrue(any(30 < t < 130 for t in ends), ends)
        # Service resumes exactly at the downtime end.
        self.assertIn(130.0, starts)

    def test_rework_routes_back_to_target(self):
        scenario = tiny_scenario(
            horizon_seconds=100000,
            arrival={"process": "poisson", "rate_per_hour": 360,
                     "start_seconds": 0, "stop_seconds": 3600},
            stations=[
                {"id": "a", "servers": 1,
                 "service": {"distribution": "constant", "seconds": 5},
                 "next": None, "rework": {"probability": 1.0, "target": "a"}},
            ],
        )
        _, log_path = self.run_to_end(self.write_scenario(scenario), "rw")
        records = self.read_log(log_path)
        self.assertTrue(any(r["event"] == "rework" and r["station"] == "a"
                            for r in records))
        # probability 1.0: nothing ever departs.
        self.assertFalse(any(r["event"] == "depart" for r in records))

    def test_all_arrivals_eventually_depart_when_uncongested(self):
        scenario = tiny_scenario()
        report_path, _ = self.run_to_end(self.write_scenario(scenario), "tiny")
        with open(report_path, "r", encoding="utf-8") as fh:
            report = json.load(fh)
        jobs = report["jobs"]
        self.assertGreater(jobs["arrived"], 0)
        self.assertEqual(jobs["arrived"], jobs["departed"] + jobs["in_system"])
        self.assertEqual(jobs["in_system"], 0)
        util = report["stations"]["a"]["utilization"]
        self.assertAlmostEqual(util, 5.0 * jobs["departed"] / 200.0)


class TestReportFormat(TempDirMixin):
    def test_report_schema_and_values(self):
        report_path, _ = self.run_to_end(LINE, "rep")
        with open(report_path, "r", encoding="utf-8") as fh:
            report = json.load(fh)
        self.assertEqual(report["format"], "sim-report")
        self.assertEqual(report["version"], 1)
        self.assertEqual(report["seed"], load_scenario(LINE)["seed"])
        self.assertGreater(report["events_processed"], 0)
        station_ids = [s["id"] for s in load_scenario(LINE)["stations"]]
        self.assertEqual(list(report["stations"].keys()), station_ids)
        for sid in station_ids:
            st = report["stations"][sid]
            self.assertGreaterEqual(st["utilization"], 0.0)
            self.assertLessEqual(st["utilization"], 1.0)
            self.assertIsInstance(st["queue_peak"], int)
            waits = st["wait_seconds"]
            if waits["count"]:
                ordered = [waits["p50"], waits["p90"], waits["p95"],
                           waits["p99"], waits["max"]]
                self.assertEqual(ordered, sorted(ordered))
                self.assertGreaterEqual(waits["p50"], 0.0)
        jobs = report["jobs"]
        self.assertEqual(jobs["arrived"], jobs["departed"] + jobs["in_system"])

    def test_quantile_interpolation(self):
        self.assertEqual(quantile([], 0.5), None)
        self.assertEqual(quantile([3.0], 0.99), 3.0)
        self.assertEqual(quantile([0.0, 10.0], 0.5), 5.0)
        self.assertEqual(quantile([0.0, 10.0], 0.0), 0.0)
        self.assertEqual(quantile([0.0, 10.0], 1.0), 10.0)
        self.assertAlmostEqual(quantile([1.0, 2.0, 3.0, 4.0], 0.9), 3.7)


class TestScenarioValidation(unittest.TestCase):
    def test_unknown_next_station_rejected(self):
        with self.assertRaises(ValueError):
            validate_scenario(tiny_scenario(
                stations=[{"id": "a", "servers": 1,
                           "service": {"distribution": "constant", "seconds": 5},
                           "next": "ghost"}]))

    def test_unknown_downtime_station_rejected(self):
        with self.assertRaises(ValueError):
            validate_scenario(tiny_scenario(
                downtime=[{"station": "ghost", "start_seconds": 0,
                           "duration_seconds": 10}]))

    def test_samples_are_valid(self):
        validate_scenario(load_scenario(HUB))
        validate_scenario(load_scenario(LINE))


if __name__ == "__main__":
    unittest.main()
