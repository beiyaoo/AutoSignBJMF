import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from web_signin.app import create_app


class SiteAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for replacement in [
            patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "site-test-password"}),
            patch("web_signin.app.db.DB_PATH", Path(self.temp.name) / "test.db"),
            patch("web_signin.app.admin.Path", return_value=Path(self.temp.name) / "app" / "admin.py"),
        ]:
            replacement.start()
            self.addCleanup(replacement.stop)
        self.app = create_app(start_scheduler=False)
        self.app.config["TESTING"] = True
        self.addCleanup(self.app.config["DB_ENGINE"].dispose)
        self.addCleanup(self.app.config["SCHEDULER"].shutdown)
        self.client = self.app.test_client()

    def login(self):
        response = self.client.post("/api/admin/login", json={"username": "admin", "password": "site-test-password"})
        self.assertEqual(response.status_code, 200)
        return response

    def test_every_private_page_redirects_anonymous_users_to_login(self):
        for url in ["/", "/admin", "/manage", "/static/index.html", "/static/manage.html", "/static/manage.js", "/static/site-auth.js"]:
            with self.subTest(url=url), self.client.get(url) as response:
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.location.startswith("/login?next="))
        for url in ["/login", "/static/login.js", "/health"]:
            with self.client.get(url) as response:
                self.assertEqual(response.status_code, 200)

    def test_all_task_and_qr_apis_require_site_login(self):
        with patch("web_signin.app.qr_service.create_qr_session") as create_qr:
            for url in ["/api/qr", "/api/qr/status?token=known", "/api/user/config?token=known",
                        "/api/user/logs?token=known", "/api/tasks/1", "/api/tasks/1/logs"]:
                self.assertEqual(self.client.get(url).status_code, 401)
            self.assertEqual(self.client.post("/api/tasks", json={"token": "known"}).status_code, 401)
            self.assertEqual(self.client.options("/api/tasks").status_code, 401)
            create_qr.assert_not_called()

    def test_login_cookie_is_http_only_and_grants_private_pages(self):
        response = self.login()
        cookie = response.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        for url in ["/", "/admin", "/manage", "/static/manage.js", "/static/site-auth.js"]:
            with self.client.get(url) as page:
                self.assertEqual(page.status_code, 200)
                self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertEqual(self.client.get("/api/admin/me").json["username"], "admin")

    def test_cookie_mutations_require_csrf_and_logout_revokes_session(self):
        self.login()
        csrf = self.client.get("/api/admin/me").json["csrf_token"]
        self.assertEqual(self.client.post("/api/admin/logout").status_code, 403)
        self.assertEqual(self.client.post("/api/admin/logout", headers={"X-CSRF-Token": "invalid"}).status_code, 403)
        self.assertEqual(self.client.post("/api/admin/logout", headers={"X-CSRF-Token": "无效"}).status_code, 403)
        self.assertEqual(self.client.post("/api/admin/logout", headers={"X-CSRF-Token": csrf}).status_code, 200)
        self.assertEqual(self.client.get("/api/admin/me").status_code, 401)
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_cookie_expiry_is_enforced_and_external_redirect_is_ignored(self):
        response = self.login()
        self.assertEqual(self.client.get("/login?next=https://example.com/").location, "/")
        self.assertEqual(self.client.get("/login?next=//example.com/").location, "/")
        self.assertEqual(self.client.get("/login?next=/admin").location, "/admin")
        self.app.config["ADMIN_TOKENS"][response.json["token"]] = datetime.utcnow() - timedelta(seconds=1)
        self.assertEqual(self.client.get("/api/admin/me").status_code, 401)
        self.assertEqual(self.client.get("/admin").status_code, 302)

    def test_bearer_api_access_remains_compatible_without_cookies(self):
        token = self.login().json["token"]
        separate = self.app.test_client()
        self.assertEqual(separate.get("/api/admin/tasks", headers={"X-Admin-Token": token}).status_code, 200)
        self.assertEqual(separate.post("/api/admin/logout", headers={"X-Admin-Token": token}).status_code, 200)
        self.assertEqual(self.client.get("/api/admin/me").status_code, 401)


if __name__ == "__main__":
    unittest.main()
