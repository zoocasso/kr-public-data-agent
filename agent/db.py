"""에이전트 전용 읽기 전용 DB 접근.

agent_ro 계정은 SELECT 권한만 있고 default_transaction_read_only=on,
statement_timeout=5s 가 걸려 있다 (db/init/02_roles.sh).
결과는 서버 쪽 커서로 받아서 max_rows+1 행까지만 가져온다.
"""

from __future__ import annotations

import datetime as dt
import os
import time
from dataclasses import dataclass
from decimal import Decimal

import psycopg
from psycopg import sql as pgsql

MAX_ROWS = 200
MAX_CELL_CHARS = 200


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    truncated: bool
    elapsed_ms: int


def cell(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, Decimal):
        return format(v.normalize(), "f") if v == v.to_integral_value() else str(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    s = str(v).replace("\n", " ")
    return s if len(s) <= MAX_CELL_CHARS else s[:MAX_CELL_CHARS] + "…"


class Database:
    def __init__(self, url: str | None = None):
        self.conn = psycopg.connect(url or os.environ["AGENT_DATABASE_URL"], autocommit=False)

    def close(self) -> None:
        self.conn.close()

    def query(self, text: str, max_rows: int = MAX_ROWS) -> QueryResult:
        """서버 쪽 커서(DECLARE ... CURSOR)로 실행: SELECT/VALUES 외에는 PostgreSQL 이 거부한다."""
        start = time.perf_counter()
        try:
            with self.conn.cursor(name="agent_q") as cur:
                cur.execute(text)
                rows = cur.fetchmany(max_rows + 1)
                columns = [d.name for d in cur.description or []]
        finally:
            self.conn.rollback()
        return QueryResult(
            columns=columns,
            rows=rows[:max_rows],
            truncated=len(rows) > max_rows,
            elapsed_ms=round((time.perf_counter() - start) * 1000),
        )

    def fetch(self, query: pgsql.Composable | str, params: tuple = ()) -> list[tuple]:
        """도구 내부용 메타데이터 조회 (사용자 SQL 아님)."""
        try:
            with self.conn.cursor() as cur:
                cur.execute(query, params)
                return cur.fetchall()
        finally:
            self.conn.rollback()
