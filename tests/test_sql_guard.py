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
