"""Event log writer.

The log is CSV with a fixed header. Every field is either an integer or a
token drawn from a small fixed set, so the byte stream is fully determined
by the scenario and the seed. `detail` uses `key=value` pairs joined by `;`
and never contains commas.
"""


class EventLogger:
    HEADER = "seq,time_us,event,station,job_id,detail"

    def __init__(self, fp):
        self.fp = fp

    def write_header(self):
        self.fp.write(self.HEADER + "\n")

    def log(self, seq, time_us, event, station, job_id, detail=""):
        self.fp.write("%d,%d,%s,%s,%s,%s\n" % (seq, time_us, event, station, job_id, detail))
