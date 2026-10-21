"""에이전트 도구: 정의(JSON 스키마)와 실행.

list_tables · describe_table · run_sql · search_docs 는 자료를 돌려주고,
submit_answer 는 최종 답을 구조화된 형태로 받는 종료 신호다.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field

import psycopg
from psycopg import sql as pgsql

from .db import MAX_ROWS, Database, cell
from .docs import DocIndex
from .sql_guard import SqlRejected, check_select

ENUM_MAX_DISTINCT = 25  # 고유값이 이 이하인 문자 칼럼은 모든 값을 보여 준다
TOP_VALUES = 5
DOC_CHARS = 1200


@dataclass
class ToolOutcome:
    text: str
    is_error: bool = False
    meta: dict = field(default_factory=dict)


def tool_definitions(table_names: list[str]) -> list[dict]:
    """도구 목록. 캐시 접두사가 흔들리지 않도록 순서·내용을 결정적으로 만든다."""
    return [
        {
            "name": "list_tables",
            "description": (
                "DB에 있는 테이블 목록과 각 테이블 설명(출처, 1행이 뜻하는 것, 기간), 행 수를 돌려준다. "
                "질문을 받으면 먼저 호출해서 어떤 데이터로 답할 수 있는지 확인한다."
            ),
            "strict": True,
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "describe_table",
            "description": (
                "테이블의 칼럼(이름, 타입, 설명)과 값 분포를 보여 준다. 고유값이 적은 문자 칼럼은 모든 값과 건수, "
                "고유값이 많은 문자 칼럼은 자주 나오는 값, 숫자·날짜 칼럼은 최솟값·최댓값을 보여 주고 예시 행 3개를 붙인다. "
                "WHERE 조건에 쓸 값(부처명, 유형, '미분류' 같은 표기)은 추측하지 말고 여기서 확인한다."
            ),
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": {"table": {"type": "string", "enum": sorted(table_names)}},
                "required": ["table"],
                "additionalProperties": False,
            },
        },
        {
            "name": "run_sql",
            "description": (
                f"PostgreSQL SELECT 쿼리 하나를 읽기 전용 계정으로 실행하고 결과를 최대 {MAX_ROWS}행까지 돌려준다. "
                "list_tables 의 테이블만 읽을 수 있고, 함수는 집계·윈도·조건·숫자·문자열·날짜·형 변환 함수만 쓸 수 있다 "
                "(lpad·rpad 길이 1000 이하, replace 의 바꿀 문자열 100자 이하, 쿼리 4,000자 이하). "
                "쓰기·DDL·여러 문장은 거부되고 5초가 넘으면 취소된다. 오류가 나면 메시지를 보고 고쳐서 다시 실행한다. "
                "행이 많을 결과는 GROUP BY·집계로 줄여서 요청한다."
            ),
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "실행할 SELECT 쿼리 (PostgreSQL 문법)"},
                    "purpose": {"type": "string", "description": "이 쿼리로 확인하려는 것, 한 줄"},
                },
                "required": ["sql", "purpose"],
                "additionalProperties": False,
            },
        },
        {
            "name": "search_docs",
            "description": (
                "데이터 설명서·데이터 사전·공공데이터포털 데이터셋 설명에서 관련 구절을 찾는다. "
                "용어 정의, 칼럼이 만들어진 방식, 코드의 의미, 데이터 범위·갱신 중단 사유처럼 표에 없는 정보를 찾을 때 쓴다."
            ),
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "찾을 내용 (한국어 키워드 위주)"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "name": "submit_answer",
            "description": (
                "최종 답을 제출한다. 답을 정했으면 반드시 이 도구로 끝낸다. "
                "evidence_sql 은 이번 대화에서 run_sql 로 실제 실행해 성공한 쿼리를 그대로 넣는다. "
                "거절이 아니면 evidence_sql 이나 sources(search_docs 로 찾은 문서) 중 하나는 있어야 하고, "
                "실행하지 않은 쿼리나 찾지 않은 문서를 넣으면 제출이 거부된다."
            ),
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": {
                    "answer": {"type": "string", "description": "사용자에게 보여 줄 한국어 답"},
                    "values": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "핵심 값. 숫자는 쉼표·단위 없이(원 단위), 이름은 데이터 표기 그대로, 순위는 순서대로",
                    },
                    "evidence_sql": {
                        "type": "string",
                        "description": "답의 핵심 값을 직접 보여 주는 실행된 쿼리. 거절하거나 문서만으로 답했으면 빈 문자열",
                    },
                    "sources": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "search_docs 로 참고한 문서 경로. 없으면 빈 배열",
                    },
                    "refused": {"type": "boolean", "description": "데이터로 답할 수 없어 거절하면 true"},
                },
                "required": ["answer", "values", "evidence_sql", "sources", "refused"],
                "additionalProperties": False,
            },
        },
    ]


class Toolbox:
    def __init__(self, db: Database, docs: DocIndex | None = None):
        self.db = db
        self.docs = docs or DocIndex()
        self.tables = [
            r[0]
            for r in db.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY 1"
            )
        ]

    def definitions(self) -> list[dict]:
        return tool_definitions(self.tables)

    def run(self, name: str, args: dict) -> ToolOutcome:
        handler = {
            "list_tables": self.list_tables,
            "describe_table": self.describe_table,
            "run_sql": self.run_sql,
            "search_docs": self.search_docs,
        }.get(name)
        if handler is None:
            return ToolOutcome(f"알 수 없는 도구: {name}", is_error=True)
        try:
            inspect.signature(handler).bind(**args)
        except TypeError as e:
            return ToolOutcome(f"도구 입력 오류: {e}", is_error=True)
        # describe_table·list_tables 의 DB 오류(시간 초과, 끊긴 연결)도 run_sql 처럼 모델에게 도구 결과로 돌려준다.
        # 그 밖의 예외는 하네스 버그라서 run_agent 가 실행 기록과 함께 harness_error 로 남긴다.
        try:
            return handler(**args)
        except psycopg.Error as e:
            return db_error_outcome(e)

    # ---------------------------------------------------------------- list_tables
    def list_tables(self) -> ToolOutcome:
        lines = []
        for t in self.tables:
            comment = self.db.fetch("SELECT obj_description(%s::regclass, 'pg_class')", (t,))[0][0]
            n = self.db.fetch(pgsql.SQL("SELECT count(*) FROM {}").format(pgsql.Identifier(t)))[0][0]
            lines.append(f"- {t} ({n:,}행): {comment or ''}")
        return ToolOutcome("\n".join(lines))

    # ---------------------------------------------------------------- describe_table
    def describe_table(self, table: str) -> ToolOutcome:
        if table not in self.tables:
            return ToolOutcome(f"없는 테이블: {table}. 있는 테이블: {', '.join(self.tables)}", is_error=True)
        ident = pgsql.Identifier(table)
        comment = self.db.fetch("SELECT obj_description(%s::regclass, 'pg_class')", (table,))[0][0]
        cols = self.db.fetch(
            """
            SELECT a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull,
                   col_description(a.attrelid, a.attnum)
            FROM pg_attribute a
            WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY a.attnum
            """,
            (table,),
        )
        total = self.db.fetch(pgsql.SQL("SELECT count(*) FROM {}").format(ident))[0][0]
        out = [f"# {table} ({total:,}행)", comment or "", "", "## 칼럼"]
        for name, typ, nullable, col_comment in cols:
            c = pgsql.Identifier(name)
            nulls, distinct = self.db.fetch(
                pgsql.SQL("SELECT count(*) FILTER (WHERE {c} IS NULL), count(DISTINCT {c}) FROM {t}").format(c=c, t=ident)
            )[0]
            head = f"- {name} {typ}{'' if not nullable else ' NULL허용'}: {col_comment or ''}"
            stats = [f"고유값 {distinct:,}"] + ([f"NULL {nulls:,}"] if nulls else [])
            if typ in ("text",) or typ.startswith("character"):
                limit = ENUM_MAX_DISTINCT if distinct <= ENUM_MAX_DISTINCT else TOP_VALUES
                top = self.db.fetch(
                    pgsql.SQL(
                        "SELECT {c}, count(*) FROM {t} WHERE {c} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT %s"
                    ).format(c=c, t=ident),
                    (limit,),
                )
                label = "전체 값" if distinct <= ENUM_MAX_DISTINCT else "자주 나오는 값"
                stats.append(f"{label}: " + " · ".join(f"{cell(v)}({n:,})" for v, n in top))
            elif typ not in ("boolean",):
                lo, hi = self.db.fetch(pgsql.SQL("SELECT min({c}), max({c}) FROM {t}").format(c=c, t=ident))[0]
                stats.append(f"범위 {cell(lo)} ~ {cell(hi)}")
            out.append(head)
            out.append("    " + " | ".join(stats))
        sample = self.db.query(pgsql.SQL("SELECT * FROM {} ORDER BY 1 LIMIT 3").format(ident).as_string(self.db.conn))
        out += ["", "## 예시 행", " | ".join(sample.columns)]
        out += [" | ".join(cell(v) for v in row) for row in sample.rows]
        return ToolOutcome("\n".join(out))

    # ---------------------------------------------------------------- run_sql
    def run_sql(self, sql: str, purpose: str = "") -> ToolOutcome:
        try:
            text = check_select(sql, self.tables)
        except SqlRejected as e:
            return ToolOutcome(f"실행 거부: {e}", is_error=True, meta={"rejected": True})
        try:
            res = self.db.query(text)
        except psycopg.Error as e:
            return db_error_outcome(e)
        lines = [" | ".join(res.columns)]
        lines += [" | ".join(cell(v) for v in row) for row in res.rows]
        n = f"{len(res.rows)}행" + (f" (앞 {MAX_ROWS}행만 표시, 실제로는 더 있음)" if res.truncated else "")
        return ToolOutcome(
            f"{n}, {res.elapsed_ms}ms\n" + "\n".join(lines),
            meta={
                "rows": len(res.rows),
                "truncated": res.truncated,
                "ms": res.elapsed_ms,
                "result": [[cell(v) for v in row] for row in res.rows[:50]],
            },
        )

    # ---------------------------------------------------------------- search_docs
    def search_docs(self, query: str) -> ToolOutcome:
        hits = self.docs.search(query)
        if not hits:
            return ToolOutcome("관련 문서를 찾지 못했습니다.")
        parts = []
        for score, ch in hits:
            text = ch.text if len(ch.text) <= DOC_CHARS else ch.text[:DOC_CHARS] + "…"
            parts.append(f"[{ch.source} § {ch.heading}] (점수 {score:.1f})\n{text}")
        return ToolOutcome("\n\n---\n\n".join(parts), meta={"sources": sorted({c.source for _, c in hits})})


def db_error_outcome(e: psycopg.Error) -> ToolOutcome:
    if isinstance(e, psycopg.errors.QueryCanceled):
        return ToolOutcome(
            "시간 초과(5초)로 취소됨. 조건을 좁히거나 집계 방식을 바꾸세요.", is_error=True, meta={"db_error": "timeout"}
        )
    msg = (e.diag.message_primary or str(e)).strip()
    hint = f" (힌트: {e.diag.message_hint})" if e.diag.message_hint else ""
    return ToolOutcome(f"DB 오류: {msg}{hint}", is_error=True, meta={"db_error": msg})


def as_tool_result(tool_use_id: str, outcome: ToolOutcome) -> dict:
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": outcome.text}
    if outcome.is_error:
        block["is_error"] = True
    return block
