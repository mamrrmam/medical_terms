"""Thin database layer so loaders run unchanged on SQLite and PostgreSQL.

Loaders write SQL with `?` placeholders; `Database.execute` rewrites them to `%s`
for psycopg. Upserts use `INSERT ... ON CONFLICT`, which both SQLite (>= 3.24) and
PostgreSQL support.
"""

import re
import sqlite3
from importlib import resources
from urllib.parse import urlparse


class Database:
    def __init__(self, url: str):
        self.url = url
        scheme = urlparse(url).scheme
        if scheme == "sqlite" or scheme == "":
            path = url[len("sqlite:///"):] if url.startswith("sqlite:///") else url
            self.conn = sqlite3.connect(path)
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.dialect = "sqlite"
        elif scheme in ("postgres", "postgresql"):
            import psycopg  # optional dependency: pip install medterms[postgres]

            self.conn = psycopg.connect(url)
            self.dialect = "postgresql"
        else:
            raise ValueError(f"unsupported database url: {url}")

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.dialect == "postgresql" else sql

    def execute(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(self._sql(sql), params)
        return cur

    def executemany(self, sql, rows):
        cur = self.conn.cursor()
        cur.executemany(self._sql(sql), rows)
        return cur

    def query(self, sql, params=()):
        return self.execute(sql, params).fetchall()

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()

    def table_exists(self, name: str) -> bool:
        if self.dialect == "sqlite":
            sql = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?"
        else:
            sql = "SELECT 1 FROM information_schema.tables WHERE table_name = ?"
        return bool(self.query(sql, (name,)))

    def init_schema(self):
        """Create the tables if they don't exist yet."""
        if self.table_exists("concept"):
            return
        script = resources.files("medterms").joinpath("schema.sql").read_text()
        if self.dialect == "sqlite":
            self.conn.executescript(script)
        else:
            # Strip comments so semicolons inside them don't split statements.
            script = re.sub(r"--[^\n]*", "", script)
            for stmt in script.split(";"):
                if stmt.strip():
                    self.execute(stmt)
        self.commit()

    def next_concept_id(self, floor: int = 1) -> int:
        (max_id,) = self.query("SELECT MAX(concept_id) FROM concept")[0]
        return max(floor, (max_id or 0) + 1)
