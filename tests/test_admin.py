import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from web_signin.app import create_app
from web_signin.app.models import Setting, Task, TaskLog
from web_signin.app.qr_login import QRSessionState
from web_signin.app.scheduler import register_task_jobs, run_task_job


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "test-admin-password"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.db_path = patch("web_signin.app.db.DB_PATH", Path(self.temp.name) / "test.db")
        self.db_path.start()
        self.addCleanup(self.db_path.stop)
        self.config_path = patch("web_signin.app.admin.Path", return_value=Path(self.temp.name) / "app" / "admin.py")
        self.config_path.start()
        self.addCleanup(self.config_path.stop)
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.app.config["SCHEDULER"].pause()
        self.addCleanup(self.app.config["DB_ENGINE"].dispose)
        self.addCleanup(self.app.config["SCHEDULER"].shutdown)
        self.client = self.app.test_client()

    def login(self, **payload):
        response = self.client.post("/api/admin/login", json=payload or {
            "username": "admin", "password": "test-admin-password"
        })
        self.assertEqual(response.status_code, 200)
        return {"X-Admin-Token": response.json["token"]}

    def task(self, **overrides):
        values = dict(name="测试用户", class_id="1001", cookie="private-cookie", lat="30.1", lng="120.1",
                      times=json.dumps(["07:30:00", "12:30:00"]), enabled=True)
        values.update(overrides)
        with self.app.config["DB_SESSION_FACTORY"]() as db:
            task = Task(**values)
            db.add(task)
            db.commit()
            register_task_jobs(self.app, task)
            return task.id

    def task_jobs(self):
        return [job for job in self.app.config["SCHEDULER"].get_jobs() if job.id.startswith("task_")]

    def test_every_admin_resource_requires_login(self):
        task_id = self.task()
        for method, url in [
            ("get", "/api/admin/tasks"), ("get", "/api/admin/logs"), ("get", "/api/admin/me"),
            ("patch", f"/api/admin/tasks/{task_id}"), ("delete", f"/api/admin/tasks/{task_id}"),
            ("post", "/api/admin/logout")
        ]:
            with self.subTest(method=method, url=url):
                self.assertEqual(getattr(self.client, method)(url).status_code, 401)
        self.assertEqual(self.client.get("/api/admin/tasks?admin_token=invalid").status_code, 401)

    def test_login_wrong_credentials_and_malformed_data(self):
        for payload in [{"username": "other", "password": "test-admin-password"},
                        {"username": "admin", "password": "incorrect"}]:
            self.assertEqual(self.client.post("/api/admin/login", json=payload).status_code, 401)
        for payload in [[], {"password": 123}, {"password": ""}]:
            self.assertEqual(self.client.post("/api/admin/login", json=payload).status_code, 400)

    def test_logout_and_expiry_revoke_tokens(self):
        headers = self.login()
        self.assertEqual(self.client.get("/api/admin/me", headers=headers).json["username"], "admin")
        self.assertEqual(self.client.post("/api/admin/logout", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/api/admin/tasks", headers=headers).status_code, 401)
        headers = self.login()
        self.app.config["ADMIN_TOKENS"][headers["X-Admin-Token"]] = datetime.utcnow() - timedelta(seconds=1)
        self.assertEqual(self.client.get("/api/admin/tasks", headers=headers).status_code, 401)
        self.assertNotIn(headers["X-Admin-Token"], self.app.config["ADMIN_TOKENS"])

    def test_public_initialization_is_disabled_and_no_default_password(self):
        self.assertEqual(self.client.post("/api/admin/init", json={"key": "takeover"}).status_code, 401)
        with self.app.config["DB_SESSION_FACTORY"]() as db:
            db.query(Setting).delete()
            db.commit()
        self.assertEqual(self.client.post("/api/admin/login", json={"password": "admin"}).status_code, 503)
        self.assertEqual(self.client.get("/api/admin/tasks").status_code, 401)

    def test_legacy_password_is_accepted_and_migrated(self):
        with self.app.config["DB_SESSION_FACTORY"]() as db:
            db.query(Setting).filter(Setting.key == "admin_key_hash").first().value = hashlib.sha256(b"legacy-password").hexdigest()
            db.commit()
        headers = self.login(key="legacy-password")
        self.assertEqual(self.client.get("/api/admin/tasks", headers=headers).status_code, 200)
        with self.app.config["DB_SESSION_FACTORY"]() as db:
            self.assertTrue(db.query(Setting).first().value.startswith("scrypt:"))

    def test_task_list_has_schedule_and_latest_result_without_cookie(self):
        task_id = self.task()
        with self.app.config["DB_SESSION_FACTORY"]() as db:
            db.add(TaskLog(task_id=task_id, status="success", message="签到成功"))
            db.commit()
        response = self.client.get("/api/admin/tasks", headers=self.login())
        task = response.json[0]
        self.assertNotIn("cookie", task)
        self.assertNotIn("private-cookie", response.get_data(as_text=True))
        self.assertEqual(task["runtime_status"], "scheduled")
        self.assertTrue(task["next_run_at"].endswith("+08:00"))
        self.assertEqual(task["latest_run"]["status"], "success")
        self.assertTrue(task["latest_run"]["run_at"].endswith("Z"))

    def test_schedule_changes_remove_old_times_and_apply_date_range(self):
        task_id = self.task()
        headers = self.login()
        response = self.client.patch(f"/api/admin/tasks/{task_id}", headers=headers, json={"times": ["09:45"]})
        self.assertEqual(response.status_code, 200)
        jobs = self.task_jobs()
        self.assertEqual([job.id for job in jobs], [f"task_{task_id}_094500"])
        response = self.client.patch(f"/api/admin/tasks/{task_id}", headers=headers,
                                     json={"date_start": "2099-01-01", "date_end": "2099-01-02"})
        self.assertEqual(response.status_code, 200)
        job = self.task_jobs()[0]
        self.assertEqual(job.trigger.start_date.year, 2099)
        self.assertEqual(job.trigger.end_date.day, 2)
        self.assertEqual(self.client.get("/api/admin/tasks", headers=headers).json[0]["runtime_status"], "pending")
        self.assertEqual(self.client.patch(f"/api/admin/tasks/{task_id}", headers=headers, json={"enabled": False}).status_code, 200)
        self.assertEqual(self.task_jobs(), [])
        self.assertEqual(self.client.get("/api/admin/tasks", headers=headers).json[0]["runtime_status"], "disabled")

    def test_invalid_schedule_update_does_not_change_task(self):
        task_id = self.task()
        headers = self.login()
        for payload in [{"times": ["25:00"]}, {"times": []}, {"times": [1]}, {"enabled": "false"},
                        {"date_start": "2099-01-02", "date_end": "2099-01-01"}]:
            with self.subTest(payload=payload):
                self.assertEqual(self.client.patch(f"/api/admin/tasks/{task_id}", headers=headers, json=payload).status_code, 400)
        self.assertEqual(len(self.task_jobs()), 2)

    def test_real_execution_transitions_from_running_to_success(self):
        task_id = self.task()
        entered, release = threading.Event(), threading.Event()
        def attendance(student):
            entered.set()
            release.wait(5)
            return student["name"], "success"
        headers = self.login()
        with patch("utils.attendance.Task", side_effect=attendance):
            worker = threading.Thread(target=run_task_job, args=(task_id, self.app))
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                task = self.client.get("/api/admin/tasks", headers=headers).json[0]
                self.assertEqual(task["runtime_status"], "running")
                self.assertEqual(task["running_count"], 1)
                self.assertIsNotNone(task["running_since"])
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        task = self.client.get("/api/admin/tasks", headers=headers).json[0]
        self.assertEqual(task["runtime_status"], "scheduled")
        self.assertEqual(task["latest_run"]["status"], "success")
        self.assertEqual(self.app.config["TASK_RUNTIME"].snapshot(), {})

    def test_failed_execution_clears_running_state_and_records_error(self):
        task_id = self.task()
        with patch("utils.attendance.Task", side_effect=RuntimeError("private-cookie")):
            run_task_job(task_id, self.app)
        self.assertEqual(self.app.config["TASK_RUNTIME"].snapshot(), {})
        logs = self.client.get("/api/admin/logs", headers=self.login()).json
        self.assertEqual(logs[0]["status"], "error")
        self.assertNotIn("private-cookie", logs[0]["message"])

    def test_queued_disabled_or_out_of_date_task_does_not_execute(self):
        for overrides in [{"enabled": False}, {"date_start": datetime(2099, 1, 1)}, {"date_end": datetime(2000, 1, 1)}]:
            task_id = self.task(**overrides)
            with patch("utils.attendance.Task") as attendance:
                run_task_job(task_id, self.app)
                attendance.assert_not_called()
        self.assertEqual(self.app.config["TASK_RUNTIME"].snapshot(), {})

    def test_task_details_and_logs_require_owner_or_admin(self):
        task_id = self.task()
        owner = QRSessionState(token="owner", created_at=time.time(), cookie="private-cookie", user_name="测试用户")
        other = QRSessionState(token="other", created_at=time.time(), cookie="other-cookie", user_name="其他用户")
        with patch("web_signin.app.qr_service.get_session_state", side_effect=lambda token: {"owner": owner, "other": other}.get(token)):
            headers = self.login()
            anonymous = self.app.test_client()
            for suffix in ["", "/logs"]:
                url = f"/api/tasks/{task_id}{suffix}"
                self.assertEqual(anonymous.get(url).status_code, 401)
                self.assertEqual(anonymous.get(url + "?token=other").status_code, 401)
                self.assertEqual(anonymous.get(url + "?token=owner").status_code, 401)
                self.assertEqual(self.client.get(url, headers=headers).status_code, 200)

    def test_user_can_still_create_task_and_see_own_logs(self):
        state = QRSessionState(token="owner", created_at=time.time(), cookie="cookie", user_name="测试用户",
                               class_id="1001", class_code="TEST01")
        with patch("web_signin.app.qr_service.get_session_state", return_value=state):
            headers = self.login()
            bound = self.client.get("/api/qr/status?token=owner", headers=headers)
            self.assertEqual(bound.json["status"], "success")
            self.assertEqual(bound.json["user"]["class_code"], "TEST01")
            profile = self.client.get("/api/user/config?token=owner", headers=headers)
            self.assertEqual(profile.status_code, 200)
            self.assertFalse(profile.json["has_task"])
            self.assertIsNone(profile.json["config"])
            response = self.client.post("/api/tasks", headers=headers, json={"token": "owner", "coord": "30.1,120.1", "times": ["07:30"]})
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.json["times"], ["07:30:00"])
            self.assertEqual(self.client.get("/api/user/config?token=owner").status_code, 200)
            self.assertEqual(self.client.get("/api/user/logs?token=owner").status_code, 200)

    def test_missing_task_can_be_resubmitted_without_creating_duplicates(self):
        owner = QRSessionState(token="owner", created_at=time.time(), cookie="private-cookie",
                               user_name="测试用户", class_id="1001")
        deleted_id = self.task()
        other_id = self.task(cookie="other-cookie")
        headers = self.login()
        self.assertEqual(self.client.delete(f"/api/admin/tasks/{deleted_id}", headers=headers).status_code, 200)
        payload = {"token": "owner", "task_id": deleted_id, "reuse_existing": True,
                   "coord": "30.1 120.1", "times": ["07:30", "19:30"]}
        with patch("web_signin.app.qr_service.get_session_state", return_value=owner):
            missing = self.client.post("/api/tasks", headers=headers, json=payload)
            self.assertEqual(missing.status_code, 404)
            self.assertEqual(missing.json["code"], "task_missing")
            self.assertIn("再点一次", missing.json["error"])
            foreign = self.client.post("/api/tasks", headers=headers, json={**payload, "task_id": other_id})
            self.assertEqual(foreign.status_code, 403)
            self.assertEqual(foreign.json["code"], "task_owner_mismatch")

            payload["task_id"] = None
            created = self.client.post("/api/tasks", headers=headers, json=payload)
            self.assertEqual(created.status_code, 201)
            retried = self.client.post("/api/tasks", headers=headers, json=payload)
            self.assertEqual(retried.status_code, 200)
            self.assertEqual(retried.json["id"], created.json["id"])
            self.assertEqual(retried.json["times"], ["07:30:00", "19:30:00"])

            self.task()
            ambiguous = self.client.post("/api/tasks", headers=headers, json=payload)
            self.assertEqual(ambiguous.status_code, 409)
            self.assertEqual(ambiguous.json["code"], "task_selection_required")
        with self.app.config["DB_SESSION_FACTORY"]() as db:
            self.assertEqual(db.query(Task).count(), 3)
            self.assertEqual(db.get(Task, other_id).cookie, "other-cookie")

    def test_task_submission_explains_how_to_recover_an_expired_qr_session(self):
        with patch("web_signin.app.qr_service.get_session_state", return_value=None):
            response = self.client.post("/api/tasks", headers=self.login(), json={"token": "expired"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["code"], "qr_session_expired")
        self.assertIn("重新扫码", response.json["error"])

    def test_overlapping_executions_remain_running_until_last_finishes(self):
        runtime = self.app.config["TASK_RUNTIME"]
        first, second = runtime.start(1), runtime.start(1)
        runtime.finish(1, first)
        self.assertEqual(runtime.snapshot()[1]["running_count"], 1)
        runtime.finish(1, second)
        self.assertEqual(runtime.snapshot(), {})

    def test_page_routes_and_script_are_available(self):
        for route in ["/", "/admin", "/manage", "/static/manage.js"]:
            self.assertEqual(self.client.get(route).status_code, 302)
        with self.client.get("/login") as response:
            self.assertEqual(response.status_code, 200)
        headers = self.login()
        for route in ["/", "/admin", "/manage", "/static/manage.js"]:
            with self.client.get(route, headers=headers) as response:
                self.assertEqual(response.status_code, 200)

    def test_config_credentials_work_and_environment_takes_priority(self):
        config = Path(self.temp.name) / "config.json"
        config.write_text(json.dumps({"admin_username": "config-admin", "admin_password": "config-password"}), encoding="utf-8")
        with patch.dict(os.environ, {"ADMIN_USERNAME": "", "ADMIN_PASSWORD": ""}):
            configured = create_app()
        configured.config["SCHEDULER"].pause()
        try:
            response = configured.test_client().post("/api/admin/login", json={"username": "config-admin", "password": "config-password"})
            self.assertEqual(response.status_code, 200)
        finally:
            configured.config["SCHEDULER"].shutdown()
            configured.config["DB_ENGINE"].dispose()
        overridden = create_app()
        overridden.config["SCHEDULER"].pause()
        try:
            client = overridden.test_client()
            self.assertEqual(client.post("/api/admin/login", json={"username": "admin", "password": "test-admin-password"}).status_code, 200)
            self.assertEqual(client.post("/api/admin/login", json={"username": "config-admin", "password": "config-password"}).status_code, 401)
        finally:
            overridden.config["SCHEDULER"].shutdown()
            overridden.config["DB_ENGINE"].dispose()

    def test_restart_keeps_tasks_and_legacy_credentials(self):
        self.task()
        with self.app.config["DB_SESSION_FACTORY"]() as db:
            db.query(Setting).first().value = hashlib.sha256(b"legacy-password").hexdigest()
            db.commit()
        with patch.dict(os.environ, {"ADMIN_PASSWORD": ""}):
            restarted = create_app()
        restarted.config["SCHEDULER"].pause()
        try:
            client = restarted.test_client()
            login = client.post("/api/admin/login", json={"key": "legacy-password"})
            self.assertEqual(login.status_code, 200)
            tasks = client.get("/api/admin/tasks", headers={"X-Admin-Token": login.json["token"]}).json
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0]["runtime_status"], "scheduled")
        finally:
            restarted.config["SCHEDULER"].shutdown()
            restarted.config["DB_ENGINE"].dispose()


if __name__ == "__main__":
    unittest.main()
