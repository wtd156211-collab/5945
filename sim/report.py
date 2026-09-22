"""Statistics report assembly.

The report is a plain dict serialized as JSON with a fixed key order and
floats rounded to 6 decimals, so the output bytes depend only on the
scenario and the seed.
"""

from .engine import US_PER_SECOND

QUANTILES = (("p50", 0.50), ("p90", 0.90), ("p95", 0.95), ("p99", 0.99))


def quantile(sorted_values, p):
    """Linear-interpolation quantile on an ascending-sorted list."""
    n = len(sorted_values)
    if n == 0:
        return None
    if n == 1:
        return sorted_values[0]
    pos = p * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def _us_to_s(value):
    return None if value is None else round(value / US_PER_SECOND, 6)


def _wait_summary(waits_us):
    ordered = sorted(waits_us)
    n = len(ordered)
    summary = {"count": n}
    if n == 0:
        summary.update({"mean": None, "max": None})
        for key, _ in QUANTILES:
            summary[key] = None
        return summary
    summary["mean"] = round(sum(ordered) / n / US_PER_SECOND, 6)
    summary["max"] = _us_to_s(ordered[-1])
    for key, p in QUANTILES:
        summary[key] = _us_to_s(quantile(ordered, p))
    return summary


def build_report(engine, span_us):
    stations = []
    for st, stats in zip(engine.stations, engine.station_stats):
        stations.append({
            "id": st.id,
            "servers": st.servers,
            "utilization": round(stats.busy_us / (st.servers * span_us), 6),
            "busy_seconds": round(stats.busy_us / US_PER_SECOND, 6),
            "jobs_started": stats.started,
            "jobs_completed": stats.completed,
            "backlog_peak": stats.backlog_peak,
            "wait_seconds": _wait_summary(stats.waits),
        })

    completed = engine.completed_jobs
    return {
        "scenario": engine.scenario["name"],
        "seed": engine.seed,
        "simulated_seconds": round(span_us / US_PER_SECOND, 6),
        "events_processed": engine.events_processed,
        "jobs": {
            "arrived": engine.arrived_jobs,
            "completed": completed,
            "in_system_at_end": engine.arrived_jobs - completed,
            "time_in_system_seconds": {
                "mean": (round(engine.tis_total_us / completed / US_PER_SECOND, 6)
                         if completed else None),
                "max": _us_to_s(engine.tis_max_us) if completed else None,
            },
        },
        "stations": stations,
    }
