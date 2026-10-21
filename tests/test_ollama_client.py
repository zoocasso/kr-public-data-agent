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


def _response(model, input_tokens, output_tokens, cache_write=0, cache_read=0):
    usage = NS(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_write,
        cache_read_input_tokens=cache_read,
    )
    return NS(usage=usage, model=model)


def test_haiku_prompts_over_100k_tokens_use_the_long_prompt_price():
    u = llm.Usage("claude-haiku-5-5")
    u.add(_response("claude-haiku-5-5", 50_000, 1_000))  # 0.10 / 0.50
    assert round(u.cost_usd, 6) == round((50_000 * 0.10 + 1_000 * 0.50) / 1e6, 6)
    u.add(_response("claude-haiku-5-5", 20_000, 1_000, cache_read=90_000))  # 110K prompt: 0.50 / 2.50, read 0.05
    long_call = (20_000 * 0.50 + 1_000 * 2.50 + 90_000 * 0.05) / 1e6
    assert round(u.cost_usd, 6) == round((50_000 * 0.10 + 1_000 * 0.50) / 1e6 + long_call, 6)
    # the tier is per call: Opus has a single rate card
    o = llm.Usage("claude-opus-5-5")
    o.add(_response("claude-opus-5-5", 200_000, 1_000))
    assert round(o.cost_usd, 6) == round((200_000 * 4.00 + 1_000 * 20.00) / 1e6, 6)
    assert llm.Usage("some-unknown-model").cost_usd is None
