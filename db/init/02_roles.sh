#!/bin/sh
# 에이전트 전용 읽기 전용 계정. run_sql 도구는 이 계정으로만 접속한다.
# 앱 코드의 SQL 검사와 별개로, DB 권한 자체로 쓰기를 막는 두 번째 방어선.
set -eu

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v agent_pw="${AGENT_DB_PASSWORD:?AGENT_DB_PASSWORD is required}" <<'SQL'
CREATE ROLE agent_ro LOGIN PASSWORD :'agent_pw';
GRANT CONNECT ON DATABASE :"DBNAME" TO agent_ro;
-- PUBLIC 은 기본으로 임시 테이블을 만들 수 있다. 읽기·쓰기 트랜잭션으로 바꾼 agent_ro 가 쓸 곳이 없게 회수한다.
REVOKE TEMPORARY ON DATABASE :"DBNAME" FROM PUBLIC;
-- 대용량 객체(large object)는 테이블 권한과 상관없이 PUBLIC 이 만들고 쓸 수 있다. 이것도 회수한다.
REVOKE EXECUTE ON FUNCTION lo_creat(integer), lo_create(oid), lo_from_bytea(oid, bytea), lo_import(text),
    lo_import(text, oid), lo_put(oid, bigint, bytea), lo_unlink(oid) FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO agent_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO agent_ro;
ALTER ROLE agent_ro SET default_transaction_read_only = on;
ALTER ROLE agent_ro SET statement_timeout = '5s';
ALTER ROLE agent_ro SET idle_in_transaction_session_timeout = '10s';
ALTER ROLE agent_ro CONNECTION LIMIT 20;
SQL
