"""Discrete-event simulation engine.

Determinism rules (see README for the full contract):

* Virtual time only. Events are popped from a heap ordered by
  ``(time, event_type_priority, sequence_number)``.
* Ties on time are broken first by event type priority
  (downtime_start < service_end < downtime_end < arrival), then by FIFO
  sequence number. Never by dict order or payload content.
* All randomness flows through a single injectable ``random.Random``
  instance. Nothing touches the global random module state.
"""
import heapq
import json
import random
from collections import deque

EVENT_PRIORITY = {
    "downtime_start": 0,
    "service_end": 1,
    "downtime_end": 2,
    "arrival": 3,
}

SNAPSHOT_FORMAT = "sim-snapshot"
SNAPSHOT_VERSION = 1


class StationState:
    __slots__ = ("spec", "busy", "down", "down_since", "queue")

    def __init__(self, spec):
        self.spec = spec
        self.busy = 0
        self.down = False
        self.down_since = None
        self.queue = deque()


class StationStats:
    __slots__ = ("enqueued", "started", "completed", "busy_time",
                 "downtime", "queue_peak", "waits")

    def __init__(self):
        self.enqueued = 0
        self.started = 0
        self.completed = 0
        self.busy_time = 0.0
        self.downtime = 0.0
        self.queue_peak = 0
        self.waits = []

    def to_dict(self):
        return {
            "enqueued": self.enqueued,
            "started": self.started,
            "completed": self.completed,
            "busy_time": self.busy_time,
            "downtime": self.downtime,
            "queue_peak": self.queue_peak,
            "waits": list(self.waits),
        }

    @classmethod
    def from_dict(cls, data):
        obj = cls()
        obj.enqueued = data["enqueued"]
        obj.started = data["started"]
        obj.completed = data["completed"]
        obj.busy_time = data["busy_time"]
        obj.downtime = data["downtime"]
        obj.queue_peak = data["queue_peak"]
        obj.waits = list(data["waits"])
        return obj


class Stats:
    def __init__(self, station_ids):
        self.jobs_arrived = 0
        self.jobs_departed = 0
        self.stations = {sid: StationStats() for sid in station_ids}

    def to_dict(self):
        return {
            "jobs_arrived": self.jobs_arrived,
            "jobs_departed": self.jobs_departed,
            "stations": {sid: s.to_dict() for sid, s in self.stations.items()},
        }

    @classmethod
    def from_dict(cls, data):
        obj = cls.__new__(cls)
        obj.jobs_arrived = data["jobs_arrived"]
        obj.jobs_departed = data["jobs_departed"]
        obj.stations = {sid: StationStats.from_dict(s)
                        for sid, s in data["stations"].items()}
        return obj


def _rng_state_to_json(state):
    version, internal, gauss_next = state
    return [version, list(internal), gauss_next]


def _rng_state_from_json(data):
    version, internal, gauss_next = data
    return (version, tuple(internal), gauss_next)


