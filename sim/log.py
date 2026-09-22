"""Event log writer: one JSON object per line (JSON Lines)."""
import json


class LogWriter:
    def __init__(self, fh):
        self.fh = fh

    def write(self, record):
        self.fh.write(json.dumps(record, sort_keys=True))
        self.fh.write("\n")
