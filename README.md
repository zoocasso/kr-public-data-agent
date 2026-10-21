# kr-public-data-agent

[![tests](https://github.com/zoocasso/kr-public-data-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/zoocasso/kr-public-data-agent/actions/workflows/ci.yml)

공공 R&D 공개 데이터에 자연어로 질문하면, 에이전트가 스키마를 찾아 SQL을 만들고 실행·검증한 뒤 **근거 쿼리와 함께** 답하는 도구.

A natural-language query agent over Korean public R&D open data (procurement data is planned, not loaded yet). Every answer cites the query or document it rests on: the agent cannot submit a query it has not run, and the eval checks that the query's result holds the submitted values.

> 진행 중 (2026-10). MVP 목표 2026-11 중순, 평가표·docker compose 배포 2026-12.

## 데이터

모두 공공데이터포털 파일데이터, 이용허락범위 제한 없음, API 키 없이 받음. 출처는 [data/sources.yaml](data/sources.yaml), 받은 파일의 해시는 [data/raw/MANIFEST.json](data/raw/MANIFEST.json).

| 테이블 | 내용 | 범위 |
|---|---|---|
| `keit_projects`, `keit_project_participants` | 한국산업기술기획평가원 산업기술 R&D 과제·참여기관 | 2015~2026, 11,900과제 |
| `kimst_project_budgets` | 해양수산과학기술진흥원 과제별·기관별 연구비 | 2021~2023, 4,694행 |
| `ketep_project_orgs` | 한국에너지기술평가원 과제별 수행기관 | 2022, 4,220행 |

- KIMST 원본의 연구책임자(마스킹된 개인 이름) 칼럼은 받을 때 지운다 (`sources.yaml` 의 `drop_columns`). 저장소의 스냅숏에도 없고, MANIFEST 에는 원본 해시(`source_sha256`)와 저장본 해시를 함께 남긴다.
- KEIT 엑셀 원본에 들어 있던 문서 메타데이터(마지막 수정자 이름, 작성 PC 경로)도 받을 때 지운다. 시트 내용은 그대로이고, MANIFEST 에 원본 해시를 함께 남긴다.
- KEIT 데이터설명서(제공기관 문서, 원문 그대로 둠)는 본문에서 "시트 4개", 데이터 시트 16열, 파일 크기 2,166,305 B 라고 적지만, 같은 문서의 시트 표(3개)와 칼럼 명세(15개)는 받은 파일(시트 3개, 15열, 1,747,866 B)과 맞는다. 적재와 평가는 실제 파일의 15개 칼럼을 기준으로 한다.
- 결측 표기는 원본 그대로 둔다 (KEIT 는 NULL 대신 `'미분류'`, `'없음'`). 평가셋이 이 함정을 확인한다.
- 나라장터 입찰공고(조달청 OpenAPI)는 인증키 발급 후 추가 예정.

## 구조

```
agent/
  tools.py          list_tables · describe_table · run_sql · search_docs · submit_answer
  sql_guard.py      run_sql 입력 검사 (단일 SELECT, 함수·테이블 허용 목록)
  db.py             읽기 전용 계정 + 서버 쪽 커서로 최대 200행
  docs.py           설명서·데이터 사전 BM25 검색 (한글 2-gram)
  loop.py           도구 호출 루프 (프레임워크 없이 Anthropic SDK 로 직접 구현)
  llm.py            모델·effort 설정, 토큰·비용 집계
  ollama_client.py  로컬 모델(Ollama)을 같은 루프로 돌리는 어댑터
  prompts.py        시스템 프롬프트
eval/
  questions.yaml  평가 질문 40개 + 정답 쿼리
  make_gold.py    정답 쿼리를 실행해 gold.json 생성
  gold.json       정답 쿼리 실행 결과
  baselines.py    비교 대상: closed_book, oneshot_sql
  grade.py        채점 규칙, 근거 쿼리 확인
  run.py          실행·채점·결과 표
scripts/
  fetch_data.py   공공데이터포털에서 원본 받기 (개인 이름 칼럼은 받을 때 삭제)
  load_db.py      원본을 Postgres 에 적재
```

### 설계 메모

- **허용 목록 검사 + 읽기 전용 계정.** `run_sql` 은 sqlglot 으로 단일 SELECT 인지, 쓰는 함수가 허용 목록(집계·윈도·조건·숫자·문자열·날짜·형 변환)에 있는지, 읽는 테이블이 공개 데이터 테이블이거나 그 자리에서 보이는 CTE 인지 확인한다(다른 범위에 같은 이름의 CTE 를 선언해도 통과하지 못한다). 그래서 `query_to_xml('select pg_sleep(4)', …)`, `pg_notify`, advisory lock, `current_setting`, `pg_roles` 같은 시스템 카탈로그는 실행 전에 거절된다. DB 쪽 `agent_ro` 계정은 SELECT 권한만 있고 임시 테이블 권한도 회수해서, 트랜잭션을 읽기·쓰기로 바꿔도 테이블·임시 객체 어디에도 쓰지 못하며(테스트로 확인), `default_transaction_read_only=on`, `statement_timeout=5s` 가 걸린다. 다만 권한만으로는 `pg_notify` 나 advisory lock 같은 부작용 함수를 막지 못하므로 함수 허용 목록이 그 몫을 맡는다.
- **근거 쿼리 = 실제 실행된 쿼리.** SQL 을 고쳐 쓰지 않고 원문 그대로 실행하고, 행 수 제한은 커서에서 건다. 답은 근거가 있어야 받는다: 이번 실행에서 run_sql 로 성공한 쿼리(evidence_sql)나 search_docs 로 찾은 문서(sources)가 없으면 제출을 도구 오류로 돌려보내 다시 내게 한다. 받은 답의 근거 쿼리는 다시 실행해 결과(앞 50행)를 붙인다. 채점에서는 정답 여부와 따로, 그 결과에 제출한 값이 모두 들어 있는지를 보고 결과 표에 "근거 쿼리 결과로 값이 확인된 정답" 비율을 낸다.
- **오류도 기록으로 남긴다.** DB 오류(시간 초과 포함)는 모델에게 도구 오류로 돌려줘서 고쳐 쓰게 한다. API·Ollama 호출이나 하네스가 실패하면 예외 대신 그때까지의 도구 호출 기록과 토큰 사용량을 담아 `api_error`·`harness_error` 로 끝난다. 사용량을 알 수 없는 문항의 비용은 0 으로 세지 않고 따로 표시한다.
- **스키마는 DB 주석에서.** 칼럼 이름은 영문, 원본 항목명과 의미는 `COMMENT` 로 저장. `describe_table` 은 주석과 함께 값 분포(고유값이 적은 칼럼은 전체 값과 건수)를 보여 줘서 모델이 `'미분류'` 같은 표기를 추측하지 않고 확인하게 한다.
- **최종 답은 도구로 제출.** `submit_answer(answer, values, evidence_sql, sources, refused)` 로 끝나서 채점과 화면 표시가 같은 구조를 쓴다.
- 모델 기본값 `claude-opus-5-5`, effort `medium`, 프롬프트 캐시 자동, 안전 분류기 거절 시 서버 쪽 폴백(`fallbacks: "default"`).

## 평가

질문 40개를 먼저 만들고 정답 쿼리를 붙였다 ([eval/questions.yaml](eval/questions.yaml)).

| 분류 | 문항 | 확인하는 것 |
|---|---|---|
| aggregate | 9 | 단순 집계 |
| grain | 5 | 1행 = 과제×기관일 때 과제 수, `'미분류'` 결측, 파생값 |
| topk | 11 | 그룹·순위 |
| join | 7 | 참여기관 조인, 데이터셋 간 합산, 동점 |
| refuse | 5 | 데이터에 없는 질문 (금액 없는 데이터의 금액, 범위 밖 연도, 적재 안 한 개인정보) |
| docs | 3 | 표 밖 정보 (폐지된 기관코드, 갱신 중단 사유, 칼럼 생성 방식) |

비교 대상 (같은 모델·effort):

- `closed_book` — 도구 없는 LLM
- `oneshot_sql` — 스키마를 프롬프트로 주고 SQL 한 번 생성·실행, 수정 루프 없음
- `agent` — 이 프로젝트

채점 ([eval/grade.py](eval/grade.py)): 숫자는 상대오차(개수는 정확히, 금액 1%)로 보되 제출한 숫자가 모두 맞아야 하고(여러 개를 내고 하나만 맞으면 오답), 여러 숫자는 개수와 순서까지, 순위는 순서까지, 동점은 집합으로 본다. 답이 연도인 문항은 답 문장에서 연도만 숫자로 본다. 거절 문항은 거절해야 정답, 답할 수 있는 문항을 거절하면 오답. 문서 질문은 필수 표현이 모두 있고 틀린 주장 표현이 없어야 정답이다 (공백 무시, 문장의 뜻까지 판정하지는 않는다). 결과는 `eval/results/` 에 쌓이고 git 에는 올리지 않는다. 첫 전체 실행은 2026-11 예정이라 아직 결과 표는 없다.

## 실행

```bash
cp .env.example .env        # ANTHROPIC_API_KEY, DB 비밀번호
python -m venv .venv && .venv/bin/pip install -r requirements.txt
docker compose up -d --wait
.venv/bin/python scripts/load_db.py      # 원본은 repo 에 포함. 새로 받으려면 scripts/fetch_data.py
.venv/bin/python -m pytest               # API 키 없이 돈다 (가짜 클라이언트). DB 테스트는 위 Postgres 에서

.venv/bin/python -m agent -v "2022년 해양수산 R&D 과제는 몇 개였어?"
.venv/bin/python -m eval.run --systems closed_book,oneshot_sql,agent
```
