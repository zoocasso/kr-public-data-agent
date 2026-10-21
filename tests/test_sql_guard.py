from pathlib import Path

import pytest
import yaml

from agent.sql_guard import SqlRejected, check_select

ROOT = Path(__file__).resolve().parent.parent
TABLES = ["keit_project_participants", "keit_projects", "ketep_project_orgs", "kimst_project_budgets"]
QUESTIONS = yaml.safe_load((ROOT / "eval" / "questions.yaml").read_text())["questions"]


def check(sql):
    return check_select(sql, TABLES)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select count(*) from keit_projects where support_year = 2024;",
        "WITH t AS (SELECT 1 AS a) SELECT * FROM t",
        "WITH t(a) AS (SELECT 1) SELECT * FROM t",
        "SELECT 1 UNION SELECT 2",
        "(SELECT 1)",
        "SELECT title FROM keit_projects WHERE title LIKE '%delete%'",
        "SELECT title FROM keit_projects WHERE title ~ 'pg_sleep'",
        "SELECT count(*) FROM public.keit_projects",
        'SELECT count(*) FROM "keit_projects"',
        "SELECT count(*) FROM KEIT_PROJECTS",
        "SELECT org_name, rank() OVER (ORDER BY count(*) DESC) FROM ketep_project_orgs GROUP BY 1",
        "SELECT string_agg(org_name, ', ' ORDER BY org_name) FROM ketep_project_orgs",
        "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY total_budget_krw) FROM kimst_project_budgets",
        "SELECT round(avg(end_date - start_date + 1) / 365.25, 2)::numeric FROM keit_projects",
        "SELECT extract(year FROM start_date), date_trunc('month', start_date), age(end_date, start_date) FROM keit_projects",
        "SELECT coalesce(nullif(trim(lead_org_code), ''), '없음'), upper(left(title, 3)) FROM keit_projects",
        "SELECT count(*) FILTER (WHERE field_detail = '미분류') FROM keit_projects",
        "SELECT count(DISTINCT (program_name, project_title)) FROM kimst_project_budgets",
        "SELECT CASE WHEN gov_grant_krw > 0 THEN 1 ELSE 0 END FROM kimst_project_budgets",
    ],
)
def test_allows_select(sql):
    assert check(sql) == sql.strip().rstrip(";").strip()


@pytest.mark.parametrize("q", [q for q in QUESTIONS if q.get("gold_sql")], ids=lambda q: q["id"])
def test_every_gold_query_passes(q):
    check(q["gold_sql"])


@pytest.mark.parametrize(
    "sql, reason",
    [
        ("DELETE FROM keit_projects", "SELECT 쿼리만"),
        ("UPDATE keit_projects SET title = 'x'", "SELECT 쿼리만"),
        ("DROP TABLE keit_projects", "SELECT 쿼리만"),
        ("SELECT 1; SELECT 2", "하나만"),
        ("SELECT 1; DELETE FROM keit_projects", "하나만"),
        ("SELECT * INTO t2 FROM keit_projects", "INTO"),
        ("WITH d AS (DELETE FROM keit_projects RETURNING *) SELECT * FROM d", "DELETE"),
        ("SELECT * FROM keit_projects FOR UPDATE", "LOCK"),
        ("", "빈 쿼리"),
    ],
)
def test_rejects_non_select(sql, reason):
    with pytest.raises(SqlRejected, match=reason):
        check(sql)


# side effects, waiting, server state and nested queries: none is on the allowlist
@pytest.mark.parametrize(
    "sql, name",
    [
        ("SELECT query_to_xml('select pg_sleep(4)', true, true, '')", "query_to_xml"),
        ("SELECT pg_sleep(10)", "pg_sleep"),
        ("SELECT pg_catalog.pg_sleep(10)", "pg_sleep"),
        ('SELECT "pg_sleep"(10)', "pg_sleep"),
        ("SELECT pg_try_advisory_lock(1)", "pg_try_advisory_lock"),
        ("SELECT pg_advisory_xact_lock(1)", "pg_advisory_xact_lock"),
        ("SELECT pg_advisory_lock(1)", "pg_advisory_lock"),
        ("SELECT pg_notify('ch', 'x')", "pg_notify"),
        ("SELECT current_setting('statement_timeout')", "current_setting"),
        ("SELECT set_config('statement_timeout', '0', false)", "set_config"),
        ("SELECT pg_read_file('/etc/passwd')", "pg_read_file"),
        ("SELECT dblink('host=x', 'select 1')", "dblink"),
        ("SELECT txid_current()", "txid_current"),
        ("SELECT version()", "version"),
        ("SELECT current_user", "current_user"),
        ("SELECT repeat('x', 100000000)", "repeat"),
        ("SELECT * FROM generate_series(1, 100000000)", "generate_series"),
        ("SELECT count(*) FROM keit_projects WHERE title = pg_sleep(1)::text", "pg_sleep"),
        ("SELECT * FROM keit_projects WHERE EXISTS (SELECT pg_notify('ch', 'x'))", "pg_notify"),
    ],
)
def test_rejects_functions_outside_the_allowlist(sql, name):
    with pytest.raises(SqlRejected, match=f"허용되지 않는 함수입니다: {name}"):
        check(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT rolname FROM pg_roles",
        "SELECT * FROM pg_catalog.pg_roles",
        "SELECT * FROM pg_stat_activity",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM other_schema.keit_projects",
        "SELECT * FROM somedb.public.keit_projects",
        'SELECT * FROM "KEIT_PROJECTS"',
        "SELECT * FROM keit_projects k JOIN pg_shadow s ON true",
        "SELECT title FROM keit_projects WHERE ministry IN (SELECT rolname FROM pg_roles)",
        "WITH t AS (SELECT * FROM pg_settings) SELECT * FROM t",
    ],
)
def test_rejects_tables_outside_the_public_data(sql):
    with pytest.raises(SqlRejected, match="조회할 수 없는 테이블"):
        check(sql)


