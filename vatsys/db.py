"""Engine and session setup."""

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from . import config
from .models import Base


class Database:
    def __init__(self, url: str | None = None):
        url = url or config.DATABASE_URL
        if url.startswith("sqlite:///"):
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, connect_args=connect_args)
        self.sessionmaker = sessionmaker(self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        from .seed import seed_defaults

        Base.metadata.create_all(self.engine)
        self._add_missing_columns()
        with self.session() as session:
            seed_defaults(session)
            session.commit()

    def _add_missing_columns(self) -> None:
        """Add nullable columns introduced after a database was created (no migration tool yet)."""
        existing = inspect(self.engine)
        with self.engine.begin() as conn:
            for table in Base.metadata.sorted_tables:
                have = {c["name"] for c in existing.get_columns(table.name)}
                for col in table.columns:
                    if col.name not in have and col.nullable:
                        ddl = col.type.compile(dialect=self.engine.dialect)
                        conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {ddl}'))

    def session(self) -> Session:
        return self.sessionmaker()
