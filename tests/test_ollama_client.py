"""Ollama 어댑터의 요청·응답 변환 (서버 없이)."""

from types import SimpleNamespace as NS

from agent import llm
from agent.ollama_client import OllamaClient


def test_messages_round_trip_tool_calls_and_results():
    history = [
        {"role": "user", "content": "질문"},
        {
            "role": "assistant",
            "content": [
                NS(type="text", text="테이블을 볼게요"),
                NS(type="tool_use", id="call_1", name="describe_table", input={"table": "keit_projects"}),
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "칼럼..."}]},
        {"role": "user", "content": "submit_answer 도구로 최종 답을 제출해 주세요."},
    ]
    out = OllamaClient._messages("시스템", history)
    assert out[0] == {"role": "system", "content": "시스템"}
    assert out[2]["tool_calls"] == [{"function": {"name": "describe_table", "arguments": {"table": "keit_projects"}}}]
    assert out[3] == {"role": "tool", "tool_name": "describe_table", "content": "칼럼..."}
    assert out[4]["role"] == "user"


def test_response_maps_to_anthropic_shape():
    data = {
        "message": {"content": "", "tool_calls": [{"function": {"name": "run_sql", "arguments": '{"sql": "SELECT 1"}'}}]},
        "done_reason": "stop",
        "prompt_eval_count": 1200,
        "eval_count": 40,
    }
    r = OllamaClient._response("ollama:qwen3:4b", data)
    assert r.stop_reason == "tool_use"
    assert r.content[0].name == "run_sql" and r.content[0].input == {"sql": "SELECT 1"}
    assert r.usage.input_tokens == 1200

    r2 = OllamaClient._response("ollama:qwen3:4b", {"message": {"content": "끝"}, "done_reason": "length"})
    assert r2.stop_reason == "max_tokens" and r2.content[0].text == "끝"


def test_routing_and_zero_cost():
    assert isinstance(llm.client("ollama:qwen3:4b"), OllamaClient)
    assert "fallbacks" not in llm.request_kwargs("ollama:qwen3:4b", "low")
    u = llm.Usage("ollama:qwen3:4b")
    u.add(
        NS(
            usage=NS(input_tokens=5000, output_tokens=500, cache_creation_input_tokens=0, cache_read_input_tokens=0),
            model="ollama:qwen3:4b",
        )
    )
    assert u.cost_usd == 0.0
