"""Administrator authentication for the single-process Web service."""

import hashlib
import json
import os
import re
import secrets
import threading
from datetime import datetime, timedelta
from pathlib import Path

from flask import Blueprint, jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash

from .models import Setting


def _password_matches(stored_hash, password):
    # Existing installations used SHA-256; migrate after a successful login.
    if re.fullmatch(r"[0-9a-f]{64}", stored_hash):
        return secrets.compare_digest(stored_hash, hashlib.sha256(password.encode()).hexdigest())
    return check_password_hash(stored_hash, password)


def require_admin(app):
    token = request.headers.get("X-Admin-Token", "")
    with app.config["ADMIN_TOKEN_LOCK"]:
        tokens = app.config["ADMIN_TOKENS"]
        now = datetime.utcnow()
        for expired in [key for key, expiry in tokens.items() if expiry <= now]:
            tokens.pop(expired, None)
        if token and token in tokens:
            return token, None
    return None, (jsonify({"error": "请先登录管理员账号", "code": "admin_unauthorized"}), 401)


def init_admin(app):
    config_path = Path(__file__).resolve().parent.parent / "config.json"
    config = {}
    if config_path.exists():
        try:
            with config_path.open(encoding="utf-8") as handle:
                config = json.load(handle)
            if not isinstance(config, dict):
                config = {}
        except (OSError, ValueError):
            app.logger.warning("无法读取 config.json，请检查配置文件；管理员可通过环境变量配置。")

    username = os.getenv("ADMIN_USERNAME") or config.get("admin_username") or "admin"
    password = os.getenv("ADMIN_PASSWORD") or config.get("admin_password") or ""
    if not isinstance(username, str) or not isinstance(password, str):
        raise ValueError("admin_username 和 admin_password 必须为字符串")
    app.config["ADMIN_USERNAME"] = username
    app.config["ADMIN_TOKENS"] = {}
    app.config["ADMIN_TOKEN_LOCK"] = threading.Lock()

    with app.config["DB_SESSION_FACTORY"]() as db:
        setting = db.query(Setting).filter(Setting.key == "admin_key_hash").first()
        if password and (not setting or not _password_matches(setting.value, password)):
            if not setting:
                setting = Setting(key="admin_key_hash")
                db.add(setting)
            setting.value = generate_password_hash(password)
            db.commit()

    admin = Blueprint("admin_auth", __name__, url_prefix="/api/admin")

    @admin.post("/init")
    def initialize():
        # Provision credentials on the server, never through a public first-visitor API.
        return jsonify({"error": "请在服务端设置 ADMIN_PASSWORD 或 config.json 的 admin_password 后重启"}), 403

    @admin.post("/login")
    def login():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "请提交管理员账号和密码"}), 400
        supplied_username = data.get("username", username)
        supplied_password = data.get("password", data.get("key", ""))
        if not isinstance(supplied_username, str) or not isinstance(supplied_password, str):
            return jsonify({"error": "账号和密码格式错误"}), 400
        if not supplied_password or len(supplied_password) > 1024:
            return jsonify({"error": "请输入管理员密码"}), 400

        with app.config["DB_SESSION_FACTORY"]() as db:
            setting = db.query(Setting).filter(Setting.key == "admin_key_hash").first()
            if not setting:
                return jsonify({"error": "管理员尚未配置，请设置 ADMIN_PASSWORD 或 config.json 的 admin_password 后重启"}), 503
            valid_password = _password_matches(setting.value, supplied_password)
            if not secrets.compare_digest(supplied_username.encode(), username.encode()) or not valid_password:
                return jsonify({"error": "管理员账号或密码错误"}), 401
            if re.fullmatch(r"[0-9a-f]{64}", setting.value):
                setting.value = generate_password_hash(supplied_password)
                db.commit()

        token = secrets.token_urlsafe(32)
        expires_in = 24 * 3600
        with app.config["ADMIN_TOKEN_LOCK"]:
            app.config["ADMIN_TOKENS"][token] = datetime.utcnow() + timedelta(seconds=expires_in)
        return jsonify({"token": token, "expires_in": expires_in, "username": username})

    @admin.get("/me")
    def current_admin():
        _, error = require_admin(app)
        if error:
            return error
        return jsonify({"username": username})

    @admin.post("/logout")
    def logout():
        token, error = require_admin(app)
        if error:
            return error
        with app.config["ADMIN_TOKEN_LOCK"]:
            app.config["ADMIN_TOKENS"].pop(token, None)
        return jsonify({"status": "ok"})

    app.register_blueprint(admin)
