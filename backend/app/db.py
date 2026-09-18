from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str | None = None):
    url = url or settings.database_url
    kwargs = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, future=True, **kwargs)


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


def init_db(target_engine=None) -> list[str]:
    """Create missing tables and add missing columns. Aurora has no migration
    tool yet; columns are only ever added (nullable or with a default), so
    ALTER TABLE ... ADD COLUMN on SQLite and Postgres covers every schema change
    so far. Returns the statements it ran."""
    from sqlalchemy import inspect, text

    from app import models  # noqa: F401  (registers tables on Base)

    eng = target_engine or engine
    Base.metadata.create_all(eng)
    applied: list[str] = []
    insp = inspect(eng)
    with eng.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(dialect=eng.dialect)}"
                default = col.default.arg if col.default is not None and not callable(getattr(col.default, "arg", None)) else None
                if default is not None:
                    ddl += " DEFAULT " + (f"'{default}'" if isinstance(default, str) else str(default))
                elif not col.nullable and col.type.python_type in (int, float):
                    ddl += " DEFAULT 0"
                conn.execute(text(ddl))
                applied.append(ddl)
    return applied


def get_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
