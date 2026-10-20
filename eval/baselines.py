"""비교용 베이스라인 두 가지.

closed_book  도구도 스키마도 없이 질문만 받는 LLM. "도구 없는 LLM" 기준선.
oneshot_sql  스키마(DDL + 칼럼 설명)를 프롬프트로 받고 SQL 을 한 번만 만든다.
             하네스가 그 SQL 을 run_sql 과 같은 검사·계정으로 한 번 실행하고, 결과를 그대로 답으로 본다.
             오류 수정·값 확인·문서 검색이 없는 "흔한 Text-to-SQL" 기준선.

두 베이스라인 모두 에이전트와 같은 모델·effort·거절 규칙을 쓴다.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from agent import llm
from agent.tools import Toolbox

ROOT = Path(__file__).resolve().parent.parent

CLOSED_BOOK_SYSTEM = """\
너는 한국 공공 R&D 통계 질문에 답하는 도우미다. 데이터베이스나 검색 도구는 없다.
알고 있는 지식으로 답하되, 확실히 알 수 없으면 추정하지 말고 refused=true 로 답한다.
values 에는 핵심 값만 넣는다. 숫자는 쉼표·단위 없이 원 단위 그대로, 이름은 정식 명칭으로, 순위는 순서대로.
"""

ONESHOT_SYSTEM = """\
너는 한국 공공 R&D 공개 데이터(PostgreSQL)에 대한 질문을 SQL 로 바꾸는 도우미다.
아래 스키마만 보고 질문에 답하는 SELECT 쿼리 하나를 만든다. 쿼리는 한 번만 실행되고, 결과의 첫 칼럼이 답으로 쓰인다.
그러므로 답이 되는 값(개수, 합계, 이름 목록)을 첫 칼럼에 두고, 순위 질문이면 ORDER BY 로 순서를 맞춘다.
필요한 칼럼·기간·대상이 스키마에 없으면 refused=true 로 하고 sql 은 빈 문자열로 둔다.

## 스키마
{schema}
"""

_CLOSED_FORMAT = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "values": {"type": "array", "items": {"type": "string"}},
            "refused": {"type": "boolean"},
        },
        "required": ["answer", "values", "refused"],
        "additionalProperties": False,
    },
}

_ONESHOT_FORMAT = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "sql": {"type": "string"},
            "refused": {"type": "boolean"},
            "reason": {"type": "string"},
        },
        "required": ["sql", "refused", "reason"],
        "additionalProperties": False,
    },
}


def schema_text() -> str:
    """에이전트가 describe_table 로 보는 것과 같은 주석이 달린 DDL (값 분포는 빠짐)."""
    return (ROOT / "db" / "init" / "01_schema.sql").read_text()


def _call(client, model: str, effort: str, system: str, question: str, fmt: dict) -> tuple[dict, llm.Usage, str]:
    usage = llm.Usage(model)
    kwargs = llm.request_kwargs(model, effort)
    kwargs["output_config"] = {**kwargs["output_config"], "format": fmt}
    response = client.beta.messages.create(system=system, messages=[{"role": "user", "content": question}], **kwargs)
    usage.add(response)
    if response.stop_reason in ("refusal", "max_tokens"):
        return {}, usage, response.stop_reason
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text), usage, "ok"


def closed_book(question: str, *, client, model: str, effort: str, **_) -> dict:
    t0 = time.perf_counter()
    out, usage, stop = _call(client, model, effort, CLOSED_BOOK_SYSTEM, question, _CLOSED_FORMAT)
    return {
        "status": ("refused" if out.get("refused") else "answered") if stop == "ok" else stop,
        "answer": out.get("answer", ""),
        "values": out.get("values", []),
        "refused": bool(out.get("refused")),
        "usage": usage.as_dict(),
        "latency_s": round(time.perf_counter() - t0, 2),
    }


def _values_from_rows(answer_type: str, rows: list[list[str]]) -> list[str]:
    if not rows:
        return []
    if answer_type == "number":
        return [rows[0][0]]
    if answer_type == "numbers":
        return list(rows[0])
    return [r[0] for r in rows]


def oneshot_sql(question: str, *, client, model: str, effort: str, toolbox: Toolbox, answer_type: str, **_) -> dict:
    t0 = time.perf_counter()
    system = ONESHOT_SYSTEM.format(schema=schema_text())
    out, usage, stop = _call(client, model, effort, system, question, _ONESHOT_FORMAT)
    pred = {
        "status": stop,
        "answer": out.get("reason", ""),
        "values": [],
        "refused": bool(out.get("refused")),
        "evidence_sql": out.get("sql", ""),
        "sql_errors": 0,
        "usage": usage.as_dict(),
    }
    if stop == "ok":
        if pred["refused"] or not pred["evidence_sql"].strip():
            pred["status"] = "refused" if pred["refused"] else "no_sql"
        else:
            res = toolbox.run_sql(pred["evidence_sql"], "oneshot")
            if res.is_error:
                pred["status"], pred["sql_errors"], pred["answer"] = "sql_error", 1, res.text
            else:
                pred["status"] = "answered"
                pred["values"] = _values_from_rows(answer_type, res.meta["result"])
                pred["answer"] = res.text[:500]
    pred["latency_s"] = round(time.perf_counter() - t0, 2)
    return pred
