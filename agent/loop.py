"""에이전트 루프 (프레임워크 없이 직접 구현).

질문 → [Claude 응답 → 도구 실행 → 결과 전달] 반복 → submit_answer 로 종료.
매 호출의 사용량을 모으고, 도구 호출 기록(trace)을 남기며,
근거 없는 제출(이번 실행에서 성공하지 않은 쿼리, search_docs 로 찾지 않은 문서, 둘 다 없음)은 도구 오류로 돌려보내고,
받은 답의 근거 쿼리는 다시 실행해 결과를 붙인다.
API·로컬 모델·하네스 오류로 멈춰도 예외를 올리지 않고, 그때까지의 trace 와 사용량을 담아 돌려준다.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field

import anthropic
import sqlglot

from . import llm
from .ollama_client import OllamaError
from .prompts import SYSTEM_PROMPT
from .tools import Toolbox, ToolOutcome, as_tool_result

MAX_CALLS = 15
NUDGE = "submit_answer 도구로 최종 답을 제출해 주세요."


@dataclass
class AgentResult:
    question: str
    # answered | refused | no_answer | max_calls | model_refusal | max_tokens | api_error | harness_error
    status: str
    answer: str = ""
    values: list[str] = field(default_factory=list)
    evidence_sql: str = ""
    sources: list[str] = field(default_factory=list)
    evidence_check: dict | None = None
    trace: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    latency_s: float = 0.0

    @property
    def tool_calls(self) -> int:
        return len(self.trace)

    @property
    def sql_errors(self) -> int:
        return sum(1 for t in self.trace if t["tool"] == "run_sql" and not t["ok"])

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tool_calls"] = self.tool_calls
        d["sql_errors"] = self.sql_errors
        return d


def _norm_sql(s: str) -> str:
    """같은 쿼리인지 비교하는 형태. 키워드 대소문자·공백만 맞추고 문자열 값은 그대로 둔다 ('ABC' ≠ 'abc')."""
    text = s.strip().rstrip(";").strip()
    try:
        return sqlglot.parse_one(text, read="postgres").sql(dialect="postgres")
    except sqlglot.errors.SqlglotError:
        return re.sub(r"\s+", " ", text)


def _evidence_problem(args: dict, trace: list[dict]) -> str | None:
    """제출을 받을 수 없는 이유. 거절이 아닌 답은 이번 실행의 근거(성공한 run_sql 쿼리나 search_docs 문서)가 있어야 한다."""
    if args.get("refused"):
        return None
    evidence = str(args.get("evidence_sql") or "").strip()
    sources = [str(s) for s in args.get("sources") or []]
    if not evidence and not sources:
        return (
            "근거가 없습니다. 숫자는 run_sql 로 실행해 성공한 쿼리를 evidence_sql 에, "
            "문서 내용은 search_docs 로 찾은 문서 경로를 sources 에 넣어 다시 제출하세요."
        )
    executed = {_norm_sql(t["input"].get("sql", "")) for t in trace if t["tool"] == "run_sql" and t["ok"]}
    if evidence and _norm_sql(evidence) not in executed:
        return (
            "evidence_sql 은 이번 대화에서 run_sql 로 실행해 성공한 쿼리여야 합니다. "
            "그 쿼리를 먼저 run_sql 로 실행하거나, 실행한 쿼리를 그대로 넣어 다시 제출하세요."
        )
    found = {src for t in trace if t["tool"] == "search_docs" and t["ok"] for src in t.get("meta", {}).get("sources", [])}
    unknown = [src for src in sources if src not in found]
    if unknown:
        return f"sources 에는 이번 대화의 search_docs 결과에 나온 문서 경로만 넣을 수 있습니다: {unknown}"
    return None


def _text_of(content) -> str:
    return "\n".join(b.text for b in content if b.type == "text").strip()


def run_agent(
    question: str,
    toolbox: Toolbox,
    *,
    model: str | None = None,
    effort: str | None = None,
    client: anthropic.Anthropic | None = None,
    on_event=None,
) -> AgentResult:
    model = model or llm.model_name()
    effort = effort or llm.effort_level()
    client = client or llm.client(model)
    usage = llm.Usage(model)
    tools = toolbox.definitions()
    base = llm.request_kwargs(model, effort)
    messages: list[dict] = [{"role": "user", "content": question}]
    result = AgentResult(question=question, status="max_calls")
    nudged = False
    start = time.perf_counter()

    def emit(kind: str, **data):
        if on_event:
            on_event(kind, data)

    try:
        for _ in range(MAX_CALLS):
            response = client.beta.messages.create(system=SYSTEM_PROMPT, tools=tools, messages=messages, **base)
            usage.add(response)

            if response.stop_reason == "refusal":
                result.status = "model_refusal"
                break
            if response.stop_reason == "max_tokens":
                result.status = "max_tokens"
                break

            # 생각 블록까지 응답 그대로 이어 붙인다 (편집하면 다음 호출에서 무효가 됨)
            messages.append({"role": "assistant", "content": response.content})
            tool_uses = [b for b in response.content if b.type == "tool_use"]

            if not tool_uses:
                if nudged:
                    result.status, result.answer = "no_answer", _text_of(response.content)
                    break
                nudged = True
                messages.append({"role": "user", "content": NUDGE})
                continue

            # 다른 도구를 먼저 실행하고 제출은 마지막에 본다: 같은 응답에서 실행한 쿼리도 근거로 인정된다
            final = next((b for b in tool_uses if b.name == "submit_answer"), None)
            outcomes: dict[str, ToolOutcome] = {}
            for block in tool_uses:
                if block is final:
                    continue
                t0 = time.perf_counter()
                outcomes[block.id] = toolbox.run(block.name, dict(block.input))
                _record(result, emit, block.name, dict(block.input), outcomes[block.id], t0)

            if final is not None:
                args = final.input if isinstance(final.input, dict) else {}
                problem = _evidence_problem(args, result.trace)
                if problem is None:
                    values = args.get("values") or []
                    result.answer = str(args.get("answer") or "")
                    result.values = [str(v) for v in (values if isinstance(values, list) else [values])]
                    result.evidence_sql = str(args.get("evidence_sql") or "")
                    result.sources = [str(s) for s in args.get("sources") or []]
                    result.status = "refused" if args.get("refused") else "answered"
                    emit("final", **args)
                    break
                # 근거 없는 제출은 받지 않고 이유를 도구 오류로 돌려준다. 다시 제출할 기회는 MAX_CALLS 안에서 남는다.
                outcomes[final.id] = ToolOutcome(problem, is_error=True)
                _record(result, emit, "submit_answer", dict(args), outcomes[final.id], time.perf_counter())

            messages.append({"role": "user", "content": [as_tool_result(b.id, outcomes[b.id]) for b in tool_uses]})
    except (anthropic.APIError, OllamaError) as e:
        result.status = "api_error"
        result.answer = f"{type(e).__name__}: {e}"
    except Exception as e:  # 도구나 루프의 버그: 평가가 이 문항의 trace·사용량을 잃지 않게 기록하고 끝낸다
        result.status = "harness_error"
        result.answer = f"{type(e).__name__}: {e}"

    result.latency_s = round(time.perf_counter() - start, 2)
    result.usage = usage.as_dict()
    if result.evidence_sql.strip():
        try:
            result.evidence_check = _check_evidence(result, toolbox)
        except Exception as e:  # 근거 확인이 실패해도 이 문항의 기록은 남긴다
            result.evidence_check = {"error": f"{type(e).__name__}: {e}"}
    return result


def _record(result: AgentResult, emit, tool: str, args: dict, outcome: ToolOutcome, t0: float) -> None:
    record = {
        "tool": tool,
        "input": args,
        "ok": not outcome.is_error,
        "ms": round((time.perf_counter() - t0) * 1000),
        "output": outcome.text[:2000],
        **({"meta": outcome.meta} if outcome.meta else {}),
    }
    result.trace.append(record)
    emit("tool", **record)


def _check_evidence(result: AgentResult, toolbox: Toolbox) -> dict:
    """근거 쿼리가 이번 실행에서 성공했던 쿼리인지 보고, 다시 실행해 결과를 붙인다."""
    executed = {_norm_sql(t["input"]["sql"]) for t in result.trace if t["tool"] == "run_sql" and t["ok"]}
    rerun = toolbox.run_sql(result.evidence_sql, "evidence check")
    return {
        "executed_in_run": _norm_sql(result.evidence_sql) in executed,
        "runs": not rerun.is_error,
        "result": rerun.meta.get("result") if not rerun.is_error else rerun.text,
    }
