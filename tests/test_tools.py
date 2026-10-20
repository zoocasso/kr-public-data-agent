import os

import psycopg
import pytest


def test_list_tables_shows_row_meaning(toolbox):
    out = toolbox.list_tables().text
    assert "keit_projects (11,900행)" in out
    assert "1행 = 과제×수행기관" in out


def test_describe_table_exposes_placeholder_values(toolbox):
    out = toolbox.describe_table("keit_projects").text
    assert "미분류(3,649)" in out  # 결측이 NULL 이 아니라 문자열이라는 걸 모델이 볼 수 있어야 함
    assert "소방청(58)" in out


def test_describe_unknown_table_is_error(toolbox):
    assert toolbox.describe_table("pg_shadow").is_error


def test_run_sql_returns_rows_and_caps(toolbox):
    ok = toolbox.run_sql("SELECT count(*) FROM keit_projects WHERE support_year = 2024", "t")
    assert not ok.is_error and ok.text.splitlines()[-1] == "1569"
    big = toolbox.run_sql("SELECT * FROM keit_project_participants", "t")
    assert big.meta["truncated"] and big.meta["rows"] == 200


def test_run_sql_errors_are_reported_not_raised(toolbox):
    out = toolbox.run_sql("SELECT nope FROM keit_projects", "t")
    assert out.is_error and "nope" in out.text
    # 오류 뒤에도 연결이 정상이어야 한다
    assert not toolbox.run_sql("SELECT 1", "t").is_error


def test_server_side_cursor_only_takes_select(db):
    # DECLARE CURSOR rejects a DELETE before the role's privileges are even checked; see the next test for those
    with pytest.raises(psycopg.errors.SyntaxError):
        db.query("DELETE FROM keit_projects RETURNING 1")
    with pytest.raises(psycopg.errors.QueryCanceled):
        db.query("SELECT pg_sleep(6)")


@pytest.fixture
def agent_conn(db):
    """A plain agent_ro connection: no guard, no cursor, only the role's own settings and privileges."""
    with psycopg.connect(os.environ["AGENT_DATABASE_URL"]) as conn:
        yield conn


def test_agent_role_starts_read_only(agent_conn):
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        agent_conn.execute("DELETE FROM keit_projects WHERE false")


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM keit_projects WHERE false",
        "UPDATE keit_projects SET title = title WHERE false",
        "INSERT INTO keit_project_participants VALUES (0, 'x')",
        "TRUNCATE kimst_project_budgets",
        "CREATE TABLE scratch (a int)",
    ],
)
def test_agent_role_cannot_write_even_in_a_read_write_transaction(agent_conn, sql):
    # default_transaction_read_only is only a default the session may change; the grants are what stop the write
    agent_conn.execute("SET TRANSACTION READ WRITE")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        agent_conn.execute(sql)


def test_agent_role_has_a_statement_timeout(agent_conn):
    assert agent_conn.execute("SHOW statement_timeout").fetchone()[0] == "5s"


def test_run_sql_rejects_tables_outside_the_data(toolbox):
    out = toolbox.run_sql("SELECT rolname FROM pg_roles", "t")
    assert out.is_error and out.meta.get("rejected")
