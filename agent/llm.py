"""Claude API 호출 설정과 토큰·비용 집계."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import anthropic

from .ollama_client import OllamaClient, is_ollama

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"

# USD / 1M tokens: (input, output, cache write 5분, cache read). 2026-10 공개 가격 기준.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
}
# 프롬프트(입력 + 캐시 쓰기 + 캐시 읽기)가 이 토큰 수를 넘는 호출은 다른 가격표를 쓴다.
# Haiku 5.5 는 100K 토큰을 넘으면 입력·출력 단가가 5배 (캐시 쓰기 1.25배, 읽기 0.1배 비율은 같음).
LONG_PROMPT_PRICES = {
    "claude-haiku-5-5": (100_000, (0.50, 2.50, 0.625, 0.05)),
}


def call_cost(model: str, input_tokens: int, output_tokens: int, cache_write: int, cache_read: int) -> float | None:
    """API 호출 한 번의 비용(USD). 가격표에 없는 모델이면 None."""
    price = PRICES.get(model)
    if price is None:
        return None
    long_prompt = LONG_PROMPT_PRICES.get(model)
    if long_prompt and input_tokens + cache_write + cache_read > long_prompt[0]:
        price = long_prompt[1]
    p_in, p_out, p_cw, p_cr = price
    return (input_tokens * p_in + output_tokens * p_out + cache_write * p_cw + cache_read * p_cr) / 1_000_000


# 안전 분류기가 요청을 거절하면 서버에서 다른 모델로 다시 돌린다. Haiku 는 지원하지 않음.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def model_name() -> str:
    return os.environ.get("AGENT_MODEL", DEFAULT_MODEL)


def effort_level() -> str:
    return os.environ.get("AGENT_EFFORT", DEFAULT_EFFORT)


def client(model: str | None = None):
    """모델 이름이 ollama: 로 시작하면 로컬 Ollama 어댑터, 아니면 Anthropic 클라이언트."""
    if model and is_ollama(model):
        return OllamaClient()
    return anthropic.Anthropic()


def request_kwargs(model: str, effort: str) -> dict:
    """모든 호출에 공통으로 붙는 인자. 프롬프트 캐시는 마지막 블록까지 자동으로 건다."""
    kwargs = {
        "model": model,
        "max_tokens": 16000,
        "output_config": {"effort": effort},
        "cache_control": {"type": "ephemeral"},
    }
    if model in FALLBACK_MODELS:
        kwargs["betas"] = [FALLBACK_BETA]
        kwargs["fallbacks"] = "default"
    return kwargs


@dataclass
class Usage:
    model: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    served_by: set[str] = field(default_factory=set)
    # 가격표가 프롬프트 길이에 따라 달라서 합계가 아니라 호출마다 계산해 더한다
    priced_cost: float = 0.0
    unpriced_calls: int = 0

    def add(self, response) -> None:
        u = response.usage
        tokens = (
            u.input_tokens or 0,
            u.output_tokens or 0,
            u.cache_creation_input_tokens or 0,
            u.cache_read_input_tokens or 0,
        )
        self.calls += 1
        self.input_tokens += tokens[0]
        self.output_tokens += tokens[1]
        self.cache_write_tokens += tokens[2]
        self.cache_read_tokens += tokens[3]
        self.served_by.add(response.model)
        cost = call_cost(self.model, *tokens)
        if cost is None:
            self.unpriced_calls += 1
        else:
            self.priced_cost += cost

    @property
    def cost_usd(self) -> float | None:
        if is_ollama(self.model):
            return 0.0  # 로컬 실행: API 비용 없음
        if self.model not in PRICES or self.unpriced_calls:
            return None
        return self.priced_cost

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "served_by": sorted(self.served_by),
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cost_usd": None if self.cost_usd is None else round(self.cost_usd, 5),
        }
