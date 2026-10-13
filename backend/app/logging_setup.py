"""Structured JSON logging with PII scrubbing (emails are masked)."""
import json
import logging
import re
import sys
import time

_EMAIL = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def mask_pii(text: str) -> str:
    return _EMAIL.sub(r"\1***@\2", text)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)), "level": record.levelname, "logger": record.name,
             "msg": mask_pii(record.getMessage())}
        for k in ("run_id", "job_id", "stage", "worker_id"):
            if hasattr(record, k):
                d[k] = getattr(record, k)
        if record.exc_info:
            d["exc"] = mask_pii(self.formatException(record.exc_info))
        return json.dumps(d)


def setup_logging(level: str = "INFO") -> None:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
