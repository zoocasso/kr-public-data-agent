"""예측을 정답과 비교한다. 모든 비교 시스템(에이전트·베이스라인)에 같은 규칙을 쓴다.

예측은 {"refused": bool, "values": [...], "answer": str} 모양.
values 를 우선 보고, values 가 비어 있을 때만 answer 문장에서 숫자를 뽑는다.
"""

from __future__ import annotations

import re

_UNITS = {"조": 10**12, "억": 10**8, "만": 10**4}
_NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
_KOR_AMOUNT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(조|억|만)")


def parse_number(s: str) -> float | None:
    """'570019537000', '1,569', '5,700억', '1조 6,514억' 같은 표기를 숫자로."""
    s = str(s).strip()
    if not s:
        return None
    if _KOR_AMOUNT.search(s):
        total = 0.0
        for num, unit in _KOR_AMOUNT.findall(s):
            total += float(num.replace(",", "")) * _UNITS[unit]
        rest = _KOR_AMOUNT.sub("", s)
        tail = _NUM.search(rest.replace("원", ""))
        if tail and not re.search(r"[가-힣]", rest[: tail.start()]):
            total += float(tail.group().replace(",", ""))
        return total
    m = _NUM.search(s)
    return float(m.group().replace(",", "")) if m else None


def numbers_in(text: str) -> list[float]:
    out = []
    for m in re.finditer(r"\d[\d,]*(?:\.\d+)?\s*(?:조(?:\s*\d[\d,]*\s*억)?|억|만)?", text):
        v = parse_number(m.group())
        if v is not None:
            out.append(v)
    return out


def norm_name(s: str) -> str:
    return re.sub(r"\s+", "", str(s)).lower()


def _close(pred: float, gold: float, tol: float) -> bool:
    if tol == 0:
        return abs(pred - gold) < 1e-6
    return abs(pred - gold) <= tol * max(abs(gold), 1e-9)


def _names(values: list) -> list[str]:
    return [norm_name(v) for v in values if parse_number(v) is None or re.search(r"[가-힣a-zA-Z]", str(v))]


def grade(q: dict, gold, pred: dict) -> tuple[bool, str]:
    t = q["answer_type"]
    refused = bool(pred.get("refused"))
    if t == "refuse":
        return refused, "거절함" if refused else "거절해야 하는데 답함"
    if refused:
        return False, "답할 수 있는데 거절함"

    values = [str(v) for v in pred.get("values") or []]
    answer = pred.get("answer") or ""

    if t == "text":
        missing = [m for m in q["must_include"] if not any(alt in answer for alt in m.split("|"))]
        return (not missing), ("필수 내용 포함" if not missing else f"빠짐: {missing}")

    tol = float(q.get("tolerance", 0))
    if t == "number":
        cands = [v for v in (parse_number(x) for x in values) if v is not None] or numbers_in(answer)
        if not cands:
            return False, "숫자 없음"
        ok = _close(cands[0], float(gold), tol)
        return ok, f"예측 {cands[0]:g} / 정답 {float(gold):g}"
    if t == "numbers":
        cands = [v for v in (parse_number(x) for x in values) if v is not None] or numbers_in(answer)
        ok = len(cands) >= len(gold) and all(_close(p, float(g), tol) for p, g in zip(cands, gold, strict=False))
        return ok, f"예측 {cands[: len(gold)]} / 정답 {gold}"
    if t == "ordered_list":
        names = _names(values)
        ok = names[: len(gold)] == [norm_name(g) for g in gold] and len(names) == len(gold)
        return ok, f"예측 {values} / 정답 {gold}"
    if t == "set":
        ok = set(_names(values)) == {norm_name(g) for g in gold}
        return ok, f"예측 {values} / 정답 {gold}"
    raise ValueError(f"알 수 없는 answer_type: {t}")
