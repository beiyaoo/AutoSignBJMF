"""Persistent daily random attempts, fixed fallback, and failure notices."""

import hashlib
import json
import random
from datetime import datetime, timedelta

import pytz
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.jobstores.base import JobLookupError

from .models import Task, TaskDay, TaskLog, TaskNotice
from .schedule_policy import parse_times

ZONE = pytz.timezone("Asia/Shanghai")
SUCCESS = {"success", "already_signed"}
STATUS_LABELS = {
    "success": "签到成功", "already_signed": "已签到，无需重复",
    "not_started": "未开始签到", "no_sign_in": "当前无可用签到",
    "no_new_sign_in": "只发现已签到的活动，尚未发现本时段的新签到",
    "unconfirmed_sign_in": "平台未明确返回签到结果，请在班级魔方查看本时段活动",
    "skip": "登录信息不完整或 Cookie 已失效", "error": "签到执行出错",
}


def local_now():
    return datetime.now(ZONE)


def schedule_signature(task):
    config = [sorted(set(parse_times(task.times))), task.random_minutes, task.fallback_time,
              str(task.date_start), str(task.date_end)]
    if task.periods:
        config.append(task.periods)
    return hashlib.sha256(json.dumps(config).encode()).hexdigest()


def _seconds(value):
    hour, minute, second = map(int, value.split(":"))
    return hour * 3600 + minute * 60 + second


def _day_active(task, day):
    return (not task.date_start or day >= task.date_start.date()) and (
        not task.date_end or day <= task.date_end.date())


