from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

DB_PATH = Path(__file__).resolve().parent.parent / "db" / "web_signin.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


class Base(DeclarativeBase):
    pass


def get_engine():
    url = f"sqlite:///{DB_PATH}"
    engine = create_engine(url, echo=False, connect_args={"check_same_thread": False})
    return engine


def get_session_factory(engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    """
    初始化数据库引擎和会话工厂，并创建所有表。
    具体表结构在 models.py 中定义。
    """
    from . import models  # noqa: F401  # 导入以注册模型

    engine = get_engine()
    models.Base.metadata.create_all(bind=engine)
    # create_all does not add columns to an existing SQLite database.
    columns = {column["name"] for column in inspect(engine).get_columns("tasks")}
    additions = {
        "random_enabled": "BOOLEAN NOT NULL DEFAULT 0",
        "random_minutes": "INTEGER NOT NULL DEFAULT 5",
        "fallback_time": "VARCHAR(8)",
        "periods": "JSON",
    }
    day_columns = {column["name"] for column in inspect(engine).get_columns("task_days")}
    with engine.begin() as connection:
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(text(f"ALTER TABLE tasks ADD COLUMN {name} {definition}"))
        if "period_states" not in day_columns:
            connection.execute(text("ALTER TABLE task_days ADD COLUMN period_states JSON"))
    SessionLocal = get_session_factory(engine)
    return engine, SessionLocal

