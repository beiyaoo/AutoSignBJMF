import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from sqlalchemy import inspect

from web_signin.app import create_app
from web_signin.app.daily_schedule import ZONE, _make_plan, dispatch_pending_notices, refresh_daily_jobs
from web_signin.app.models import Task, TaskDay, TaskLog, TaskNotice
from web_signin.app.qr_login import QRSessionState
from web_signin.app.scheduler import register_task_jobs, run_task_job
from utils.notification import sendQQmessage as real_send_qq, wx_send as real_send_wx


class DailyScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = ZONE.localize(datetime(2030, 1, 15, 6))
        for replacement in [
            patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": ""}),
            patch("web_signin.app.db.DB_PATH", Path(self.temp.name) / "test.db"),
            patch("web_signin.app.admin.Path", return_value=Path(self.temp.name) / "app" / "admin.py"),
            patch("web_signin.app.daily_schedule.local_now", side_effect=lambda: self.now),
            patch("web_signin.app.local_now", side_effect=lambda: self.now),
        ]:
            replacement.start()
            self.addCleanup(replacement.stop)
        self.app = self.new_app()
        self.client = self.app.test_client()
        self.app.config["ADMIN_TOKENS"]["test-token"] = datetime.utcnow() + timedelta(days=1)
        self.headers = {"X-Admin-Token": "test-token"}
        attendance = patch("utils.attendance.Task", return_value=("测试用户", "error"))
        self.attendance = attendance.start()
        self.addCleanup(attendance.stop)
        for channel, name in [("wx_send", "wx"), ("sendQQmessage", "qq")]:
            sender = patch("utils.notification." + channel, return_value=True)
            setattr(self, name, sender.start())
            self.addCleanup(sender.stop)

    def new_app(self):
        app = create_app(start_scheduler=False)
        app.config["TESTING"] = True
        self.addCleanup(app.config["DB_ENGINE"].dispose)
        self.addCleanup(app.config["SCHEDULER"].shutdown)
        return app

    def task(self, **overrides):
        values = dict(name="测试用户", class_id="1001", cookie="private-cookie", lat="30.1", lng="120.1",
                      times=json.dumps(["07:30:00", "08:00:00"]), random_enabled=True,
                      random_minutes=5, fallback_time="09:00:00", wx_key="wx-test", qq_key="qq-test", enabled=True)
        values.update(overrides)
        with self.app.config["DB_SESSION_FACTORY"]() as db:
            task = Task(**values)
            db.add(task)
            db.commit()
            register_task_jobs(self.app, task)
            return task.id

    def state(self, task_id, day=None):
        with self.app.config["DB_SESSION_FACTORY"]() as db:
            state = db.query(TaskDay).filter_by(task_id=task_id, local_day=day or self.now.date().isoformat()).one()
            return dict(plan=json.loads(state.plan), completed=json.loads(state.completed_slots),
                        succeeded=state.succeeded, failures=state.consecutive_failures, fallback_done=state.fallback_done)

    def args(self, task_id, kind="regular", index=0, app=None):
        app = app or self.app
        jobs = [job for job in app.config["SCHEDULER"].get_jobs() if
                job.id.startswith(f"task_{task_id}_{self.now.strftime('%Y%m%d')}_{kind}_")]
        return sorted(jobs, key=lambda job: job.next_run_time)[index].args

    def attempt(self, args, status="error"):
        state = self.state(args[0], args[3])
        entry = next(item for item in state["plan"] if item["kind"] == args[2] and item["slot"] == args[4])
        self.now = datetime.fromisoformat(entry["run_at"]) + timedelta(seconds=1)
        self.attendance.return_value = ("测试用户", status)
        run_task_job(*args)

    def notices(self):
        with self.app.config["DB_SESSION_FACTORY"]() as db:
            return [(notice.kind, notice.wx_sent, notice.qq_sent, notice.delivered_at is not None)
                    for notice in db.query(TaskNotice).order_by(TaskNotice.id).all()]

    def test_random_windows_and_fixed_fallback_repeat_daily_without_reroll(self):
        with patch("web_signin.app.daily_schedule.random.randint", side_effect=[26700, 28500, 27300, 29100]):
            task_id = self.task()
        original = self.state(task_id)["plan"]
        self.assertEqual([entry["run_at"][11:19] for entry in original], ["07:25:00", "07:55:00", "09:00:00"])
        tomorrow = self.state(task_id, "2030-01-16")["plan"]
        self.assertEqual([entry["run_at"][11:19] for entry in tomorrow], ["07:35:00", "08:05:00", "09:00:00"])
        refresh_daily_jobs(task_id, self.app)
        self.assertEqual(self.state(task_id)["plan"], original)
        self.new_app()
        self.assertEqual(self.state(task_id)["plan"], original)

    def test_overlapping_windows_are_ordered_and_do_not_cross_midnight_or_fallback(self):
        task_id = self.task(times=json.dumps(["00:00:01", "00:01:00", "00:02:00"]), fallback_time="00:02:01")
        with self.app.config["DB_SESSION_FACTORY"]() as db:
            task = db.get(Task, task_id)
            for _ in range(50):
                entries = _make_plan(task, self.now.date())
                selected = [datetime.fromisoformat(entry["run_at"]) for entry in entries]
                self.assertEqual(selected, sorted(selected))
                self.assertTrue(all(value.date() == self.now.date() for value in selected))
                self.assertTrue(all(value < selected[-1] for value in selected[:-1]))

    def test_first_success_skips_remaining_regular_attempts_and_fallback(self):
        task_id = self.task()
        first, second, fallback = self.args(task_id), self.args(task_id, index=1), self.args(task_id, "fallback")
        self.attempt(first, "success")
        self.attempt(second)
        self.attempt(fallback)
        self.assertEqual(self.attendance.call_count, 1)
        self.assertTrue(self.state(task_id)["succeeded"])
        self.assertFalse(self.state(task_id)["fallback_done"])
        self.wx.assert_not_called()
        self.qq.assert_not_called()
        self.assertFalse(any(job.id.startswith(f"task_{task_id}_20300115_") for job in self.app.config["SCHEDULER"].get_jobs()))
        self.assertTrue(any(job.id.startswith(f"task_{task_id}_20300116_") for job in self.app.config["SCHEDULER"].get_jobs()))

    def test_independent_periods_keep_evening_after_morning_success_and_restart(self):
        task_id = self.task(periods=[
            {"name": "早上", "times": ["07:30:00", "08:00:00"], "fallback_time": "09:00:00"},
            {"name": "晚上", "times": ["18:30:00", "19:00:00"], "fallback_time": "20:00:00"},
        ])
        self.attempt(self.args(task_id), "success")
        restarted = self.new_app()
        evening = self.args(task_id, app=restarted)
        self.assertEqual(evening[4], "18:30:00")
        self.attempt(evening, "already_signed")
        self.assertTrue(self.attendance.call_args.args[0]["require_new_sign_in"])
        with restarted.config["DB_SESSION_FACTORY"]() as db:
            self.assertIn("尚未发现本时段的新签到", db.query(TaskLog).order_by(TaskLog.id.desc()).first().message)
        self.attempt(self.args(task_id, index=1, app=restarted), "success")
        listed = self.client.get("/api/admin/tasks", headers=self.headers).json[0]
        self.assertEqual([(item["name"], item["today_succeeded"]) for item in listed["today_periods"]],
                         [("早上", True), ("晚上", True)])
        self.assertEqual(self.attendance.call_count, 3)
        self.wx.assert_not_called()

    def test_period_notices_and_failure_streaks_are_independent(self):
        task_id = self.task(periods=[
            {"name": "早上", "times": ["07:30:00", "08:00:00"], "fallback_time": "09:00:00"},
            {"name": "晚上", "times": ["18:30:00", "19:00:00"], "fallback_time": "20:00:00"},
        ])
        regulars = [self.args(task_id, index=index) for index in range(4)]
        early, late = (self.args(task_id, "fallback", index=index) for index in range(2))
        self.attempt(regulars[0])
        self.attempt(regulars[1])
        self.attempt(early)
        self.attempt(regulars[2])
        self.assertEqual(self.wx.call_count, 2)
        self.attempt(regulars[3])
        self.attempt(late)
        self.assertEqual(self.wx.call_count, 4)
        self.assertEqual(len({entry[0] for entry in self.notices()}), 4)
        self.assertIn("早上", self.wx.call_args_list[0].args[1])
        self.assertIn("晚上", self.wx.call_args_list[2].args[1])

    def test_admin_lists_four_periods_with_independent_results_and_next_runs(self):
        task_id = self.task(random_minutes=0, periods=[
            {"name": "第一节", "times": ["07:30:00"], "fallback_time": "08:00:00"},
            {"name": "第二节", "times": ["10:00:00"], "fallback_time": "10:30:00"},
            {"name": "第三节", "times": ["13:00:00"], "fallback_time": "13:30:00"},
            {"name": "第四节", "times": ["17:00:00"], "fallback_time": "17:30:00"},
        ])
        first, second, third, fourth = (self.args(task_id, index=index) for index in range(4))
        second_fallback = self.args(task_id, "fallback", index=1)
        self.attempt(first, "success")
        self.attempt(second)
        self.attempt(second_fallback)
        runtime = self.app.config["TASK_RUNTIME"]
        self.now = ZONE.localize(datetime(2030, 1, 15, 13))
        third_job = next(job for job in self.app.config["SCHEDULER"].get_jobs() if job.args == third)
        run_id = runtime.start(task_id, third_job.id)
        try:
            rows = self.client.get("/api/admin/tasks", headers=self.headers).json[0]["today_periods"]
            self.assertEqual([(row["name"], row["status"]) for row in rows], [
                ("第一节", "success"), ("第二节", "failed"), ("第三节", "running"), ("第四节", "scheduled"),
            ])
            self.assertEqual([row["next_run_at"] for row in rows], [None, None, None, "2030-01-15T17:00:00+08:00"])
        finally:
            runtime.finish(task_id, run_id)
        self.attempt(third, "success")
        self.attempt(fourth, "success")
        rows = self.client.get("/api/admin/tasks", headers=self.headers).json[0]["today_periods"]
        self.assertEqual([row["status"] for row in rows], ["success", "failed", "success", "success"])

    def test_switching_from_daily_mode_does_not_replay_new_past_periods(self):
        task_id = self.task()
        self.attempt(self.args(task_id), "success")
        self.now = ZONE.localize(datetime(2030, 1, 15, 13))
        response = self.client.patch(f"/api/admin/tasks/{task_id}", headers=self.headers, json={"periods": [
            {"name": "一", "times": ["07:30"], "fallback_time": "08:00"},
            {"name": "二", "times": ["14:00"], "fallback_time": "14:30"},
            {"name": "三", "times": ["18:00"], "fallback_time": "18:30"},
        ]})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.args(task_id, "fallback")[4], "14:30:00")
        restarted = self.new_app()
        self.assertEqual(self.args(task_id, "fallback", app=restarted)[4], "14:30:00")
        rows = self.client.get("/api/admin/tasks", headers=self.headers).json[0]["today_periods"]
        self.assertEqual([row["status"] for row in rows], ["missed", "scheduled", "scheduled"])

    def test_restart_at_evening_does_not_replay_the_morning_fallback(self):
        task_id = self.task(periods=[
            {"name": "早上", "times": ["07:30:00"], "fallback_time": "09:00:00"},
            {"name": "晚上", "times": ["18:30:00"], "fallback_time": "20:00:00"},
        ])
        self.now = ZONE.localize(datetime(2030, 1, 15, 19))
        restarted = self.new_app()
        self.assertEqual(self.args(task_id, "fallback", app=restarted)[4], "20:00:00")

    def test_period_configuration_can_create_and_update_one_student_task(self):
        periods = [
            {"name": "早上", "times": ["07:30", "07:50"], "fallback_time": "08:10"},
            {"name": "晚上", "times": ["18:30", "18:50"], "fallback_time": "19:10"},
        ]
        qr = QRSessionState(token="qr-test", created_at=time.time(), cookie="periods-cookie",
                            user_name="时段用户", class_id="1001")
        payload = {"token": "qr-test", "coord": "30.1 120.1", "periods": periods, "wx_key": "wx-test"}
        with patch("web_signin.app.qr_service.get_session_state", return_value=qr):
            created = self.client.post("/api/tasks", json=payload, headers=self.headers)
            self.assertEqual(created.status_code, 201, created.json)
            self.assertEqual(created.json["periods"][1]["times"], ["18:30:00", "18:50:00"])
            task_id = created.json["id"]
            overlapping = [dict(periods[0], fallback_time="18:35"), periods[1]]
            invalid = self.client.patch(f"/api/admin/tasks/{task_id}", headers=self.headers,
                                        json={"periods": overlapping})
            self.assertEqual(invalid.status_code, 400)
            replaced = self.client.patch(f"/api/admin/tasks/{task_id}", headers=self.headers,
                                         json={"periods": [], "random_enabled": False, "times": ["19:00"]})
            self.assertEqual(replaced.status_code, 200, replaced.json)
            self.assertEqual(replaced.json["periods"], [])
            self.assertEqual(replaced.json["times"], ["19:00:00"])

    def test_first_failure_then_already_signed_does_not_alert_or_use_fallback(self):
        task_id = self.task()
        first, second, fallback = self.args(task_id), self.args(task_id, index=1), self.args(task_id, "fallback")
        self.attempt(first, "not_started")
        self.assertEqual(self.state(task_id)["failures"], 1)
        self.attempt(second, "already_signed")
        self.attempt(fallback)
        self.assertEqual(self.attendance.call_count, 2)
        self.assertEqual(self.state(task_id)["failures"], 0)
        self.assertEqual(self.notices(), [])

    def test_two_regular_failures_alert_once_and_fallback_failure_alerts_separately(self):
        task_id = self.task(times=json.dumps(["07:30:00", "08:00:00", "08:30:00"]))
        attempts = [self.args(task_id, index=index) for index in range(3)]
        fallback = self.args(task_id, "fallback")
        self.attempt(attempts[0], "no_sign_in")
        self.wx.assert_not_called()
        self.attempt(attempts[1], "skip")
        self.assertIn("连续两次失败", self.wx.call_args.args[1])
        self.attempt(attempts[2])
        self.assertEqual(self.wx.call_count, 1)
        self.attempt(fallback)
        self.attempt(fallback)
        self.assertEqual(self.attendance.call_count, 4)
        self.assertEqual(self.wx.call_count, 2)
        self.assertEqual(self.qq.call_count, 2)
        self.assertEqual([entry[0] for entry in self.notices()], ["consecutive_failure", "fallback_failure"])
        self.assertTrue(self.state(task_id)["fallback_done"])

    def test_fallback_success_does_not_send_failure_notice(self):
        task_id = self.task()
        first, second, fallback = self.args(task_id), self.args(task_id, index=1), self.args(task_id, "fallback")
        self.attempt(first)
        self.attempt(second)
        self.attempt(fallback, "success")
        self.assertTrue(self.state(task_id)["succeeded"])
        self.assertEqual(self.wx.call_count, 1)
        self.assertEqual(len(self.notices()), 1)

    def test_restart_keeps_failure_streak_and_completed_slots(self):
        task_id = self.task()
        first, second = self.args(task_id), self.args(task_id, index=1)
        self.attempt(first)
        restarted = self.new_app()
        self.assertFalse(any(job.args[2:5] == first[2:5] for job in restarted.config["SCHEDULER"].get_jobs()))
        second = self.args(task_id, index=0, app=restarted)
        self.attempt(second)
        self.assertEqual(self.state(task_id)["failures"], 2)
        self.assertEqual(self.wx.call_count, 1)

    def test_restart_after_success_does_not_restore_today_fallback(self):
        task_id = self.task()
        self.attempt(self.args(task_id), "success")
        restarted = self.new_app()
        self.assertFalse(any(job.id.startswith(f"task_{task_id}_20300115_") for job in restarted.config["SCHEDULER"].get_jobs()))

    def test_new_day_resets_success_and_failure_streak(self):
        task_id = self.task()
        self.attempt(self.args(task_id), "success")
        self.now = ZONE.localize(datetime(2030, 1, 16, 6))
        refresh_daily_jobs(task_id, self.app)
        self.assertFalse(self.state(task_id)["succeeded"])
        self.assertEqual(self.state(task_id)["failures"], 0)
        self.attempt(self.args(task_id))
        self.assertEqual(self.state(task_id)["failures"], 1)
        self.wx.assert_not_called()

    def test_missed_fallback_is_recovered_once_after_restart(self):
        task_id = self.task()
        self.now = ZONE.localize(datetime(2030, 1, 15, 16))
        restarted = self.new_app()
        args = self.args(task_id, "fallback", app=restarted)
        job = next(job for job in restarted.config["SCHEDULER"].get_jobs() if job.args == args)
        self.assertEqual(job.next_run_time, self.now + timedelta(seconds=1))
        run_task_job(*args)
        refresh_daily_jobs(task_id, restarted)
        self.assertFalse(any(job.id.startswith(f"task_{task_id}_20300115_fallback_") for job in restarted.config["SCHEDULER"].get_jobs()))
        self.assertEqual(self.notices()[0][0], "fallback_failure")

    def test_overlapping_regular_and_fallback_do_not_duplicate_success(self):
        task_id = self.task()
        regular, fallback = self.args(task_id), self.args(task_id, "fallback")
        entered, release = threading.Event(), threading.Event()
        def attendance(student):
            entered.set()
            release.wait(3)
            return student["name"], "success"
        self.now = ZONE.localize(datetime(2030, 1, 15, 7, 30))
        self.attendance.side_effect = attendance
        first = threading.Thread(target=run_task_job, args=regular)
        second = threading.Thread(target=run_task_job, args=fallback)
        first.start()
        try:
            self.assertTrue(entered.wait(2))
            self.now = ZONE.localize(datetime(2030, 1, 15, 9))
            second.start()
        finally:
            release.set()
            first.join(4)
            second.join(4)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(self.attendance.call_count, 1)
        self.assertEqual(self.app.config["TASK_RUNTIME"].snapshot(), {})

    def test_failed_notice_retries_only_failed_channel(self):
        task_id = self.task()
        first, second = self.args(task_id), self.args(task_id, index=1)
        self.wx.return_value = False
        self.attempt(first)
        self.attempt(second)
        self.assertEqual(self.notices(), [("consecutive_failure", False, True, False)])
        self.wx.return_value = True
        dispatch_pending_notices(self.app)
        dispatch_pending_notices(self.app)
        self.assertEqual(self.wx.call_count, 2)
        self.assertEqual(self.qq.call_count, 1)
        self.assertEqual(self.notices(), [("consecutive_failure", True, True, True)])

    def test_api_requires_fallback_later_than_regular_and_notification_key(self):
        task_id = self.task(random_enabled=False)
        for payload in [{"random_enabled": True, "fallback_time": None},
                        {"random_enabled": True, "fallback_time": "08:00"},
                        {"random_enabled": True, "fallback_time": "09:00", "wx_key": "", "qq_key": ""},
                        {"random_enabled": True, "fallback_time": "09:00", "random_minutes": -1}]:
            with self.subTest(payload=payload):
                self.assertEqual(self.client.patch(f"/api/admin/tasks/{task_id}", headers=self.headers, json=payload).status_code, 400)
        response = self.client.patch(f"/api/admin/tasks/{task_id}", headers=self.headers,
                                     json={"random_enabled": True, "fallback_time": "09:00"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["fallback_time"], "09:00:00")
        task = self.client.get("/api/admin/tasks", headers=self.headers).json[0]
        self.assertEqual(len(task["today_plan"]), 3)
        self.assertEqual(task["today_regular_failures"], 0)

    def test_user_edits_owned_task_without_creating_duplicate_or_editing_other_owner(self):
        task_id = self.task()
        state = QRSessionState(token="owner", created_at=time.time(), cookie="private-cookie", user_name="测试用户", class_id="1001")
        payload = {"token": "owner", "task_id": task_id, "coord": "30.1 120.1", "times": ["07:30", "08:00"],
                   "random_enabled": True, "fallback_time": "09:00", "wx_key": "wx-test"}
        with patch("web_signin.app.qr_service.get_session_state", return_value=state):
            response = self.client.post("/api/tasks", json=payload, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["id"], task_id)
            self.assertTrue(self.client.get("/api/user/config?token=owner", headers=self.headers).json["config"]["random_enabled"])
            state.cookie = "other-cookie"
            self.assertEqual(self.client.post("/api/tasks", json=payload, headers=self.headers).status_code, 403)
        with self.app.config["DB_SESSION_FACTORY"]() as db:
            self.assertEqual(db.query(Task).count(), 1)

    def test_old_sqlite_database_migrates_without_losing_users_and_is_idempotent(self):
        legacy_path = Path(self.temp.name) / "legacy.db"
        with sqlite3.connect(legacy_path) as connection:
            connection.executescript('''CREATE TABLE tasks (
                id INTEGER PRIMARY KEY, name VARCHAR(100) NOT NULL, class_id VARCHAR(100) NOT NULL,
                cookie TEXT NOT NULL, lat VARCHAR(32) NOT NULL, lng VARCHAR(32) NOT NULL, acc VARCHAR(32),
                wx_key VARCHAR(255), qq_key VARCHAR(255), times TEXT NOT NULL, enabled BOOLEAN,
                date_start DATETIME, date_end DATETIME, created_at DATETIME, updated_at DATETIME);
                INSERT INTO tasks(id,name,class_id,cookie,lat,lng,times,enabled)
                VALUES(1,'old-user','1001','old-cookie','30','120','["07:30"]',1);''')
        connection.close()
        with patch("web_signin.app.db.DB_PATH", legacy_path):
            migrated = self.new_app()
            self.new_app()
        columns = {column["name"] for column in inspect(migrated.config["DB_ENGINE"]).get_columns("tasks")}
        self.assertTrue({"random_enabled", "random_minutes", "fallback_time"}.issubset(columns))
        with migrated.config["DB_SESSION_FACTORY"]() as db:
            task = db.get(Task, 1)
            self.assertEqual(task.cookie, "old-cookie")
            self.assertFalse(task.random_enabled)
            self.assertEqual(task.random_minutes, 5)

    def test_notification_helpers_check_api_failure_even_when_http_is_200(self):
        response = Mock(status_code=200)
        with patch("utils.notification.requests.post", return_value=response):
            response.json.return_value = {"success": False, "code": 500}
            self.assertFalse(real_send_qq("test-key", "模拟提醒"))
            self.assertFalse(real_send_wx("test-key", "模拟提醒"))
            response.json.return_value = {"success": True, "code": 0}
            self.assertTrue(real_send_qq("test-key", "模拟提醒"))
            self.assertTrue(real_send_wx("test-key", "模拟提醒"))
            response.status_code = 500
            self.assertFalse(real_send_qq("test-key", "模拟提醒"))
            self.assertFalse(real_send_wx("test-key", "模拟提醒"))

    def test_only_configured_channel_is_sent_and_disabled_task_skips_attempts(self):
        task_id = self.task(wx_key="")
        first, second, fallback = self.args(task_id), self.args(task_id, index=1), self.args(task_id, "fallback")
        self.attempt(first)
        self.attempt(second)
        self.wx.assert_not_called()
        self.assertEqual(self.qq.call_count, 1)
        self.assertEqual(self.notices(), [("consecutive_failure", False, True, True)])
        self.client.patch(f"/api/admin/tasks/{task_id}", headers=self.headers, json={"enabled": False})
        self.attempt(fallback)
        self.assertEqual(self.attendance.call_count, 2)

    def test_success_logged_before_enabling_random_mode_skips_today(self):
        task_id = self.task(random_enabled=False)
        with self.app.config["DB_SESSION_FACTORY"]() as db:
            db.add(TaskLog(task_id=task_id, run_at=ZONE.localize(datetime(2030, 1, 15, 7)).astimezone(timezone.utc).replace(tzinfo=None),
                           status="success", message="旧固定任务今天已完成"))
            db.commit()
        response = self.client.patch(f"/api/admin/tasks/{task_id}", headers=self.headers, json={"random_enabled": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.state(task_id)["succeeded"])
        self.assertFalse(any(job.id.startswith(f"task_{task_id}_20300115_") for job in self.app.config["SCHEDULER"].get_jobs()))

    def test_stale_schedule_and_late_regular_attempt_are_ignored(self):
        task_id = self.task()
        old = self.args(task_id)
        self.client.patch(f"/api/admin/tasks/{task_id}", headers=self.headers, json={"random_minutes": 3})
        self.attempt(old)
        self.attendance.assert_not_called()
        current = self.args(task_id)
        self.now = ZONE.localize(datetime(2030, 1, 15, 9, 1))
        run_task_job(*current)
        self.attendance.assert_not_called()

    def test_deleting_task_removes_daily_state_and_pending_notices(self):
        task_id = self.task()
        first, second = self.args(task_id), self.args(task_id, index=1)
        self.wx.return_value = False
        self.attempt(first)
        self.attempt(second)
        self.assertEqual(self.client.delete(f"/api/admin/tasks/{task_id}", headers=self.headers).status_code, 200)
        with self.app.config["DB_SESSION_FACTORY"]() as db:
            self.assertEqual(db.query(TaskDay).count(), 0)
            self.assertEqual(db.query(TaskNotice).count(), 0)


if __name__ == "__main__":
    unittest.main()
