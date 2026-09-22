"""Scenario loading and validation."""
import json

SERVICE_DISTRIBUTIONS = ("constant", "uniform", "exponential")


def load_scenario(path):
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return validate_scenario(data)


def validate_scenario(data):
    """Validate a scenario dict, raising ValueError on any problem."""
    for key in ("name", "seed", "horizon_seconds", "arrival", "stations"):
        if key not in data:
            raise ValueError("scenario missing required key: %r" % key)
    if not isinstance(data["seed"], int):
        raise ValueError("seed must be an integer")
    if data["horizon_seconds"] <= 0:
        raise ValueError("horizon_seconds must be positive")

    arrival = data["arrival"]
    if arrival.get("process") != "poisson":
        raise ValueError("arrival.process must be 'poisson'")
    if arrival.get("rate_per_hour", 0) <= 0:
        raise ValueError("arrival.rate_per_hour must be positive")
    start = arrival.get("start_seconds", 0)
    stop = arrival.get("stop_seconds", 0)
    if not 0 <= start <= stop:
        raise ValueError("arrival window must satisfy 0 <= start_seconds <= stop_seconds")

    stations = data["stations"]
    if not stations:
        raise ValueError("scenario must define at least one station")
    ids = []
    for st in stations:
        sid = st.get("id")
        if not sid:
            raise ValueError("every station needs an id")
        if sid in ids:
            raise ValueError("duplicate station id: %r" % sid)
        ids.append(sid)
        if st.get("servers", 0) < 1:
            raise ValueError("station %r: servers must be >= 1" % sid)
        service = st.get("service") or {}
        dist = service.get("distribution")
        if dist not in SERVICE_DISTRIBUTIONS:
            raise ValueError("station %r: unknown service distribution %r" % (sid, dist))
        if dist == "constant" and service.get("seconds", 0) <= 0:
            raise ValueError("station %r: constant service needs seconds > 0" % sid)
        if dist == "uniform" and not 0 <= service.get("min_seconds", -1) <= service.get("max_seconds", -1):
            raise ValueError("station %r: uniform service needs 0 <= min_seconds <= max_seconds" % sid)
        if dist == "exponential" and service.get("mean_seconds", 0) <= 0:
            raise ValueError("station %r: exponential service needs mean_seconds > 0" % sid)
        if "next" not in st:
            raise ValueError("station %r: missing 'next' (use null to exit the system)" % sid)
    for st in stations:
        nxt = st["next"]
        if nxt is not None and nxt not in ids:
            raise ValueError("station %r: unknown next station %r" % (st["id"], nxt))
        rework = st.get("rework")
        if rework is not None:
            if rework.get("target") not in ids:
                raise ValueError("station %r: unknown rework target %r" % (st["id"], rework.get("target")))
            if not 0.0 <= rework.get("probability", -1) <= 1.0:
                raise ValueError("station %r: rework probability must be in [0, 1]" % st["id"])

    for dt in data.get("downtime", []):
        if dt.get("station") not in ids:
            raise ValueError("downtime references unknown station %r" % dt.get("station"))
        if dt.get("start_seconds", -1) < 0 or dt.get("duration_seconds", 0) <= 0:
            raise ValueError("downtime needs start_seconds >= 0 and duration_seconds > 0")
    return data
