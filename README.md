# kr-public-data-agent

[![tests](https://github.com/zoocasso/kr-public-data-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/zoocasso/kr-public-data-agent/actions/workflows/ci.yml)

공공 R&D·조달 공개 데이터에 자연어로 질문하면, 에이전트가 스키마를 찾아 SQL을 만들고 실행·검증한 뒤 **근거 쿼리와 함께** 답하는 도구.

A natural-language query agent over Korean public R&D and procurement open data. Every number in an answer comes with the SQL that produced it.

> 진행 중 (2026-10). MVP 목표 2026-11 중순, 평가표·docker compose 배포 2026-12.

## 데이터

모두 공공데이터포털 파일데이터, 이용허락범위 제한 없음, API 키 없이 받음. 출처는 [data/sources.yaml](data/sources.yaml), 받은 파일의 해시는 [data/raw/MANIFEST.json](data/raw/MANIFEST.json).

| 테이블 | 내용 | 범위 |
|---|---|---|
| `keit_projects`, `keit_project_participants` | 한국산업기술기획평가원 산업기술 R&D 과제·참여기관 | 2015~2026, 11,900과제 |
| `kimst_project_budgets` | 해양수산과학기술진흥원 과제별·기관별 연구비 | 2021~2023, 4,694행 |
| `ketep_project_orgs` | 한국에너지기술평가원 과제별 수행기관 | 2022, 4,220행 |

- KIMST 원본의 연구책임자(마스킹된 개인 이름) 칼럼은 받을 때 지운다 (`sources.yaml` 의 `drop_columns`). 저장소의 스냅숏에도 없고, MANIFEST 에는 원본 해시(`source_sha256`)와 저장본 해시를 함께 남긴다.
- 결측 표기는 원본 그대로 둔다 (KEIT 는 NULL 대신 `'미분류'`, `'없음'`). 평가셋이 이 함정을 확인한다.
- 나라장터 입찰공고(조달청 OpenAPI)는 인증키 발급 후 추가 예정.

## 구조

```
agent/
  tools.py      list_tables · describe_table · run_sql · search_docs · submit_answer
  sql_guard.py  run_sql 입력 검사 (단일 SELECT, 위험 함수 차단)
  db.py         읽기 전용 계정 + 서버 쪽 커서로 최대 200행
  docs.py       설명서·데이터 사전 BM25 검색 (한글 2-gram)
  loop.py       도구 호출 루프 (프레임워크 없이 Anthropic SDK 로 직접 구현)
eval/
  questions.yaml  평가 질문 40개 + 정답 쿼리
  gold.json       정답 쿼리 실행 결과
  baselines.py    비교 대상: closed_book, oneshot_sql
  run.py          실행·채점·결과 표
```

### 설계 메모

- **두 겹의 쓰기 차단.** `run_sql` 은 sqlglot 으로 단일 SELECT 만 통과시키고, DB 쪽 `agent_ro` 계정은 SELECT 권한만, `default_transaction_read_only=on`, `statement_timeout=5s`. 서버 쪽 커서(`DECLARE CURSOR`)는 SELECT 외 문장을 받지 않는다.
- **근거 쿼리 = 실제 실행된 쿼리.** SQL 을 고쳐 쓰지 않고 원문 그대로 실행하고, 행 수 제한은 커서에서 건다. 답을 제출하면 근거 쿼리가 이번 실행에서 성공했던 쿼리인지 확인하고 다시 실행해 결과를 붙인다.
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

채점: 숫자는 상대오차(개수는 정확히, 금액 1%), 순위는 순서까지, 동점은 집합, 거절 문항은 거절해야 정답, 답할 수 있는 문항을 거절하면 오답. 결과 표는 `eval/results/` 에 쌓인다. 첫 전체 실행은 2026-11 예정이라 아직 결과 표는 없다.

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
