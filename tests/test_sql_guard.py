import pytest

from agent.sql_guard import SqlRejected, check_select


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select count(*) from keit_projects where support_year = 2024;",
        "WITH t AS (SELECT 1 AS a) SELECT * FROM t",
        "SELECT 1 UNION SELECT 2",
        "(SELECT 1)",
        "SELECT title FROM keit_projects WHERE title LIKE '%delete%'",
    ],
)
def test_allows_select(sql):
    assert check_select(sql) == sql.strip().rstrip(";").strip()


@pytest.mark.parametrize(
    "sql, reason",
    [
        ("DELETE FROM keit_projects", "SELECT 쿼리만"),
        ("UPDATE keit_projects SET title = 'x'", "SELECT 쿼리만"),
        ("DROP TABLE keit_projects", "SELECT 쿼리만"),
        ("SELECT 1; SELECT 2", "하나만"),
        ("SELECT 1; DELETE FROM keit_projects", "하나만"),
        ("SELECT * INTO t2 FROM keit_projects", "INTO"),
        ("SELECT pg_sleep(10)", "pg_sleep"),
        ("SELECT pg_read_file('/etc/passwd')", "pg_read_file"),
        ("WITH d AS (DELETE FROM keit_projects RETURNING *) SELECT * FROM d", "DELETE"),
        ("SELECT * FROM keit_projects FOR UPDATE", "LOCK"),
        ("", "빈 쿼리"),
    ],
)
def test_rejects(sql, reason):
    with pytest.raises(SqlRejected, match=reason):
        check_select(sql)
