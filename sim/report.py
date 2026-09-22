"""Statistics report assembly."""
import math

REPORT_FORMAT = "sim-report"
REPORT_VERSION = 1

QUANTILES = (("p50", 0.50), ("p90", 0.90), ("p95", 0.95), ("p99", 0.99))


def quantile(sorted_values, p):
    """Linear-interpolation quantile on an already-sorted list."""
    n = len(sorted_values)
    if n == 0:
        return None
    if n == 1:
        return sorted_values[0]
    pos = (n - 1) * p
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


def wait_summary(waits):
    ordered = sorted(waits)
    summary = {"count": len(ordered), "mean": None, "max": None}
    if ordered:
        summary["mean"] = math.fsum(ordered) / len(ordered)
        summary["max"] = ordered[-1]
    for name, p in QUANTILES:
        summary[name] = quantile(ordered, p)
    return summary


def build_report(engine):
    scenario = engine.scenario
    stats = engine.stats
    stations = {}
    for spec in scenario["stations"]:
        sid = spec["id"]
        s = stats.stations[sid]
        stations[sid] = {
            "servers": spec["servers"],
            "jobs_enqueued": s.enqueued,
            "jobs_started": s.started,
            "jobs_completed": s.completed,
            "utilization": s.busy_time / (spec["servers"] * engine.horizon),
            "downtime_seconds": s.downtime,
            "queue_peak": s.queue_peak,
            "wait_seconds": wait_summary(s.waits),
        }
    return {
        "format": REPORT_FORMAT,
        "version": REPORT_VERSION,
        "scenario": scenario["name"],
        "seed": engine.seed,
        "horizon_seconds": scenario["horizon_seconds"],
        "events_processed": engine.events_processed,
        "jobs": {
            "arrived": stats.jobs_arrived,
            "departed": stats.jobs_departed,
            "in_system": stats.jobs_arrived - stats.jobs_departed,
        },
        "stations": stations,
    }
