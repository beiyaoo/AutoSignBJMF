"""Thread-safe snapshots of tasks actually executing in this Web process."""

import secrets
import threading
from datetime import datetime


class TaskRuntime:
    def __init__(self):
        self._lock = threading.Lock()
        self._runs = {}

    def start(self, task_id):
        run_id = secrets.token_hex(8)
        with self._lock:
            self._runs.setdefault(task_id, {})[run_id] = datetime.utcnow()
        return run_id

    def finish(self, task_id, run_id):
        with self._lock:
            runs = self._runs.get(task_id, {})
            runs.pop(run_id, None)
            if not runs:
                self._runs.pop(task_id, None)

    def snapshot(self):
        with self._lock:
            return {
                task_id: {"running_since": min(runs.values()).isoformat() + "Z", "running_count": len(runs)}
                for task_id, runs in self._runs.items()
            }
