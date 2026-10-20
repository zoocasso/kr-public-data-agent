"""도구·모델·하네스 오류가 실행 기록을 잃지 않는지 (DB·API 키 없이).

describe_table 이 시간 초과로 끊기면 모델에게 도구 오류로 돌려주고 루프는 계속 간다.
모델 호출(Ollama 포함)이나 하네스가 실패하면 run_agent 는 예외 대신 그때까지의 trace·사용량을 담은 결과를 돌려준다.
"""

from types import SimpleNamespace as NS

import psycopg
import pytest

from agent.loop import run_agent
from agent.ollama_client import OllamaClient, OllamaError
from agent.tools import Toolbox
from eval.baselines import closed_book
from eval.run import summarize


class TimeoutDb:
    """테이블 목록은 주지만, 그 밖의 조회는 statement_timeout 에 걸리는 DB."""

    conn = None

    def fetch(self, query, params=()):
        if "information_schema.tables" in str(query):
            return [("keit_projects",)]
        raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")

    def query(self, text, max_rows=200):
        raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")


class BrokenDb(TimeoutDb):
    def fetch(self, query, params=()):
        if "information_schema.tables" in str(query):
            return [("keit_projects",)]
        raise psycopg.OperationalError("the connection is closed")


def usage():
    return NS(input_tokens=1000, output_tokens=100, cache_creation_input_tokens=0, cache_read_input_tokens=0)


def response(*content, stop="tool_use", model="claude-opus-5-5"):
    return NS(content=list(content), stop_reason=stop, usage=usage(), model=model)


def tool(id_, name, **inp):
    return NS(type="tool_use", id=id_, name=name, input=inp)


class ScriptedClient:
    """준비한 응답을 차례로 돌려주고, 예외가 들어 있으면 그 자리에서 던진다."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


SUBMIT = tool("t9", "submit_answer", answer="데이터를 확인하지 못함", values=[], evidence_sql="", sources=[], refused=True)


# ---------------------------------------------------------------- 도구 오류는 도구 결과로


@pytest.mark.parametrize("db, needle", [(TimeoutDb(), "시간 초과"), (BrokenDb(), "connection is closed")])
def test_db_errors_in_describe_table_become_tool_errors(db, needle):
    out = Toolbox(db).run("describe_table", {"table": "keit_projects"})
    assert out.is_error and needle in out.text


def test_bad_tool_input_is_a_tool_error():
    tb = Toolbox(TimeoutDb())
    out = tb.run("describe_table", {"tablename": "keit_projects"})
    assert out.is_error and "도구 입력 오류" in out.text


def test_an_unterminated_quote_is_rejected_not_raised():
    out = Toolbox(TimeoutDb()).run("run_sql", {"sql": "SELECT 'abc", "purpose": "t"})
    assert out.is_error and "실행 거부" in out.text


def test_the_loop_goes_on_after_a_describe_table_timeout():
    client = ScriptedClient([response(tool("t1", "describe_table", table="keit_projects")), response(SUBMIT)])
    r = run_agent("q", Toolbox(TimeoutDb()), client=client, model="claude-opus-5-5", effort="low")
    assert r.status == "refused"
    assert r.trace[0]["tool"] == "describe_table" and r.trace[0]["ok"] is False
    tool_result = client.requests[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True and "시간 초과" in tool_result["content"]


# ---------------------------------------------------------------- 하네스 오류는 기록을 남기고 끝


def test_an_unreachable_ollama_server_is_an_ollama_error():
    client = OllamaClient(host="http://127.0.0.1:9", timeout=5)  # discard 포트: 아무것도 듣지 않음
    with pytest.raises(OllamaError):
        client.create(model="ollama:qwen3:4b", messages=[{"role": "user", "content": "q"}])


def test_a_model_failure_mid_run_keeps_trace_and_usage():
    client = ScriptedClient([response(tool("t1", "list_tables")), OllamaError("http://127.0.0.1:11434: URLError: refused")])
    r = run_agent("q", Toolbox(TimeoutDb()), client=client, model="claude-opus-5-5", effort="low")
    assert r.status == "api_error" and "OllamaError" in r.answer
    assert [t["tool"] for t in r.trace] == ["list_tables"]
    assert r.usage["calls"] == 1 and r.usage["cost_usd"] > 0


def test_a_bug_in_a_tool_is_a_harness_error_with_trace_and_usage():
    class BuggyToolbox(Toolbox):
        def search_docs(self, query):
            raise RuntimeError("boom")

    client = ScriptedClient([response(tool("t1", "list_tables")), response(tool("t2", "search_docs", query="IRIS"))])
    r = run_agent("q", BuggyToolbox(TimeoutDb()), client=client, model="claude-opus-5-5", effort="low")
    assert r.status == "harness_error" and "RuntimeError: boom" in r.answer
    assert [t["tool"] for t in r.trace] == ["list_tables"]
    assert r.usage["calls"] == 2 and r.usage["cost_usd"] > 0


def test_a_baseline_keeps_its_usage_when_the_output_is_unreadable():
    client = ScriptedClient([response(NS(type="text", text="not json"), stop="end_turn")])
    pred = closed_book("q", client=client, model="claude-opus-5-5", effort="low")
    assert pred["status"] == "bad_output" and "JSONDecodeError" in pred["answer"]
    assert pred["usage"]["calls"] == 1 and pred["usage"]["cost_usd"] > 0


def test_a_baseline_api_error_is_recorded_not_raised():
    client = ScriptedClient([OllamaError("refused")])
    pred = closed_book("q", client=client, model="ollama:qwen3:4b", effort="low")
    assert pred["status"] == "api_error" and pred["usage"]["calls"] == 0


def test_summary_does_not_count_an_unknown_cost_as_zero():
    q = {"id": "r33", "category": "refuse", "question": "q", "answer_type": "refuse"}
    rows = [
        {"id": "r33", "system": "agent", "correct": True, "refused": True, "status": "refused",
         "latency_s": 2.0, "usage": {"cost_usd": 0.02}, "trace": []},
        {"id": "r33", "system": "agent", "correct": False, "refused": False, "status": "harness_error",
         "latency_s": None, "usage": {"cost_usd": None}},
    ]  # fmt: skip
    md = summarize(rows, [q], ["agent"], {"started": "2026-10-20T00:00", "model": "m", "effort": "low"})
    assert "| 총 비용($) | 0.020 (모름 1건 제외) |" in md
    assert "| 응답 시간 중앙값(초) | 2.0 |" in md
