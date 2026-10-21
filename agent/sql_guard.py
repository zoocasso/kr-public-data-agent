"""run_sql 에 들어온 SQL 이 공개 데이터 테이블만 읽는 단일 SELECT 인지 검사한다.

허용 목록 방식이다. 함수는 아래 목록(집계·윈도·조건·숫자·문자열·날짜·형 변환)만, 테이블은
호출한 쪽이 넘긴 공개 데이터 테이블과, 그 자리에서 보이는 CTE 만 통과한다. 위험한 함수를 하나씩 막는
금지 목록은 query_to_xml('select pg_sleep(4)', ...) 처럼 문자열 안에 쿼리를 넣거나 목록에 없는
함수(pg_try_advisory_lock, pg_notify, current_setting)를 쓰면 뚫리고, pg_roles 같은 시스템
카탈로그도 막지 못한다.

DB 계정(agent_ro)의 SELECT 전용 권한·읽기 전용 트랜잭션·5초 제한이 마지막 방어선이고, 이 검사는
그 앞에서 요청을 일찍 거절하고 모델에게 이유를 알려 준다. SQL 은 고치지 않고 원문 그대로
실행하므로, 답에 붙는 근거 쿼리는 실제로 실행된 쿼리와 같다.
"""

from __future__ import annotations

from collections.abc import Iterable

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

# sqlglot 이 자기 표현식 클래스로 읽는 함수. 주석은 그 클래스로 읽히는 PostgreSQL 이름.
# 클래스로 확인하므로 sqlglot 이 이름을 어떻게 바꿔 부르든 통과하는 것은 이 목록뿐이다.
ALLOWED_FUNCTION_TYPES: frozenset[type[exp.Func]] = frozenset(
    {
        # 집계: count sum avg min max stddev(_pop/_samp) variance var_pop var_samp string_agg array_agg
        #       bool_and bool_or percentile_cont percentile_disc mode corr
        exp.Count,
        exp.Sum,
        exp.Avg,
        exp.Min,
        exp.Max,
        exp.Stddev,
        exp.StddevPop,
        exp.StddevSamp,
        exp.Variance,
        exp.VariancePop,
        exp.GroupConcat,
        exp.ArrayAgg,
        exp.LogicalAnd,
        exp.LogicalOr,
        exp.PercentileCont,
        exp.PercentileDisc,
        exp.Mode,
        exp.Corr,
        # 윈도: row_number rank dense_rank percent_rank cume_dist ntile lag lead first_value last_value nth_value
        exp.RowNumber,
        exp.Rank,
        exp.DenseRank,
        exp.PercentRank,
        exp.CumeDist,
        exp.Ntile,
        exp.Lag,
        exp.Lead,
        exp.FirstValue,
        exp.LastValue,
        exp.NthValue,
        # 조건·형 변환: CASE coalesce nullif greatest least CAST/:: EXISTS
        exp.Case,
        exp.If,
        exp.Coalesce,
        exp.Nullif,
        exp.Greatest,
        exp.Least,
        exp.Cast,
        exp.Exists,
        # 숫자: abs round trunc floor ceil sign power sqrt ln log exp width_bucket
        exp.Abs,
        exp.Round,
        exp.Trunc,
        exp.Floor,
        exp.Ceil,
        exp.Sign,
        exp.Pow,
        exp.Sqrt,
        exp.Ln,
        exp.Log,
        exp.Exp,
        exp.WidthBucket,
        # 문자열: length lower upper initcap trim/btrim/ltrim/rtrim lpad rpad substring/substr left right
        #         position/strpos replace translate concat concat_ws split_part regexp_replace starts_with
        #         reverse string_to_array array_length ARRAY[...]
        exp.Length,
        exp.Lower,
        exp.Upper,
        exp.Initcap,
        exp.Trim,
        exp.Pad,
        exp.Substring,
        exp.Left,
        exp.Right,
        exp.StrPosition,
        exp.Replace,
        exp.Translate,
        exp.Concat,
        exp.ConcatWs,
        exp.SplitPart,
        exp.RegexpReplace,
        exp.StartsWith,
        exp.Reverse,
        exp.StringToArray,
        exp.ArraySize,
        exp.Array,
        # 날짜: extract/date_part date_trunc to_char to_date current_date
        exp.Extract,
        exp.TimestampTrunc,
        exp.TimeToStr,
        exp.StrToDate,
        exp.CurrentDate,
        # sqlglot 에서는 함수인 연산자: AND OR ~ ~*
        exp.And,
        exp.Or,
        exp.RegexpLike,
        exp.RegexpILike,
    }
)

