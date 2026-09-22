import io
import json
import os
import random
import tempfile
import unittest

from sim import Engine, EventLogger, build_report, load_scenario, seconds_to_us
from sim.__main__ import main as cli_main
from sim.report import quantile

SAMPLES = os.path.join(os.path.dirname(__file__), os.pardir, "samples")
HUB = os.path.join(SAMPLES, "hub.json")
LINE = os.path.join(SAMPLES, "line.json")


def run_engine(scenario, seed=None, stop_at_us=None, snapshot_at_us=None,
               snapshots=None, logger=None, log=None):
    """Run one engine segment in memory; returns (engine, log_text, report)."""
    if log is None:
        log = io.StringIO()
        logger = EventLogger(log)
        logger.write_header()
    elif logger is None:
        logger = EventLogger(log)
    engine = Engine(scenario, seed if seed is not None else scenario["seed"], logger)
    end_us = engine.run(stop_at_us=stop_at_us, snapshot_at_us=snapshot_at_us,
                        snapshot_sink=snapshots.append if snapshots is not None else None)
    return engine, log.getvalue(), build_report(engine, end_us)


def resume_engine(snapshot, log):
    logger = EventLogger(log)
    return Engine.from_snapshot(snapshot, logger)


def run_segmented(scenario, split_points_us):
    """Run a scenario in len(split_points_us)+1 segments via snapshots.

    Returns (log_text, report) exactly as a CLI user would observe them.
    """
    log = io.StringIO()
    logger = EventLogger(log)
    logger.write_header()
    engine = Engine(scenario, scenario["seed"], logger)
    end_us = None
    for split_us in split_points_us:
        snaps = []
        engine.run(stop_at_us=split_us, snapshot_at_us=split_us,
                   snapshot_sink=snaps.append)
        self_check = json.loads(json.dumps(snaps[0]))  # must round-trip as JSON
        engine = Engine.from_snapshot(self_check, EventLogger(log))
    end_us = engine.run()
    return log.getvalue(), build_report(engine, end_us)


class DeterminismTests(unittest.TestCase):
    def test_same_seed_same_bytes(self):
        scenario = load_scenario(HUB)
        _, log_a, report_a = run_engine(scenario)
        _, log_b, report_b = run_engine(scenario)
        self.assertEqual(log_a, log_b)
        self.assertEqual(json.dumps(report_a, sort_keys=True),
                         json.dumps(report_b, sort_keys=True))

    def test_different_seed_differs(self):
        scenario = load_scenario(HUB)
        _, log_a, _ = run_engine(scenario, seed=1)
        _, log_b, _ = run_engine(scenario, seed=2)
        self.assertNotEqual(log_a, log_b)

    def test_global_random_module_is_not_used(self):
        scenario = load_scenario(HUB)
        random.seed(111)
        _, log_a, report_a = run_engine(scenario)
        random.seed(999)
        for _ in range(100):
            random.random()
        _, log_b, report_b = run_engine(scenario)
        self.assertEqual(log_a, log_b)
        self.assertEqual(report_a, report_b)


class EventLogFormatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scenario = load_scenario(HUB)
        _, cls.log_text, _ = run_engine(scenario)
        cls.lines = cls.log_text.splitlines()

    def test_header(self):
        self.assertEqual(self.lines[0], EventLogger.HEADER)

    def test_row_shape_and_known_events(self):
        known = {"arrival", "service_start", "service_end", "exit",
                 "downtime_start", "downtime_end"}
        for line in self.lines[1:]:
            fields = line.split(",")
            self.assertEqual(len(fields), 6, line)
            self.assertIn(fields[2], known, line)
            int(fields[0]); int(fields[1])  # seq and time must be integers

    def test_seq_is_dense_and_ordered(self):
        seqs = [int(line.split(",")[0]) for line in self.lines[1:]]
        self.assertEqual(seqs, list(range(len(seqs))))

    def test_timestamps_non_decreasing(self):
        times = [int(line.split(",")[1]) for line in self.lines[1:]]
        self.assertEqual(times, sorted(times))

    def test_no_floats_in_log(self):
        self.assertNotIn(".", self.log_text)


class SnapshotFormatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scenario = load_scenario(HUB)
        cls.snaps = []
        run_engine(cls.scenario, stop_at_us=seconds_to_us(3600),
                   snapshot_at_us=seconds_to_us(3600), snapshots=cls.snaps)
        cls.snap = cls.snaps[0]

    def test_exactly_one_snapshot(self):
        self.assertEqual(len(self.snaps), 1)

    def test_top_level_structure(self):
        snap = self.snap
        self.assertEqual(snap["format"], "gsb-sim-snapshot")
        self.assertEqual(snap["version"], 1)
        for key in ("scenario", "seed", "sim_time_us", "counters", "rng_state",
                    "heap", "stations", "jobs", "stats"):
            self.assertIn(key, snap)
        self.assertEqual(snap["sim_time_us"], seconds_to_us(3600))

    def test_rng_state_shape(self):
        version, internal, gauss = self.snap["rng_state"]
        self.assertEqual(version, 3)
        self.assertEqual(len(internal), 625)
        self.assertTrue(all(isinstance(v, int) for v in internal))
        self.assertIsNone(gauss)

    def test_heap_entries(self):
        t0 = self.snap["sim_time_us"]
        for entry in self.snap["heap"]:
            self.assertEqual(len(entry), 4)
            time_us, kind, seq, payload = entry
            self.assertGreater(time_us, t0, "snapshot must only contain future events")
            self.assertIn(kind, (0, 1, 2, 3))
            self.assertIsInstance(payload, list)

    def test_station_state_shape(self):
        stations = self.snap["stations"]
        self.assertEqual([s["id"] for s in stations],
                         [s["id"] for s in self.scenario["stations"]])
        for st in stations:
            self.assertIsInstance(st["busy"], int)
            self.assertIsInstance(st["down"], bool)
            for job_id, enqueued in st["queue"]:
                self.assertIsInstance(job_id, int)
                self.assertLessEqual(enqueued, self.snap["sim_time_us"])

    def test_json_round_trip_restores_equal_snapshot(self):
        restored = json.loads(json.dumps(self.snap))
        self.assertEqual(restored, self.snap)


class ResumeConsistencyTests(unittest.TestCase):
    def check_split(self, scenario_path, split_seconds):
        scenario = load_scenario(scenario_path)
        _, full_log, full_report = run_engine(scenario)
        splits = [seconds_to_us(s) for s in split_seconds]
        split_log, split_report = run_segmented(scenario, splits)
        self.assertEqual(split_log, full_log,
                         "event log diverges after resume (%s)" % scenario_path)
        self.assertEqual(json.dumps(split_report, indent=2),
                         json.dumps(full_report, indent=2),
                         "report diverges after resume (%s)" % scenario_path)

    def test_hub_single_split(self):
        self.check_split(HUB, [3600])

    def test_hub_chained_splits(self):
        self.check_split(HUB, [2000, 5000, 12000, 20000])

    def test_hub_split_at_arrival_stop_boundary(self):
        self.check_split(HUB, [25200])

    def test_line_split_at_downtime_start(self):
        # 28800s is exactly the weld downtime start: stresses same-timestamp
        # ordering across the snapshot boundary.
        self.check_split(LINE, [28800])

    def test_line_split_inside_downtime(self):
        self.check_split(LINE, [29000, 60000])

    def test_snapshot_bytes_are_deterministic(self):
        scenario = load_scenario(HUB)
        snaps_a, snaps_b = [], []
        run_engine(scenario, stop_at_us=seconds_to_us(3600),
                   snapshot_at_us=seconds_to_us(3600), snapshots=snaps_a)
        run_engine(scenario, stop_at_us=seconds_to_us(3600),
                   snapshot_at_us=seconds_to_us(3600), snapshots=snaps_b)
        self.assertEqual(json.dumps(snaps_a[0]), json.dumps(snaps_b[0]))


class StatsSanityTests(unittest.TestCase):
    def test_line_report_invariants(self):
        scenario = load_scenario(LINE)
        _, _, report = run_engine(scenario)
        self.assertGreater(report["jobs"]["arrived"], 40000)
        self.assertEqual(report["jobs"]["in_system_at_end"],
                         report["jobs"]["arrived"] - report["jobs"]["completed"])
        for st in report["stations"]:
            self.assertGreaterEqual(st["utilization"], 0.0)
            self.assertLessEqual(st["utilization"], 1.0)
            self.assertEqual(st["wait_seconds"]["count"], st["jobs_started"])
            self.assertGreaterEqual(st["backlog_peak"], 0)
            if st["wait_seconds"]["count"]:
                ws = st["wait_seconds"]
                self.assertLessEqual(ws["p50"], ws["p90"])
                self.assertLessEqual(ws["p90"], ws["p99"])
                self.assertLessEqual(ws["p99"], ws["max"])

    def test_quantile(self):
        self.assertIsNone(quantile([], 0.5))
        self.assertEqual(quantile([5], 0.9), 5)
        self.assertEqual(quantile([0, 10], 0.5), 5)
        self.assertEqual(quantile([0, 10], 0.0), 0)
        self.assertEqual(quantile([0, 10], 1.0), 10)
        self.assertAlmostEqual(quantile([0, 10, 20], 0.25), 5)


class CliTests(unittest.TestCase):
    def test_cli_split_run_matches_single_run_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            full = os.path.join(tmp, "full")
            part = os.path.join(tmp, "part")
            cli_main(["run", HUB, "-o", full])
            cli_main(["run", HUB, "-o", part,
                      "--stop-at", "3600", "--snapshot-at", "3600"])
            cli_main(["run", "--resume", os.path.join(part, "snapshot.json"),
                      "-o", part])
            for name in ("events.csv", "report.json"):
                with open(os.path.join(full, name), "rb") as fp:
                    expected = fp.read()
                with open(os.path.join(part, name), "rb") as fp:
                    actual = fp.read()
                self.assertEqual(actual, expected, name)

    def test_cli_seed_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli_main(["run", HUB, "-o", tmp, "--seed", "7"])
            with open(os.path.join(tmp, "report.json")) as fp:
                self.assertEqual(json.load(fp)["seed"], 7)


if __name__ == "__main__":
    unittest.main()