class Engine:
    """Event-queue simulator. Create fresh, or restore via ``from_snapshot``."""

    def __init__(self, scenario, seed=None, rng=None, log=None):
        self.scenario = scenario
        self.seed = scenario["seed"] if seed is None else seed
        self.rng = rng if rng is not None else random.Random(self.seed)
        self.horizon = float(scenario["horizon_seconds"])
        self.log = log
        self.clock = 0.0
        self.seq = 0
        self.log_seq = 0
        self.next_job_id = 1
        self.events_processed = 0
        self.heap = []
        self.jobs = {}
        self.stations = {s["id"]: StationState(s) for s in scenario["stations"]}
        self.entry_station_id = scenario["stations"][0]["id"]
        self.stats = Stats([s["id"] for s in scenario["stations"]])
        self._schedule_initial()

    # ------------------------------------------------------------------ events

    def _push(self, time, kind, payload=()):
        entry = (time, EVENT_PRIORITY[kind], self.seq, (kind,) + tuple(payload))
        heapq.heappush(self.heap, entry)
        self.seq += 1

    def _schedule_initial(self):
        arrival = self.scenario["arrival"]
        rate_per_second = arrival["rate_per_hour"] / 3600.0
        first = arrival["start_seconds"] + self.rng.expovariate(rate_per_second)
        if first <= arrival["stop_seconds"]:
            self._push(first, "arrival")
        for dt in self.scenario.get("downtime", []):
            start = float(dt["start_seconds"])
            self._push(start, "downtime_start", (dt["station"],))
            self._push(start + dt["duration_seconds"], "downtime_end", (dt["station"],))

    def run(self, stop_at=None):
        """Process events until the heap empties, the horizon is reached,
        or the next event lies strictly after ``stop_at`` (snapshot point)."""
        horizon = self.horizon
        while self.heap:
            top = self.heap[0]
            t = top[0]
            if t > horizon or (stop_at is not None and t > stop_at):
                break
            heapq.heappop(self.heap)
            self.clock = t
            self.events_processed += 1
            payload = top[3]
            kind = payload[0]
            if kind == "arrival":
                self._handle_arrival(t)
            elif kind == "service_end":
                self._handle_service_end(t, payload[1], payload[2])
            elif kind == "downtime_start":
                self._handle_downtime_start(t, payload[1])
            else:
                self._handle_downtime_end(t, payload[1])
        return self.clock

    # ---------------------------------------------------------------- handlers

    def _handle_arrival(self, t):
        arrival = self.scenario["arrival"]
        rate_per_second = arrival["rate_per_hour"] / 3600.0
        if t < arrival["stop_seconds"]:
            nxt = t + self.rng.expovariate(rate_per_second)
            if nxt <= arrival["stop_seconds"]:
                self._push(nxt, "arrival")
        job_id = self.next_job_id
        self.next_job_id += 1
        self.jobs[job_id] = t
        self.stats.jobs_arrived += 1
        self._log("arrival", job=job_id)
        self._enqueue(self.stations[self.entry_station_id], job_id, t)

    def _handle_service_end(self, t, station_id, job_id):
        st = self.stations[station_id]
        st.busy -= 1
        self.stats.stations[station_id].completed += 1
        self._log("service_end", job=job_id, station=station_id)
        spec = st.spec
        rework = spec.get("rework")
        target = None
        if rework is not None and self.rng.random() < rework["probability"]:
            target = rework["target"]
            self._log("rework", job=job_id, station=station_id, target=target)
        elif spec["next"] is not None:
            target = spec["next"]
        if target is None:
            del self.jobs[job_id]
            self.stats.jobs_departed += 1
            self._log("depart", job=job_id, station=station_id)
        else:
            self._enqueue(self.stations[target], job_id, t)
        self._dispatch(st, t)

    def _handle_downtime_start(self, t, station_id):
        st = self.stations[station_id]
        st.down = True
        st.down_since = t
        self._log("downtime_start", station=station_id)

    def _handle_downtime_end(self, t, station_id):
        st = self.stations[station_id]
        st.down = False
        self.stats.stations[station_id].downtime += t - st.down_since
        st.down_since = None
        self._log("downtime_end", station=station_id)
        self._dispatch(st, t)

    # ------------------------------------------------------------------ helpers

    def _enqueue(self, st, job_id, t):
        st.queue.append((job_id, t))
        sstats = self.stats.stations[st.spec["id"]]
        sstats.enqueued += 1
        if len(st.queue) > sstats.queue_peak:
            sstats.queue_peak = len(st.queue)
        self._log("enqueue", job=job_id, station=st.spec["id"], queue=len(st.queue))
        self._dispatch(st, t)

    def _dispatch(self, st, t):
        sstats = self.stats.stations[st.spec["id"]]
        while st.queue and not st.down and st.busy < st.spec["servers"]:
            job_id, enqueued_at = st.queue.popleft()
            duration = self._draw_service(st.spec["service"])
            st.busy += 1
            sstats.started += 1
            sstats.waits.append(t - enqueued_at)
            sstats.busy_time += min(t + duration, self.horizon) - t
            self._push(t + duration, "service_end", (st.spec["id"], job_id))
            self._log("service_start", job=job_id, station=st.spec["id"],
                      wait=t - enqueued_at, duration=duration)

    def _draw_service(self, service):
        dist = service["distribution"]
        if dist == "constant":
            return float(service["seconds"])
        if dist == "uniform":
            return self.rng.uniform(service["min_seconds"], service["max_seconds"])
        if dist == "exponential":
            return self.rng.expovariate(1.0 / service["mean_seconds"])
        raise ValueError("unknown service distribution: %r" % dist)

    def _log(self, event, **fields):
        if self.log is None:
            return
        record = {"seq": self.log_seq, "time": self.clock, "event": event}
        record.update(fields)
        self.log_seq += 1
        self.log.write(record)

    # ----------------------------------------------------------------- snapshot

    def snapshot_dict(self):
        return {
            "format": SNAPSHOT_FORMAT,
            "version": SNAPSHOT_VERSION,
            "scenario": self.scenario,
            "seed": self.seed,
            "clock": self.clock,
            "seq": self.seq,
            "log_seq": self.log_seq,
            "next_job_id": self.next_job_id,
            "events_processed": self.events_processed,
            "rng_state": _rng_state_to_json(self.rng.getstate()),
            "event_heap": [list(item) for item in sorted(self.heap)],
            "stations": {
                sid: {
                    "busy": st.busy,
                    "down": st.down,
                    "down_since": st.down_since,
                    "queue": [[job_id, enqueued_at] for job_id, enqueued_at in st.queue],
                }
                for sid, st in self.stations.items()
            },
            "jobs": {str(job_id): t for job_id, t in self.jobs.items()},
            "stats": self.stats.to_dict(),
        }

    @classmethod
    def from_snapshot(cls, data, log=None):
        if data.get("format") != SNAPSHOT_FORMAT:
            raise ValueError("not a sim snapshot: %r" % data.get("format"))
        if data.get("version") != SNAPSHOT_VERSION:
            raise ValueError("unsupported snapshot version: %r" % data.get("version"))
        self = cls.__new__(cls)
        self.scenario = data["scenario"]
        self.seed = data["seed"]
        self.rng = random.Random()
        self.rng.setstate(_rng_state_from_json(data["rng_state"]))
        self.horizon = float(self.scenario["horizon_seconds"])
        self.log = log
        self.clock = data["clock"]
        self.seq = data["seq"]
        self.log_seq = data["log_seq"]
        self.next_job_id = data["next_job_id"]
        self.events_processed = data["events_processed"]
        self.heap = [(item[0], item[1], item[2], tuple(item[3]))
                     for item in data["event_heap"]]
        heapq.heapify(self.heap)
        self.jobs = {int(job_id): t for job_id, t in data["jobs"].items()}
        self.stations = {}
        for spec in self.scenario["stations"]:
            st = StationState(spec)
            saved = data["stations"][spec["id"]]
            st.busy = saved["busy"]
            st.down = saved["down"]
            st.down_since = saved["down_since"]
            st.queue = deque((job_id, enqueued_at)
                             for job_id, enqueued_at in saved["queue"])
            self.stations[spec["id"]] = st
        self.entry_station_id = self.scenario["stations"][0]["id"]
        self.stats = Stats.from_dict(data["stats"])
        return self


def save_snapshot(engine, path):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(engine.snapshot_dict(), fh, sort_keys=True)
        fh.write("\n")


def load_snapshot(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)