# sqlglot 이 모르는 함수(exp.Anonymous)는 PostgreSQL 이름으로 확인한다
ALLOWED_FUNCTION_NAMES = frozenset({"age", "make_date", "every", "regexp_match", "cardinality", "octet_length"})

# CAST·:: 로 바꿀 수 있는 타입. regclass·regrole·regprocedure 같은 reg* 타입은 이름으로 카탈로그를 조회하고
# (없는 이름이면 오류가 나서 역할 이름을 알아내는 데 쓸 수 있다), 그 밖의 타입은 분석에 필요 없다.
_T = exp.DataType.Type
ALLOWED_CAST_TYPES = frozenset(
    {
        _T.SMALLINT,
        _T.INT,
        _T.BIGINT,
        _T.DECIMAL,
        _T.DOUBLE,
        _T.FLOAT,
        _T.TEXT,
        _T.VARCHAR,
        _T.CHAR,
        _T.BOOLEAN,
        _T.DATE,
        _T.TIME,
        _T.TIMESTAMP,
        _T.TIMESTAMPTZ,
        _T.INTERVAL,
    }
)

# 문자열을 키우는 함수의 상한. lpad('x', 250000000) 세 개를 이으면 한 쿼리가 DB 메모리를 수 GB 쓰고,
# 문자열을 만드는 동안은 취소를 확인하지 않아 statement_timeout(5초)도 넘긴다.
MAX_SQL_CHARS = 4000
MAX_PAD_LENGTH = 1000
MAX_REPLACEMENT_CHARS = 100

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


def _ident(node: exp.Identifier) -> str:
    """PostgreSQL 이 보는 이름: 따옴표가 없으면 소문자로 접힌다."""
    return node.this if node.quoted else node.this.lower()


def _check_function(node: exp.Func) -> None:
    if isinstance(node, (exp.Anonymous, exp.AnonymousAggFunc)):
        name = str(node.name).lower()
        allowed = name in ALLOWED_FUNCTION_NAMES
    else:
        # sqlglot 의 이름은 PostgreSQL 이름과 다를 수 있어서(version() → CURRENT_VERSION) PostgreSQL 표기로 보여 준다
        name = node.sql(dialect="postgres").split("(", 1)[0].strip().lower()[:40] or node.sql_name().lower()
        allowed = type(node) in ALLOWED_FUNCTION_TYPES
    if allowed:
        return
    raise SqlRejected(f"허용되지 않는 함수입니다: {name}. 집계·윈도·조건·숫자·문자열·날짜·형 변환 함수만 쓸 수 있습니다.")


def _with_of(node: exp.Expression) -> exp.With | None:
    w = node.args.get("with_") or node.args.get("with")  # sqlglot 버전에 따라 키 이름이 다르다
    return w if isinstance(w, exp.With) else None


def _cte_name(cte: exp.CTE) -> str:
    return _ident(cte.args["alias"].this)


def _visible_ctes(node: exp.Table) -> set[str]:
    """이 테이블 참조 자리에서 PostgreSQL 이 CTE 로 읽는 이름들.

    CTE 는 자기를 둘러싼 쿼리 안에서만 보인다. 같은 WITH 안의 CTE 정의에서는 앞에 정의된 CTE 만 보이고,
    WITH RECURSIVE 면 전부(자기 자신 포함) 보인다. 쿼리 전체에서 CTE 이름을 모으면 다른 범위에 선언한
    pg_roles 라는 CTE 가 실제 pg_roles 카탈로그 참조를 통과시킨다.
    """
    names: set[str] = set()
    child, parent = node, node.parent
    while parent is not None:
        if isinstance(parent, exp.With):
            ctes = list(parent.expressions)
            position = next(i for i, c in enumerate(ctes) if c is child)
            visible = ctes if parent.args.get("recursive") else ctes[:position]
            names.update(_cte_name(c) for c in visible)
        else:
            with_ = _with_of(parent)
            if with_ is not None and with_ is not child:
                names.update(_cte_name(c) for c in with_.expressions)
        child, parent = parent, parent.parent
    return names


def _check_table(node: exp.Table, tables: set[str]) -> None:
    if not isinstance(node.this, exp.Identifier):
        return  # FROM generate_series(...) 같은 함수 호출: 함수 검사가 맡는다
    name = _ident(node.this)
    db = node.args.get("db")
    schema_ok = not node.catalog and (db is None or _ident(db) == "public")
    if schema_ok and (name in tables or (db is None and name in _visible_ctes(node))):
        return
    raise SqlRejected(
        f"조회할 수 없는 테이블입니다: {node.sql(dialect='postgres')}. 쓸 수 있는 테이블: {', '.join(sorted(tables))}"
    )


