from datetime import datetime

from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, ForeignKey, UniqueConstraint, JSON
from sqlalchemy.orm import relationship

from .db import Base


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)  # 学生姓名
    class_id = Column(String(100), nullable=False)  # 班级 ID 或名称
    cookie = Column(Text, nullable=False)  # 登录凭据，仅后端使用
    lat = Column(String(32), nullable=False)
    lng = Column(String(32), nullable=False)
    acc = Column(String(32), nullable=True)
    wx_key = Column(String(255), nullable=True)
    qq_key = Column(String(255), nullable=True)
    times = Column(Text, nullable=False)  # JSON 字符串，存每天执行的多个时间点
    random_enabled = Column(Boolean, nullable=False, default=False)
    random_minutes = Column(Integer, nullable=False, default=5)
    fallback_time = Column(String(8), nullable=True)
    periods = Column(JSON, nullable=True)
    # 可选的任务生效日期范围（本地日期，按 Asia/Shanghai 理解）；为空则表示不限制
    date_start = Column(DateTime, nullable=True)
    date_end = Column(DateTime, nullable=True)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    logs = relationship("TaskLog", back_populates="task", cascade="all, delete-orphan")
    days = relationship("TaskDay", back_populates="task", cascade="all, delete-orphan")


class TaskDay(Base):
    __tablename__ = "task_days"
    __table_args__ = (UniqueConstraint("task_id", "local_day"),)

    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False, index=True)
    local_day = Column(String(10), nullable=False, index=True)
    signature = Column(String(64), nullable=False)
    plan = Column(Text, nullable=False)
    completed_slots = Column(Text, nullable=False, default="[]")
    succeeded = Column(Boolean, nullable=False, default=False)
    consecutive_failures = Column(Integer, nullable=False, default=0)
    fallback_done = Column(Boolean, nullable=False, default=False)
    period_states = Column(JSON, nullable=True)

    task = relationship("Task", back_populates="days")
    notices = relationship("TaskNotice", back_populates="day", cascade="all, delete-orphan")


class TaskNotice(Base):
    __tablename__ = "task_notices"
    __table_args__ = (UniqueConstraint("day_id", "kind"),)

    id = Column(Integer, primary_key=True)
    day_id = Column(Integer, ForeignKey("task_days.id"), nullable=False, index=True)
    kind = Column(String(32), nullable=False)
    message = Column(Text, nullable=False)
    wx_sent = Column(Boolean, nullable=False, default=False)
    qq_sent = Column(Boolean, nullable=False, default=False)
    delivered_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)

    day = relationship("TaskDay", back_populates="notices")


class TaskLog(Base):
    __tablename__ = "task_logs"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False, index=True)
    run_at = Column(DateTime, default=datetime.utcnow, index=True)
    status = Column(String(32), nullable=False)  # success / error / skipped
    message = Column(Text, nullable=False)  # 存储整段可读日志文本

    task = relationship("Task", back_populates="logs")


class Setting(Base):
    """
    存储全局配置，例如管理员 Key 哈希等。
    """

    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(100), unique=True, nullable=False)
    value = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

