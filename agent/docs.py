"""search_docs 도구용 문서 검색.

말뭉치: 원본 데이터에 딸린 설명서·데이터 사전, 공공데이터포털 데이터셋 설명 (data/raw/**.md).
한국어는 띄어쓰기 단위가 들쭉날쭉해서 글자 2-gram + 영문·숫자 단어를 토큰으로 쓰는 BM25.
문서가 수십 개 수준이라 메모리 안에서 충분하다. 규모가 커지면 pg_trgm 이나 임베딩으로 바꾼다.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPUS_GLOBS = ("data/raw/*/*.md",)
CHUNK_CHARS = 900

_WORD = re.compile(r"[a-z0-9]+")
_HANGUL_RUN = re.compile(r"[가-힣]+")


def tokenize(text: str) -> list[str]:
    text = text.lower()
    tokens = _WORD.findall(text)
    for run in _HANGUL_RUN.findall(text):
        tokens.extend(run[i : i + 2] for i in range(max(len(run) - 1, 1)))
    return tokens


@dataclass
class Chunk:
    source: str
    heading: str
    text: str


def _chunks_of(path: Path) -> list[Chunk]:
    source = str(path.relative_to(ROOT))
    lines = path.read_text(encoding="utf-8").splitlines()
    chunks: list[Chunk] = []
    heading, buf = path.stem, []

    def flush():
        body = "\n".join(buf).strip()
        while body:
            piece, body = body[:CHUNK_CHARS], body[CHUNK_CHARS:]
            if body:  # 줄 경계에서 자른다
                cut = piece.rfind("\n")
                if cut > CHUNK_CHARS // 2:
                    piece, body = piece[:cut], piece[cut:] + body
            chunks.append(Chunk(source, heading, piece.strip()))
            body = body.strip()

    for line in lines:
        if line.startswith("#"):
            flush()
            heading, buf = line.lstrip("#").strip(), []
        else:
            buf.append(line)
    flush()
    return chunks


class DocIndex:
    def __init__(self, root: Path = ROOT, k1: float = 1.2, b: float = 0.75):
        paths = sorted({p for g in CORPUS_GLOBS for p in root.glob(g)})
        self.chunks = [c for p in paths for c in _chunks_of(p)]
        self.k1, self.b = k1, b
        self.tfs = [Counter(tokenize(f"{c.heading}\n{c.text}")) for c in self.chunks]
        self.lens = [sum(tf.values()) for tf in self.tfs]
        self.avg_len = sum(self.lens) / max(len(self.lens), 1)
        df = Counter(t for tf in self.tfs for t in tf)
        n = len(self.chunks)
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    def search(self, query: str, k: int = 4) -> list[tuple[float, Chunk]]:
        q = Counter(tokenize(query))
        scored = []
        for i, tf in enumerate(self.tfs):
            s = 0.0
            for t in q:
                f = tf.get(t)
                if not f:
                    continue
                norm = f + self.k1 * (1 - self.b + self.b * self.lens[i] / self.avg_len)
                s += self.idf[t] * f * (self.k1 + 1) / norm
            if s > 0:
                scored.append((s, self.chunks[i]))
        scored.sort(key=lambda x: -x[0])
        return scored[:k]