def _check_cast(node: exp.Cast) -> None:
    to = node.to
    targets = list(to.expressions) if to.this == _T.ARRAY else [to]
    if all(isinstance(t, exp.DataType) and t.this in ALLOWED_CAST_TYPES for t in targets):
        return
    target = to.sql(dialect="postgres").lower()
    raise SqlRejected(f"허용되지 않는 형 변환입니다: {target}. 숫자·문자·날짜·시간·불리언 타입으로만 바꿀 수 있습니다.")


def _grows(node: exp.Expression) -> bool:
    """결과가 입력보다 길어질 수 있는 문자열 함수 호출인가."""
    if isinstance(node, exp.Pad):
        return True
    if isinstance(node, exp.Replace):
        pattern, replacement = node.args.get("expression"), node.args.get("replacement")
        literals = isinstance(pattern, exp.Literal) and isinstance(replacement, exp.Literal)
        return not (literals and len(replacement.name) <= len(pattern.name))
    if isinstance(node, exp.RegexpReplace):
        # 정규식은 빈 문자열에도 맞을 수 있어서, 빈 문자열로 바꾸는 게 아니면 길어질 수 있다고 본다
        replacement = node.args.get("replacement")
        return not (isinstance(replacement, exp.Literal) and replacement.name == "")
    return False


def _check_growth(node: exp.Expression) -> None:
    """문자열을 키우는 함수의 길이 인자를 상수 상한 안으로 묶고, 그런 함수끼리 겹치지 못하게 한다."""
    if isinstance(node, exp.Pad):
        length = node.args.get("expression")
        if not (isinstance(length, exp.Literal) and length.is_int and int(length.name) <= MAX_PAD_LENGTH):
            raise SqlRejected(f"lpad·rpad 의 길이는 {MAX_PAD_LENGTH} 이하의 정수 상수여야 합니다.")
    elif isinstance(node, (exp.Replace, exp.RegexpReplace)):
        replacement = node.args.get("replacement")
        if not (
            isinstance(replacement, exp.Literal) and replacement.is_string and len(replacement.name) <= MAX_REPLACEMENT_CHARS
        ):
            raise SqlRejected(f"replace·regexp_replace 의 바꿀 문자열은 {MAX_REPLACEMENT_CHARS}자 이하의 문자열 상수여야 합니다.")
    if _grows(node) and any(_grows(inner) for inner in node.this.walk()):
        raise SqlRejected("문자열을 늘리는 함수(lpad·rpad·replace·regexp_replace) 안에 또 그런 함수를 넣을 수 없습니다.")


def check_select(sql: str, tables: Iterable[str]) -> str:
    """검사를 통과하면 끝의 세미콜론만 뗀 원문을 돌려준다. tables: 읽어도 되는 public 테이블."""
    text = sql.strip().rstrip(";").strip()
    if len(text) > MAX_SQL_CHARS:
        raise SqlRejected(f"쿼리가 너무 깁니다 ({len(text):,}자). {MAX_SQL_CHARS:,}자 이하로 줄여 주세요.")
    if not text:
        raise SqlRejected("빈 쿼리입니다.")
    try:
        statements = [s for s in sqlglot.parse(text, read="postgres") if s is not None]
    except SqlglotError as e:  # ParseError, and TokenError for an unterminated quote
        raise SqlRejected(f"SQL 구문을 해석하지 못했습니다: {str(e).splitlines()[0]}") from e
    if len(statements) != 1:
        raise SqlRejected("쿼리는 한 번에 하나만 실행할 수 있습니다.")
    root = statements[0]
    if isinstance(root, exp.Subquery):
        root = root.unnest()
    if not isinstance(root, (exp.Select, exp.SetOperation)):
        raise SqlRejected("SELECT 쿼리만 실행할 수 있습니다 (WITH ... SELECT, UNION 포함).")
    allowed = set(tables)
    for node in root.walk():
        if isinstance(node, _WRITE_NODES):
            raise SqlRejected(f"읽기 전용입니다. 허용되지 않는 구문: {node.key.upper()}")
        if isinstance(node, exp.Cast):
            _check_cast(node)
        if isinstance(node, (exp.Pad, exp.Replace, exp.RegexpReplace)):
            _check_growth(node)
        if isinstance(node, exp.Func):
            _check_function(node)
        elif isinstance(node, exp.Table):
            _check_table(node, allowed)
    return text
