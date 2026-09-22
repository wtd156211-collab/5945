"""Core discrete-event engine.

Design rules that guarantee reproducibility:

* All time is virtual, stored as integer microseconds. Sampled durations
  are quantized with ``seconds_to_us`` (round-half-even on integer
  microseconds), so heap comparisons and log output never depend on
  floating-point corner cases.
* A single injectable ``random.Random`` instance produces every random
  number (inter-arrival gaps, service durations, rework decisions). The
  global ``random`` module state is never touched.
* Heap entries are ``(time_us, event_kind, seq, payload)``. ``event_kind``
  is a fixed same-timestamp priority (see the EV_* constants) and ``seq``
  is a monotonically increasing insertion counter, so ordering is total
  and deterministic; ``payload`` is never compared.
* A snapshot serializes the complete state (RNG state, heap, queues,
  counters, statistics) as JSON. Resuming from a snapshot processes
  exactly the events an uninterrupted run would, in the same order, with
  the same RNG stream.
"""

import heapq
import random
from collections import deque

US_PER_SECOND = 1_000_000

# Same-timestamp priority, lower value runs first. Documented in README.
EV_SERVICE_END = 0
EV_ARRIVAL = 1
EV_DOWNTIME_START = 2
EV_DOWNTIME_END = 3

EVENT_NAMES = {
    EV_SERVICE_END: "service_end",
    EV_ARRIVAL: "arrival",
    EV_DOWNTIME_START: "downtime_start",
    EV_DOWNTIME_END: "downtime_end",
}

SNAPSHOT_FORMAT = "gsb-sim-snapshot"
SNAPSHOT_VERSION = 1


def seconds_to_us(value):
    """Quantize a duration in seconds to integer microseconds."""
    return int(round(value * US_PER_SECOND))


class Station:
    __slots__ = ("index", "id", "servers", "service", "next", "rework",
                 "queue", "busy", "down")

    def __init__(self, index, cfg):
        self.index = index
        self.id = cfg["id"]
        self.servers = int(cfg["servers"])
        self.service = cfg["service"]
        self.next = cfg.get("next")
        self.rework = cfg.get("rework")
        self.queue = deque()  # (job_id, enqueue_time_us)
        self.busy = 0         # servers currently processing
        self.down = False     # inside a planned-downtime window


class StationStats:
    __slots__ = ("busy_us", "started", "completed", "backlog_peak", "waits")

    def __init__(self):
        self.busy_us = 0
        self.started = 0
        self.completed = 0
        self.backlog_peak = 0
        self.waits = []  # wait time in us, one sample per service start

    def to_dict(self):
        return {
            "busy_us": self.busy_us,
            "started": self.started,
            "completed": self.completed,
            "backlog_peak": self.backlog_peak,
            "waits": self.waits,
        }

    @classmethod
    def from_dict(cls, data):
        stats = cls()
        stats.busy_us = data["busy_us"]
        stats.started = data["started"]
        stats.completed = data["completed"]
        stats.backlog_peak = data["backlog_peak"]
        stats.waits = list(data["waits"])
        return stats