def test_a_cte_name_does_not_open_a_schema():
    # an unqualified name may be a CTE, a qualified one never is
    with pytest.raises(SqlRejected, match="pg_catalog.pg_roles"):
        check("WITH pg_roles AS (SELECT 1) SELECT * FROM pg_catalog.pg_roles")


# a CTE named like a catalog only covers references inside the query that declares it
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT rolname FROM pg_roles WHERE EXISTS (WITH pg_roles AS (SELECT 1 AS x) SELECT x FROM pg_roles)",
        "SELECT (WITH pg_roles AS (SELECT 1) SELECT 1), rolname FROM pg_roles",
        "SELECT (SELECT count(*) FROM pg_roles), (WITH pg_roles AS (SELECT 1 AS x) SELECT x FROM pg_roles)",
        "SELECT rolname::text FROM pg_roles UNION ALL (WITH pg_roles AS (SELECT 'x' AS rolname) SELECT rolname FROM pg_roles)",
        "SELECT * FROM (WITH pg_roles AS (SELECT 1 AS x) SELECT * FROM pg_roles) s, pg_roles",
        # without RECURSIVE a CTE does not see the ones after it
        "WITH a AS (SELECT rolname FROM pg_roles), pg_roles AS (SELECT 1) SELECT * FROM a",
    ],
)
def test_a_cte_in_one_scope_does_not_cover_a_catalog_in_another(sql):
    with pytest.raises(SqlRejected, match="조회할 수 없는 테이블"):
        check(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "WITH a AS (SELECT 1 AS x), b AS (SELECT x FROM a) SELECT * FROM b",
        "WITH RECURSIVE n AS (SELECT 1 AS i UNION ALL SELECT i + 1 FROM n WHERE i < 3) SELECT * FROM n",
        "WITH t AS (SELECT 1 AS x) SELECT * FROM t UNION ALL SELECT * FROM t",
        "WITH t AS (SELECT 1 AS x) SELECT * FROM keit_projects WHERE EXISTS (SELECT 1 FROM t)",
        "SELECT * FROM (WITH t AS (SELECT 1 AS x) SELECT * FROM t) s",
        "WITH o AS (SELECT 1 AS x) SELECT (WITH i AS (SELECT x FROM o) SELECT x FROM i)",
    ],
)
def test_ctes_still_work_where_postgres_sees_them(sql):
    check(sql)


def test_a_long_query_is_rejected():
    with pytest.raises(SqlRejected, match="너무 깁니다"):
        check("SELECT 1" + " " * 4000 + ", 2")


@pytest.mark.parametrize(
    "sql, reason",
    [
        ("SELECT length(lpad('x', 250000000, 'y') || lpad('x', 250000000, 'y'))", "lpad·rpad 의 길이"),
        ("SELECT rpad(title, 1001) FROM keit_projects", "lpad·rpad 의 길이"),
        ("SELECT lpad(title, support_year * 100000) FROM keit_projects", "lpad·rpad 의 길이"),
        ("SELECT replace(title, ' ', repeat('x', 1000)) FROM keit_projects", "바꿀 문자열"),
        ("SELECT replace(title, ' ', title) FROM keit_projects", "바꿀 문자열"),
        ("SELECT replace(title, ' ', '" + "x" * 101 + "') FROM keit_projects", "바꿀 문자열"),
        ("SELECT regexp_replace(title, '', lead_org) FROM keit_projects", "바꿀 문자열"),
        # each level multiplies the length: 1,000 x's become 10^5, 10^7, 10^9 characters
        ("SELECT replace(replace(lpad('', 1000, 'x'), 'x', '" + "x" * 100 + "'), 'x', 'xx')", "안에 또"),
        ("SELECT lpad(replace(title, 'a', 'aaaa'), 10) FROM keit_projects", "안에 또"),
        ("SELECT regexp_replace(regexp_replace(title, '', 'ab', 'g'), '', 'ab', 'g') FROM keit_projects", "안에 또"),
    ],
)
def test_string_growth_is_bounded(sql, reason):
    with pytest.raises(SqlRejected, match=reason):
        check(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT lpad(lead_org_code, 10, '0'), rpad(title, 20) FROM keit_projects",
        "SELECT replace(replace(org_name, '(주)', ''), ' ', '') FROM ketep_project_orgs",  # shrinking replaces may nest
        "SELECT replace(org_name, '(주)', '주식회사') FROM ketep_project_orgs",
        "SELECT regexp_replace(regexp_replace(org_name, '\\s+', '', 'g'), '[()]', '', 'g') FROM ketep_project_orgs",
    ],
)
def test_ordinary_string_cleaning_still_passes(sql):
    check(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'pg_authid'::regclass",
        "SELECT CAST('app' AS regrole)",
        "SELECT 'pg_read_file(text)'::regprocedure",
        "SELECT regclass 'pg_authid'",
        "SELECT 'x'::pg_catalog.regclass",
        "SELECT 'int4'::regtype, 'public'::regnamespace",
        "SELECT '1'::oid",
        "SELECT '<a/>'::xml",
    ],
)
def test_casts_outside_the_allowlist_are_rejected(sql):
    with pytest.raises(SqlRejected, match="형 변환"):
        check(sql)


def test_ordinary_casts_still_pass():
    check(
        "SELECT support_year::text, start_date::timestamp, '1 day'::interval, avg(support_year)::numeric(10, 2), "
        "date '2024-01-01', CAST(project_no AS bigint), ARRAY[1, 2]::int[] FROM keit_projects GROUP BY 1, 2"
    )
