"""Display task logs in Beijing time while preserving stored UTC timestamps."""

import re
from datetime import timezone

import pytz


BEIJING = pytz.timezone("Asia/Shanghai")
LEGACY_FIXED_HEADER = re.compile(r"^={18}[0-9]{2}:[0-9]{2}:[0-9]{2}={19}(?=\r?\n|$)")


def beijing_log_time(run_at):
    if run_at.tzinfo is None:
        run_at = run_at.replace(tzinfo=timezone.utc)
    return run_at.astimezone(BEIJING).strftime("%Y/%m/%d %H:%M:%S")


def execution_time_line(run_at):
    return f"执行时间（北京时间）: {beijing_log_time(run_at)}"


def task_log_payload(log, task=None):
    run_at = log.run_at
    message = log.message
    if run_at is not None and message and LEGACY_FIXED_HEADER.match(message):
        # Correct the display of old fixed-time logs without rewriting the database.
        message = LEGACY_FIXED_HEADER.sub(execution_time_line(run_at), message, count=1)
    payload = {
        "id": log.id,
        "run_at": run_at.isoformat() if run_at is not None else None,
        "run_at_beijing": beijing_log_time(run_at) if run_at is not None else None,
        "status": log.status,
        "message": message,
    }
    if task is not None:
        payload["task"] = {
            "id": task.id, "name": task.name, "class_id": task.class_id,
            "lat": task.lat, "lng": task.lng,
        }
    return payload
