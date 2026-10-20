"""run_sql 에 들어온 SQL 이 읽기 전용 단일 SELECT 인지 검사한다.

DB 계정(agent_ro) 권한이 실제 방어선이고, 이 검사는 그 앞에서 위험한 요청을
일찍 거절하고 모델에게 이유를 알려 주는 역할이다. SQL 은 고치지 않고 원문 그대로
실행하므로, 답에 붙는 근거 쿼리는 실제로 실행된 쿼리와 같다.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

# 읽기 전용 트랜잭션에서도 부작용이 있거나 서버 파일·프로세스에 닿는 함수
FORBIDDEN_FUNCTIONS = {
    "pg_sleep",
    "pg_sleep_for",
    "pg_sleep_until",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_stat_file",
    "lo_import",
    "lo_export",
    "dblink",
    "dblink_exec",
    "set_config",
    "pg_terminate_backend",
    "pg_cancel_backend",
    "pg_reload_conf",
    "pg_advisory_lock",
    "txid_current",
}

_WRITE_NODES = tuple(
    t
    for t in (
        getattr(exp, name, None)
        for name in (
            "Insert",
            "Update",
            "Delete",
            "Merge",
            "Create",
            "Drop",
            "Alter",
            "TruncateTable",
            "Command",
            "Into",
            "Copy",
            "Set",
            "Transaction",
            "Commit",
            "Rollback",
            "Grant",
            "Lock",
        )
    )
    if t is not None
)


class SqlRejected(ValueError):
    """모델에게 그대로 보여 줄 거절 사유."""


def check_select(sql: str) -> str:
    """검사를 통과하면 끝의 세미콜론만 뗀 원문을 돌려준다."""
    text = sql.strip().rstrip(";").strip()
    if not text:
        raise SqlRejected("빈 쿼리입니다.")
    try:
        statements = [s for s in sqlglot.parse(text, read="postgres") if s is not None]
    except ParseError as e:
        raise SqlRejected(f"SQL 구문을 해석하지 못했습니다: {str(e).splitlines()[0]}") from e
    if len(statements) != 1:
        raise SqlRejected("쿼리는 한 번에 하나만 실행할 수 있습니다.")
    root = statements[0]
    if isinstance(root, exp.Subquery):
        root = root.unnest()
    if not isinstance(root, (exp.Select, exp.SetOperation)):
        raise SqlRejected("SELECT 쿼리만 실행할 수 있습니다 (WITH ... SELECT, UNION 포함).")
    for node in root.walk():
        if isinstance(node, _WRITE_NODES):
            raise SqlRejected(f"읽기 전용입니다. 허용되지 않는 구문: {node.key.upper()}")
        if isinstance(node, exp.Func):
            name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
            if name in FORBIDDEN_FUNCTIONS:
                raise SqlRejected(f"허용되지 않는 함수입니다: {name}")
    return text
