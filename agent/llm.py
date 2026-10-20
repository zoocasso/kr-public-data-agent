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

    def add(self, response) -> None:
        u = response.usage
        self.calls += 1
        self.input_tokens += u.input_tokens or 0
        self.output_tokens += u.output_tokens or 0
        self.cache_write_tokens += u.cache_creation_input_tokens or 0
        self.cache_read_tokens += u.cache_read_input_tokens or 0
        self.served_by.add(response.model)

    @property
    def cost_usd(self) -> float | None:
        if is_ollama(self.model):
            return 0.0  # 로컬 실행: API 비용 없음
        price = PRICES.get(self.model)
        if price is None:
            return None
        p_in, p_out, p_cw, p_cr = price
        return (
            self.input_tokens * p_in + self.output_tokens * p_out + self.cache_write_tokens * p_cw + self.cache_read_tokens * p_cr
        ) / 1_000_000

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