def _make_plan(task, day):
    midnight = ZONE.localize(datetime.combine(day, datetime.min.time()))
    periods = task.periods or [{"name": None, "times": parse_times(task.times), "fallback_time": task.fallback_time}]
    margin = task.random_minutes * 60
    result = []
    start = 0
    for period_index, period in enumerate(periods):
        times = sorted(set(period["times"]))
        bases = [_seconds(value) for value in times]
        fallback = _seconds(period["fallback_time"])
        period_fields = {"period": period["name"]} if period["name"] is not None else {}
        for index, base in enumerate(bases):
            # Keep random attempts ordered and inside their own period.
            lower = max(start, base - margin, (bases[index - 1] + base) // 2 + 1 if index else start)
            upper = min(base + margin, fallback - 1,
                        (base + bases[index + 1]) // 2 if index + 1 < len(bases) else fallback - 1)
            selected = random.randint(lower, upper)
            result.append({"kind": "regular", "slot": times[index], **period_fields,
                           "run_at": (midnight + timedelta(seconds=selected)).isoformat()})
        entry = {"kind": "fallback", "slot": period["fallback_time"] if period_fields else "fallback",
                 "run_at": (midnight + timedelta(seconds=fallback)).isoformat(), **period_fields}
        start = fallback + 1
        if period_index + 1 < len(periods):
            next_base = _seconds(periods[period_index + 1]["times"][0])
            latest = max(start, next_base - margin)
            entry["end_before"] = (midnight + timedelta(seconds=latest)).isoformat()
        result.append(entry)
    return result


def _ensure_day(db, task, day, now):
    date_key = day.isoformat()
    state = db.query(TaskDay).filter_by(task_id=task.id, local_day=date_key).first()
    signature = schedule_signature(task)
    if state and state.signature == signature:
        return state
    if not state:
        start = ZONE.localize(datetime.combine(day, datetime.min.time())).astimezone(pytz.utc).replace(tzinfo=None)
        existing_success = not task.periods and db.query(TaskLog.id).filter(
            TaskLog.task_id == task.id, TaskLog.status == "success",
            TaskLog.run_at >= start, TaskLog.run_at < start + timedelta(days=1),
        ).first() is not None
        state = TaskDay(task_id=task.id, local_day=date_key, signature=signature,
                        plan="[]", completed_slots="[]", succeeded=existing_success)
        db.add(state)
    plan = _make_plan(task, day)
    if task.periods and day == now.date():
        previous_slots = {_slot_key(entry) for entry in json.loads(state.plan)}
        completed = set(json.loads(state.completed_slots))
        # Catch-up is for downtime on an existing schedule, not for newly added past periods.
        for entry in plan:
            key = _slot_key(entry)
            if entry["kind"] == "fallback" and key not in previous_slots and datetime.fromisoformat(entry["run_at"]) < now:
                completed.add(key)
        state.completed_slots = json.dumps(sorted(completed))
    state.signature = signature
    state.plan = json.dumps(plan)
    return state


def _progress(state, period=None):
    if state is None:
        return {}
    if period is not None:
        return (state.period_states or {}).get(period, {})
    return {"succeeded": state.succeeded, "consecutive_failures": state.consecutive_failures,
            "fallback_done": state.fallback_done}


def random_job_id(task_id, local_day, kind, slot):
    return f"task_{task_id}_{local_day.replace('-', '')}_{kind}_{slot.replace(':', '')}"


def _slot_key(entry):
    if "period" in entry:
        return json.dumps([entry["period"], entry["kind"], entry["slot"]], ensure_ascii=False)
    return entry["kind"] + ":" + entry["slot"]


def register_random_jobs(app, task):
    from .scheduler import run_task_job

    scheduler = app.config["SCHEDULER"]
    now = local_now()
    first_day = max(now.date(), task.date_start.date()) if task.date_start else now.date()
    plans = []
    with app.config["DB_SESSION_FACTORY"]() as db:
        for day in [first_day, first_day + timedelta(days=1)]:
            if not _day_active(task, day):
                continue
            state = _ensure_day(db, task, day, now)
            completed = set(json.loads(state.completed_slots))
            for entry in json.loads(state.plan):
                progress = _progress(state, entry.get("period"))
                fallback = entry["kind"] == "fallback"
                if _slot_key(entry) in completed or progress.get("succeeded") or (fallback and progress.get("fallback_done")):
                    continue
                run_at = datetime.fromisoformat(entry["run_at"])
                if run_at <= now:
                    if not fallback:
                        continue
                    # Catch up only while the same period can still be in effect.
                    run_at = now + timedelta(seconds=1)
                    if entry.get("end_before") and run_at >= datetime.fromisoformat(entry["end_before"]):
                        continue
                plans.append((entry, run_at, state.local_day, state.signature))
        db.commit()

    for entry, run_at, day, signature in plans:
        scheduler.add_job(run_task_job, DateTrigger(run_date=run_at),
                          id=random_job_id(task.id, day, entry["kind"], entry["slot"]),
                          args=[task.id, app, entry["kind"], day, entry["slot"], signature],
                          replace_existing=True, misfire_grace_time=None)
    scheduler.add_job(refresh_daily_jobs, CronTrigger(hour=0, minute=0, second=0,
                      start_date=task.date_start, end_date=task.date_end, timezone=ZONE),
                      id=f"task_{task.id}_refresh", args=[task.id, app], replace_existing=True,
                      misfire_grace_time=None, coalesce=True)


def refresh_daily_jobs(task_id, app):
    from .scheduler import register_task_jobs
    with app.config["DB_SESSION_FACTORY"]() as db:
        task = db.query(Task).filter_by(id=task_id).first()
        if task and task.enabled:
            register_task_jobs(app, task)


def _progress_summary(progress):
    return {
        "today_succeeded": bool(progress.get("succeeded")),
        "today_regular_failures": progress.get("consecutive_failures", 0),
        "today_fallback_done": bool(progress.get("fallback_done")),
    }


def day_summary(task, state, scheduled_runs, active_run, now):
    periods = task.periods or []
    plan = json.loads(state.plan) if state else []
    entries_by_period = {}
    if periods and state:
        for entry in plan:
            entries_by_period.setdefault(entry.get("period"), []).append(entry)
    completed = set(json.loads(state.completed_slots)) if periods and state else set()
    active_ids = set(active_run.get("job_ids", ()))
    today_periods = []
    for period in periods:
        name = period["name"]
        progress = _progress_summary(_progress(state, name))
        next_runs = []
        active = False
        fallback_at = None
        for entry in entries_by_period.get(name, ()):
            job_id = random_job_id(task.id, state.local_day, entry["kind"], entry["slot"])
            if entry["kind"] == "fallback":
                fallback_at = datetime.fromisoformat(entry["run_at"])
            if job_id in active_ids:
                active = True
            elif job_id in scheduled_runs and _slot_key(entry) not in completed:
                next_runs.append(scheduled_runs[job_id])

        next_run_at = None
        if progress["today_succeeded"]:
            status = "success"
        elif active:
            status = "running"
        elif progress["today_fallback_done"]:
            status = "failed"
        elif task.enabled and next_runs:
            status = "scheduled"
            next_run_at = min(next_runs).isoformat()
        elif task.enabled and fallback_at and fallback_at <= now:
            status = "missed"
        else:
            status = "unscheduled"
        today_periods.append({"name": name, **progress, "status": status, "next_run_at": next_run_at})

    return {
        **_progress_summary(_progress(state) if not periods else {}),
        "today_plan": plan,
        "today_periods": today_periods,
    }


def execute_random_attempt(task_id, app, kind, day, slot, signature):
    from utils.attendance import Task as AttendanceTask

    now = local_now()
    with app.config["DB_SESSION_FACTORY"]() as db:
        task = db.query(Task).filter_by(id=task_id).first()
        if not task or not task.enabled or not task.random_enabled or day != now.date().isoformat():
            return
        if not _day_active(task, now.date()) or schedule_signature(task) != signature:
            return
        state = db.query(TaskDay).filter_by(task_id=task_id, local_day=day).first()
        if not state or state.signature != signature:
            return
        entries = json.loads(state.plan)
        entry = next((item for item in entries if item["kind"] == kind and item["slot"] == slot), None)
        if not entry:
            return
        period = entry.get("period")
        progress = dict(_progress(state, period))
        completed = set(json.loads(state.completed_slots))
        key = _slot_key(entry)
        if key in completed or progress.get("succeeded") or (kind == "fallback" and progress.get("fallback_done")):
            return
        fallback = next(item for item in entries if item["kind"] == "fallback" and item.get("period") == period)
        fallback_at = datetime.fromisoformat(fallback["run_at"])
        if kind == "regular" and now >= fallback_at:
            return
        if kind == "fallback" and entry.get("end_before") and now >= datetime.fromisoformat(entry["end_before"]):
            return
        student = {
            "name": task.name, "class": task.class_id, "cookie": task.cookie,
            "lat": task.lat, "lng": task.lng, "acc": task.acc or "30",
            # The daily policy controls notifications; attendance must not send its own.
            "QmsgKEY": "", "WXKey": "",
        }
        if period is not None:
            student["require_new_sign_in"] = True
        try:
            name, status = AttendanceTask(student)
        except Exception:
            name, status = task.name, "error"
        if period is not None and status == "already_signed":
            status = "no_new_sign_in"
        label = STATUS_LABELS.get(status, "未知签到结果")
        completed.add(key)
        state.completed_slots = json.dumps(sorted(completed))
        if kind == "fallback":
            progress["fallback_done"] = True
        identity = f"{day} {task.name}" + (f"（{period}）" if period is not None else "")
        if status in SUCCESS:
            progress["succeeded"] = True
            progress["consecutive_failures"] = 0
        elif kind == "regular":
            progress["consecutive_failures"] = progress.get("consecutive_failures", 0) + 1
            if progress["consecutive_failures"] >= 2:
                _queue_notice(db, state, "consecutive_failure",
                              f"{identity} 普通签到连续两次失败，原因：{label}。仍会在 {fallback_at:%H:%M:%S} 兜底。", period)
        else:
            _queue_notice(db, state, "fallback_failure",
                          f"{identity} 最后兜底签到失败，原因：{label}，请及时手动检查。", period)

        if period is None:
            for field, value in progress.items():
                setattr(state, field, value)
        else:
            state.period_states = {**(state.period_states or {}), period: progress}

        log_status = "success" if status in SUCCESS else (
            "skipped" if status in {"not_started", "no_sign_in", "no_new_sign_in", "skip"} else "error")
        description = "普通随机尝试" if kind == "regular" else "最后兜底尝试"
        db.add(TaskLog(task_id=task_id, run_at=now.astimezone(pytz.utc).replace(tzinfo=None),
                       status=log_status, message="\n".join([
                           f"用户姓名: {name or task.name}", f"班级标识: {task.class_id}",
                           f"签到日期: {day}", f"执行类型: {description}",
                           *([f"签到时段: {period}"] if period is not None else []),
                           f"计划时间: {entry['run_at']}", f"执行时间: {now.isoformat()}",
                           f"原始状态码: {status}", f"签到结果: {label}",
                           f"普通连续失败次数: {progress.get('consecutive_failures', 0)}",
                       ])))
        db.commit()
        succeeded = progress.get("succeeded")

    if succeeded:
        with app.config["TASK_RUNTIME"].schedule_lock:
            for planned in entries:
                if planned.get("period") == period:
                    try:
                        app.config["SCHEDULER"].remove_job(random_job_id(task_id, day, planned["kind"], planned["slot"]))
                    except JobLookupError:
                        pass


def _queue_notice(db, state, kind, message, period=None):
    if period is not None:
        scope = hashlib.sha256(period.encode()).hexdigest()[:16]
        kind = ("regular:" if kind == "consecutive_failure" else "fallback:") + scope
    existing = db.query(TaskNotice).filter_by(day_id=state.id, kind=kind).first()
    if not existing:
        db.add(TaskNotice(day_id=state.id, kind=kind, message=message))


def dispatch_pending_notices(app):
    from utils.notification import sendQQmessage, wx_send

    lock = app.config["TASK_RUNTIME"].notice_lock
    if not lock.acquire(blocking=False):
        return
    try:
        with app.config["DB_SESSION_FACTORY"]() as db:
            notices = db.query(TaskNotice).filter(TaskNotice.delivered_at.is_(None)).order_by(TaskNotice.id).all()
            for notice in notices:
                task = notice.day.task
                if not task.enabled:
                    continue
                errors = []
                channels = [("wx_sent", task.wx_key, wx_send), ("qq_sent", task.qq_key, sendQQmessage)]
                for field, key, sender in channels:
                    if not key or getattr(notice, field):
                        continue
                    try:
                        if sender(key, notice.message):
                            setattr(notice, field, True)
                        else:
                            errors.append("微信推送失败" if field == "wx_sent" else "QQ 推送失败")
                    except Exception:
                        errors.append("微信推送失败" if field == "wx_sent" else "QQ 推送失败")
                    # Save each acknowledged channel so retries do not repeat it.
                    db.commit()
                if (task.wx_key or task.qq_key) and (not task.wx_key or notice.wx_sent) and (not task.qq_key or notice.qq_sent):
                    notice.delivered_at = datetime.utcnow()
                    notice.last_error = None
                else:
                    notice.last_error = "；".join(errors) or "未配置通知 Key"
                db.commit()
    finally:
        lock.release()
