"""Scenario loading and validation."""

import json

_DISTRIBUTIONS = ("constant", "uniform", "exponential")


def load_scenario(path):
    with open(path, "r", encoding="utf-8") as fp:
        scenario = json.load(fp)
    validate_scenario(scenario)
    return scenario


def _require(cond, msg):
    if not cond:
        raise ValueError(msg)


def validate_scenario(sc):
    _require(isinstance(sc.get("name"), str), "scenario.name must be a string")
    _require(isinstance(sc.get("seed"), int), "scenario.seed must be an integer")
    _require(sc.get("horizon_seconds", 0) > 0, "horizon_seconds must be > 0")

    arr = sc.get("arrival")
    _require(isinstance(arr, dict), "scenario.arrival is required")
    _require(arr.get("process") == "poisson", "only poisson arrivals are supported")
    _require(arr.get("rate_per_hour", 0) > 0, "arrival.rate_per_hour must be > 0")
    _require(arr.get("start_seconds", -1) >= 0, "arrival.start_seconds must be >= 0")
    _require(arr.get("stop_seconds", -1) >= arr["start_seconds"],
             "arrival.stop_seconds must be >= start_seconds")
    _require(arr["stop_seconds"] <= sc["horizon_seconds"],
             "arrival.stop_seconds must be <= horizon_seconds")

    stations = sc.get("stations")
    _require(isinstance(stations, list) and stations, "scenario.stations must be a non-empty list")
    ids = [s.get("id") for s in stations]
    _require(all(isinstance(i, str) for i in ids), "station ids must be strings")
    _require(len(set(ids)) == len(ids), "station ids must be unique")

    for st in stations:
        _require(st.get("servers", 0) >= 1, "station %s: servers must be >= 1" % st["id"])
        svc = st.get("service") or {}
        dist = svc.get("distribution")
        _require(dist in _DISTRIBUTIONS, "station %s: unknown distribution %r" % (st["id"], dist))
        if dist == "constant":
            _require(svc.get("seconds", 0) > 0, "station %s: seconds must be > 0" % st["id"])
        elif dist == "uniform":
            _require(0 < svc.get("min_seconds", 0) <= svc.get("max_seconds", -1),
                     "station %s: need 0 < min_seconds <= max_seconds" % st["id"])
        else:
            _require(svc.get("mean_seconds", 0) > 0, "station %s: mean_seconds must be > 0" % st["id"])
        nxt = st.get("next")
        _require(nxt is None or nxt in ids, "station %s: unknown next %r" % (st["id"], nxt))
        rework = st.get("rework")
        if rework is not None:
            _require(0.0 <= rework.get("probability", -1) <= 1.0,
                     "station %s: rework.probability out of range" % st["id"])
            _require(rework.get("target") in ids,
                     "station %s: unknown rework target %r" % (st["id"], rework.get("target")))

    for dt in sc.get("downtime", []):
        _require(dt.get("station") in ids, "downtime: unknown station %r" % dt.get("station"))
        _require(dt.get("start_seconds", -1) >= 0, "downtime.start_seconds must be >= 0")
        _require(dt.get("duration_seconds", 0) > 0, "downtime.duration_seconds must be > 0")