class Engine:
    def __init__(self, scenario, seed, logger, schedule_initial=True):
        self.scenario = scenario
        self.seed = seed
        self.logger = logger
        self.rng = random.Random(seed)
        self.stations = [Station(i, cfg) for i, cfg in enumerate(scenario["stations"])]
        self.station_by_id = {st.id: st for st in self.stations}

        self.time_us = 0
        self.event_seq = 0        # heap insertion counter (tie breaker)
        self.job_seq = 0          # job id source
        self.log_seq = 0          # event-log line counter
        self.events_processed = 0
        self.heap = []
        self.jobs = {}            # job_id -> system arrival time (us)

        self.station_stats = [StationStats() for _ in self.stations]
        self.arrived_jobs = 0
        self.completed_jobs = 0
        self.tis_total_us = 0     # time-in-system accumulators
        self.tis_max_us = 0

        arr = scenario["arrival"]
        self.arrival_rate_per_us = arr["rate_per_hour"] / 3600.0 / US_PER_SECOND
        self.arrival_start_us = seconds_to_us(arr["start_seconds"])
        self.arrival_stop_us = seconds_to_us(arr["stop_seconds"])
        self.horizon_us = seconds_to_us(scenario["horizon_seconds"])

        if schedule_initial:
            self._schedule_next_arrival(self.arrival_start_us)
            for dt in scenario.get("downtime", []):
                st = self.station_by_id[dt["station"]]
                start_us = seconds_to_us(dt["start_seconds"])
                duration_us = seconds_to_us(dt["duration_seconds"])
                self._push(start_us, EV_DOWNTIME_START, (st.index, duration_us))
                self._push(start_us + duration_us, EV_DOWNTIME_END, (st.index,))

    # ------------------------------------------------------------------
    # scheduling primitives
    # ------------------------------------------------------------------
    def _push(self, time_us, kind, payload):
        heapq.heappush(self.heap, (time_us, kind, self.event_seq, payload))
        self.event_seq += 1

    def _schedule_next_arrival(self, after_us):
        gap_us = int(round(self.rng.expovariate(self.arrival_rate_per_us)))
        at_us = after_us + gap_us
        if at_us <= self.arrival_stop_us:
            self._push(at_us, EV_ARRIVAL, ())

    def _sample_service_us(self, station):
        spec = station.service
        dist = spec["distribution"]
        if dist == "constant":
            return seconds_to_us(spec["seconds"])
        if dist == "uniform":
            return seconds_to_us(self.rng.uniform(spec["min_seconds"], spec["max_seconds"]))
        if dist == "exponential":
            return seconds_to_us(self.rng.expovariate(1.0 / spec["mean_seconds"]))
        raise ValueError("unknown distribution %r" % dist)

    def _log(self, event, station_id, job_id, detail=""):
        self.logger.log(self.log_seq, self.time_us, event, station_id, job_id, detail)
        self.log_seq += 1

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------
    def run(self, stop_at_us=None, snapshot_at_us=None, snapshot_sink=None):
        """Process events until the heap is empty or ``stop_at_us``/horizon.

        If ``snapshot_at_us`` is given, the state is captured as of that
        virtual time: every event with timestamp <= snapshot_at_us has been
        processed, none after. The snapshot dict is passed to
        ``snapshot_sink``.
        """
        end_us = self.horizon_us if stop_at_us is None else min(stop_at_us, self.horizon_us)
        pending_snapshot = snapshot_at_us
        while self.heap:
            next_us = self.heap[0][0]
            if next_us > end_us:
                break
            if (pending_snapshot is not None
                    and self.time_us <= pending_snapshot < next_us):
                self._emit_snapshot(pending_snapshot, snapshot_sink)
                pending_snapshot = None
            _, kind, _, payload = heapq.heappop(self.heap)
            self.time_us = next_us
            if kind == EV_SERVICE_END:
                self._on_service_end(payload)
            elif kind == EV_ARRIVAL:
                self._on_arrival()
            elif kind == EV_DOWNTIME_START:
                self._on_downtime_start(payload)
            else:
                self._on_downtime_end(payload)
            self.events_processed += 1
        if (pending_snapshot is not None
                and self.time_us <= pending_snapshot <= end_us):
            self._emit_snapshot(pending_snapshot, snapshot_sink)
        return end_us

    # ------------------------------------------------------------------
    # event handlers
    # ------------------------------------------------------------------
    def _on_arrival(self):
        self.job_seq += 1
        job_id = self.job_seq
        self.jobs[job_id] = self.time_us
        self.arrived_jobs += 1
        first = self.stations[0]
        self._log("arrival", first.id, job_id)
        self._enqueue(first, job_id)
        self._schedule_next_arrival(self.time_us)

    def _on_service_end(self, payload):
        station_idx, job_id, start_us = payload
        st = self.stations[station_idx]
        stats = self.station_stats[station_idx]
        st.busy -= 1
        stats.busy_us += self.time_us - start_us
        stats.completed += 1

        if st.rework is not None and self.rng.random() < st.rework["probability"]:
            target_id = st.rework["target"]
            decision = "to=rework:" + target_id
        elif st.next is not None:
            target_id = st.next
            decision = "to=" + target_id
        else:
            target_id = None
            decision = "to=exit"
        self._log("service_end", st.id, job_id, decision)

        if target_id is not None:
            self._enqueue(self.station_by_id[target_id], job_id)
        else:
            tis_us = self.time_us - self.jobs.pop(job_id)
            self.completed_jobs += 1
            self.tis_total_us += tis_us
            if tis_us > self.tis_max_us:
                self.tis_max_us = tis_us
            self._log("exit", "", job_id, "tis_us=%d" % tis_us)
        self._dispatch(st)

    def _on_downtime_start(self, payload):
        station_idx, duration_us = payload
        st = self.stations[station_idx]
        st.down = True
        self._log("downtime_start", st.id, "", "duration_us=%d" % duration_us)

    def _on_downtime_end(self, payload):
        (station_idx,) = payload
        st = self.stations[station_idx]
        st.down = False
        self._log("downtime_end", st.id, "")
        self._dispatch(st)

    # ------------------------------------------------------------------
    # queueing
    # ------------------------------------------------------------------
    def _enqueue(self, station, job_id):
        station.queue.append((job_id, self.time_us))
        stats = self.station_stats[station.index]
        if len(station.queue) > stats.backlog_peak:
            stats.backlog_peak = len(station.queue)
        self._dispatch(station)

    def _dispatch(self, station):
        stats = self.station_stats[station.index]
        while station.queue and not station.down and station.busy < station.servers:
            job_id, enqueued_us = station.queue.popleft()
            duration_us = self._sample_service_us(station)
            station.busy += 1
            wait_us = self.time_us - enqueued_us
            stats.waits.append(wait_us)
            stats.started += 1
            self._log("service_start", station.id, job_id,
                      "wait_us=%d;dur_us=%d" % (wait_us, duration_us))
            self._push(self.time_us + duration_us, EV_SERVICE_END,
                       (station.index, job_id, self.time_us))

    # ------------------------------------------------------------------
    # snapshot / restore
    # ------------------------------------------------------------------
    def _emit_snapshot(self, at_us, sink):
        if sink is not None:
            sink(self.snapshot_dict(at_us))

    def snapshot_dict(self, at_us):
        rng_version, rng_internal, rng_gauss = self.rng.getstate()
        return {
            "format": SNAPSHOT_FORMAT,
            "version": SNAPSHOT_VERSION,
            "scenario": self.scenario,
            "seed": self.seed,
            "sim_time_us": at_us,
            "counters": {
                "event_seq": self.event_seq,
                "job_seq": self.job_seq,
                "log_seq": self.log_seq,
                "events_processed": self.events_processed,
            },
            "rng_state": [rng_version, list(rng_internal), rng_gauss],
            "heap": [[t, k, s, list(p)] for (t, k, s, p) in self.heap],
            "stations": [
                {
                    "id": st.id,
                    "queue": [[job_id, t] for job_id, t in st.queue],
                    "busy": st.busy,
                    "down": st.down,
                }
                for st in self.stations
            ],
            "jobs": {str(job_id): t for job_id, t in self.jobs.items()},
            "stats": {
                "stations": [s.to_dict() for s in self.station_stats],
                "arrived_jobs": self.arrived_jobs,
                "completed_jobs": self.completed_jobs,
                "tis_total_us": self.tis_total_us,
                "tis_max_us": self.tis_max_us,
            },
        }

    @classmethod
    def from_snapshot(cls, data, logger):
        if data.get("format") != SNAPSHOT_FORMAT:
            raise ValueError("not a snapshot file: %r" % data.get("format"))
        if data.get("version") != SNAPSHOT_VERSION:
            raise ValueError("unsupported snapshot version %r" % data.get("version"))

        eng = cls(data["scenario"], data["seed"], logger, schedule_initial=False)
        eng.time_us = data["sim_time_us"]
        counters = data["counters"]
        eng.event_seq = counters["event_seq"]
        eng.job_seq = counters["job_seq"]
        eng.log_seq = counters["log_seq"]
        eng.events_processed = counters["events_processed"]

        rng_version, rng_internal, rng_gauss = data["rng_state"]
        eng.rng.setstate((rng_version, tuple(rng_internal), rng_gauss))

        eng.heap = [(t, k, s, tuple(p)) for t, k, s, p in data["heap"]]
        heapq.heapify(eng.heap)

        for st, saved in zip(eng.stations, data["stations"]):
            if st.id != saved["id"]:
                raise ValueError("snapshot station order mismatch: %s vs %s"
                                 % (st.id, saved["id"]))
            st.queue = deque((job_id, t) for job_id, t in saved["queue"])
            st.busy = saved["busy"]
            st.down = saved["down"]

        eng.jobs = {int(job_id): t for job_id, t in data["jobs"].items()}

        stats = data["stats"]
        eng.station_stats = [StationStats.from_dict(s) for s in stats["stations"]]
        eng.arrived_jobs = stats["arrived_jobs"]
        eng.completed_jobs = stats["completed_jobs"]
        eng.tis_total_us = stats["tis_total_us"]
        eng.tis_max_us = stats["tis_max_us"]
        return eng
