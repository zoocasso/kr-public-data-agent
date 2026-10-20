"""로컬 모델(Ollama) 어댑터.

에이전트 루프와 베이스라인은 Anthropic SDK 의 `client.beta.messages.create(...)` 모양만 쓴다.
이 클래스는 같은 모양으로 요청을 받아 Ollama /api/chat 으로 보내고, 응답을
Anthropic 응답과 같은 속성(content 블록, stop_reason, usage, model)으로 돌려준다.
덕분에 같은 루프·도구·채점으로 Claude 와 로컬 모델을 비교할 수 있다 (폐쇄망 설치 시나리오).

모델 이름은 "ollama:<ollama 모델명>" 으로 쓴다. 예: ollama:qwen3:4b
"""

from __future__ import annotations

import http.client
import itertools
import json
import os
import urllib.error
import urllib.request
from types import SimpleNamespace as NS

PREFIX = "ollama:"
_ids = itertools.count(1)


class OllamaError(RuntimeError):
    """Ollama 서버에 닿지 못했거나 응답을 읽지 못함. 에이전트 루프는 anthropic.APIError 처럼 다룬다."""


def is_ollama(model: str) -> bool:
    return model.startswith(PREFIX)


def _get(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


class OllamaClient:
    def __init__(self, host: str | None = None, num_ctx: int | None = None, timeout: float = 900):
        self.host = (host or os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")).rstrip("/")
        if not self.host.startswith("http"):
            self.host = "http://" + self.host
        self.num_ctx = num_ctx or int(os.environ.get("OLLAMA_NUM_CTX", "12288"))
        self.timeout = timeout
        self.beta = NS(messages=NS(create=self.create))
        self.messages = NS(create=self.create)

    # ------------------------------------------------------------ 요청 변환
    @staticmethod
    def _tools(tools: list[dict] | None) -> list[dict] | None:
        if not tools:
            return None
        return [
            {
                "type": "function",
                "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]},
            }
            for t in tools
        ]

    @staticmethod
    def _messages(system: str | None, messages: list[dict]) -> list[dict]:
        out = [{"role": "system", "content": system}] if system else []
        names: dict[str, str] = {}  # tool_use_id → 도구 이름
        for m in messages:
            role, content = m["role"], m["content"]
            if isinstance(content, str):
                out.append({"role": role, "content": content})
                continue
            if role == "assistant":
                text, calls = [], []
                for b in content:
                    kind = _get(b, "type")
                    if kind == "text":
                        text.append(_get(b, "text"))
                    elif kind == "tool_use":
                        names[_get(b, "id")] = _get(b, "name")
                        calls.append({"function": {"name": _get(b, "name"), "arguments": _get(b, "input")}})
                msg = {"role": "assistant", "content": "\n".join(text)}
                if calls:
                    msg["tool_calls"] = calls
                out.append(msg)
            else:
                for b in content:
                    if _get(b, "type") == "tool_result":
                        out.append(
                            {"role": "tool", "tool_name": names.get(_get(b, "tool_use_id"), ""), "content": _get(b, "content")}
                        )
                    elif _get(b, "type") == "text":
                        out.append({"role": "user", "content": _get(b, "text")})
        return out

    # ------------------------------------------------------------ 호출
    def create(
        self,
        *,
        model: str,
        messages: list[dict],
        system: str | None = None,
        tools=None,
        max_tokens: int = 4096,
        output_config: dict | None = None,
        **_ignored,
    ):
        name = model[len(PREFIX) :] if is_ollama(model) else model
        output_config = output_config or {}
        body = {
            "model": name,
            "messages": self._messages(system, messages),
            "stream": False,
            # effort low → 생각 끄기 (속도), 그 외 → 생각 켜기
            "think": output_config.get("effort", "low") != "low",
            "options": {"num_ctx": self.num_ctx, "num_predict": min(max_tokens, 8192)},
        }
        if tools:
            body["tools"] = self._tools(tools)
        fmt = output_config.get("format")
        if fmt and fmt.get("type") == "json_schema":
            body["format"] = fmt["schema"]

        req = urllib.request.Request(
            f"{self.host}/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
        except (urllib.error.URLError, http.client.HTTPException, OSError, json.JSONDecodeError) as e:
            raise OllamaError(f"{self.host}: {type(e).__name__}: {e}") from e
        return self._response(model, data)

    @staticmethod
    def _response(model: str, data: dict):
        msg = data.get("message", {})
        content = []
        if msg.get("content", "").strip():
            content.append(NS(type="text", text=msg["content"]))
        for call in msg.get("tool_calls") or []:
            fn = call.get("function", {})
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            content.append(NS(type="tool_use", id=f"call_{next(_ids)}", name=fn.get("name", ""), input=args))
        if any(b.type == "tool_use" for b in content):
            stop = "tool_use"
        elif data.get("done_reason") == "length":
            stop = "max_tokens"
        else:
            stop = "end_turn"
        usage = NS(
            input_tokens=data.get("prompt_eval_count", 0),
            output_tokens=data.get("eval_count", 0),
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )
        return NS(content=content, stop_reason=stop, usage=usage, model=model)
