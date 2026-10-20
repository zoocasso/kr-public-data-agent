"""가짜 Claude 클라이언트로 루프 로직만 검증 (API 키·비용 없이)."""

from types import SimpleNamespace as NS

from agent.loop import run_agent


def text(t):
    return NS(type="text", text=t)


def tool(id_, name, **inp):
    return NS(type="tool_use", id=id_, name=name, input=inp)


def response(*content, stop="tool_use"):
    usage = NS(input_tokens=1000, output_tokens=100, cache_creation_input_tokens=0, cache_read_input_tokens=500)
    return NS(content=list(content), stop_reason=stop, usage=usage, model="claude-opus-5-5")


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})  # 호출 시점 사본
        return self.script.pop(0)


GOOD_SQL = "SELECT count(*) FROM keit_projects WHERE support_year = 2024"


def test_repairs_sql_and_submits_with_verified_evidence(toolbox):
    client = FakeClient(
        [
            response(tool("t1", "list_tables")),
            response(tool("t2", "run_sql", sql="SELECT count(*) FROM keit WHERE year = 2024", purpose="첫 시도")),
            response(tool("t3", "run_sql", sql=GOOD_SQL, purpose="고친 쿼리")),
            response(
                tool("t4", "submit_answer", answer="1,569건", values=["1569"], evidence_sql=GOOD_SQL, sources=[], refused=False)
            ),
        ]
    )
    r = run_agent("2024년 KEIT 과제 수?", toolbox, client=client, model="claude-opus-5-5", effort="low")

    assert r.status == "answered" and r.values == ["1569"]
    assert r.sql_errors == 1 and r.tool_calls == 3
    assert r.evidence_check == {"executed_in_run": True, "runs": True, "result": [["1569"]]}
    assert r.usage["calls"] == 4 and r.usage["cost_usd"] > 0

    # 오류 결과는 is_error 로 돌려주고, 이전 대화는 손대지 않고 이어 붙인다
    third = client.requests[2]["messages"]
    assert third[-1]["content"][0]["is_error"] is True
    assert [m["role"] for m in third] == ["user", "assistant", "user", "assistant", "user"]
    assert client.requests[0]["fallbacks"] == "default"
    assert client.requests[0]["output_config"] == {"effort": "low"}


def test_nudges_once_when_model_answers_in_plain_text(toolbox):
    client = FakeClient(
        [
            response(text("1,569건입니다."), stop="end_turn"),
            response(tool("t1", "submit_answer", answer="데이터에 없음", values=[], evidence_sql="", sources=[], refused=True)),
        ]
    )
    r = run_agent("q", toolbox, client=client, model="claude-opus-5-5", effort="low")
    assert r.status == "refused" and r.evidence_check is None
    assert client.requests[1]["messages"][-1] == {"role": "user", "content": "submit_answer 도구로 최종 답을 제출해 주세요."}


def test_flags_evidence_that_was_never_run(toolbox):
    client = FakeClient(
        [
            response(
                tool("t1", "submit_answer", answer="1569", values=["1569"], evidence_sql=GOOD_SQL, sources=[], refused=False)
            ),
        ]
    )
    r = run_agent("q", toolbox, client=client, model="claude-haiku-5-5", effort="low")
    assert r.evidence_check["executed_in_run"] is False and r.evidence_check["runs"] is True
    assert "fallbacks" not in client.requests[0]  # Haiku 는 서버 폴백 미지원
