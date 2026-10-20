"""에이전트 루프 (프레임워크 없이 직접 구현).

질문 → [Claude 응답 → 도구 실행 → 결과 전달] 반복 → submit_answer 로 종료.
매 호출의 사용량을 모으고, 도구 호출 기록(trace)을 남기며,
제출된 근거 쿼리가 실제로 실행됐던 쿼리인지 확인하고 다시 실행해 결과를 붙인다.
API·로컬 모델·하네스 오류로 멈춰도 예외를 올리지 않고, 그때까지의 trace 와 사용량을 담아 돌려준다.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field

import anthropic

from . import llm
from .ollama_client import OllamaError
from .prompts import SYSTEM_PROMPT
from .tools import Toolbox, as_tool_result

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
    return re.sub(r"\s+", " ", s.strip().rstrip(";")).lower()


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

            final = next((b for b in tool_uses if b.name == "submit_answer"), None)
            if final is not None:
                args = final.input if isinstance(final.input, dict) else {}
                values = args.get("values") or []
                result.answer = str(args.get("answer") or "")
                result.values = [str(v) for v in (values if isinstance(values, list) else [values])]
                result.evidence_sql = str(args.get("evidence_sql") or "")
                result.sources = [str(s) for s in args.get("sources") or []]
                result.status = "refused" if args.get("refused") else "answered"
                emit("final", **args)
                break

            tool_results = []
            for block in tool_uses:
                t0 = time.perf_counter()
                outcome = toolbox.run(block.name, dict(block.input))
                record = {
                    "tool": block.name,
                    "input": dict(block.input),
                    "ok": not outcome.is_error,
                    "ms": round((time.perf_counter() - t0) * 1000),
                    "output": outcome.text[:2000],
                    **({"meta": outcome.meta} if outcome.meta else {}),
                }
                result.trace.append(record)
                emit("tool", **record)
                tool_results.append(as_tool_result(block.id, outcome))
            messages.append({"role": "user", "content": tool_results})
    except (anthropic.APIError, OllamaError) as e:
        result.status = "api_error"
        result.answer = f"{type(e).__name__}: {e}"
    except Exception as e:  # 도구나 루프의 버그: 평가가 이 문항의 trace·사용량을 잃지 않게 기록하고 끝낸다
        result.status = "harness_error"
        result.answer = f"{type(e).__name__}: {e}"

    result.latency_s = round(time.perf_counter() - start, 2)
    result.usage = usage.as_dict()
    if result.evidence_sql.strip():
        result.evidence_check = _check_evidence(result, toolbox)
    return result


def _check_evidence(result: AgentResult, toolbox: Toolbox) -> dict:
    """근거 쿼리가 이번 실행에서 성공했던 쿼리인지 보고, 다시 실행해 결과를 붙인다."""
    executed = {_norm_sql(t["input"]["sql"]) for t in result.trace if t["tool"] == "run_sql" and t["ok"]}
    rerun = toolbox.run_sql(result.evidence_sql, "evidence check")
    return {
        "executed_in_run": _norm_sql(result.evidence_sql) in executed,
        "runs": not rerun.is_error,
        "result": rerun.meta.get("result") if not rerun.is_error else rerun.text,
    }
