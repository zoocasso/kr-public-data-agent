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


def test_db_role_blocks_writes_even_without_guard(db):
    import psycopg
    import pytest

    with pytest.raises(psycopg.Error):
        db.query("DELETE FROM keit_projects RETURNING 1")
    with pytest.raises(psycopg.errors.QueryCanceled):
        db.query("SELECT pg_sleep(6)")
