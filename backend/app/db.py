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
    from sqlalchemy import Text, inspect, text

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
        # Columns the model now declares as unbounded Text but the table still
        # holds as VARCHAR(n): widen in place (Postgres; SQLite ignores lengths).
        if eng.dialect.name == "postgresql":
            for table in Base.metadata.sorted_tables:
                db_cols = {c["name"]: c for c in insp.get_columns(table.name)}
                for col in table.columns:
                    dbc = db_cols.get(col.name)
                    if dbc is None or not isinstance(col.type, Text):
                        continue
                    if getattr(dbc["type"], "length", None):
                        ddl = f"ALTER TABLE {table.name} ALTER COLUMN {col.name} TYPE TEXT"
                        conn.execute(text(ddl))
                        applied.append(ddl)
            # promotions used to be unique on name alone; now unique on (name, start_date)
            for uc in insp.get_unique_constraints("promotions"):
                if uc["column_names"] == ["name"] and uc.get("name"):
                    ddl = f"ALTER TABLE promotions DROP CONSTRAINT IF EXISTS {uc['name']}"
                    conn.execute(text(ddl))
                    applied.append(ddl)
        ddl = "CREATE UNIQUE INDEX IF NOT EXISTS uq_promotions_name_start ON promotions (name, start_date)"
        conn.execute(text(ddl))
    return applied


def get_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
