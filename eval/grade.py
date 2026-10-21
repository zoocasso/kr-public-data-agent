"""예측을 정답과 비교한다. 모든 비교 시스템(에이전트·베이스라인)에 같은 규칙을 쓴다.

예측은 {"refused": bool, "values": [...], "answer": str} 모양.
values 의 숫자를 보고, values 에 숫자가 없을 때만 answer 문장에서 숫자를 뽑는다 (연도 'NNNN년' 은 뺀다).
제출한 숫자는 모두 맞아야 한다: 여러 개를 내고 그중 하나만 맞으면 오답이다.

문서 질문(text)은 필수 표현이 모두 있고 틀린 주장 표현이 없으면 정답으로 친다 (공백 무시).
문장의 뜻까지 판정하지는 않는다.

evidence_supports 는 정답 여부와 따로, 에이전트가 낸 근거 쿼리의 결과가 제출한 값을 실제로 담고 있는지 본다.
"""

from __future__ import annotations

import re

_UNITS = {"조": 10**12, "억": 10**8, "만": 10**4}
_YEAR = re.compile(r"\d{4}\s*(?:[~\-–]\s*\d{4}\s*)?년")
_PLAIN_NUMBER = re.compile(r"-?\d+(?:\.(\d+))?")
_YEAR_VALUE = re.compile(r"(?<![\d,.])((?:19|20)\d{2})(?![\d,.])")
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


def _numbers(values: list[str], answer: str, year: bool = False) -> list[float]:
    """values 의 숫자. values 에 숫자가 하나도 없으면 answer 문장의 숫자를 쓴다.

    문장에서는 보통 연도 표기('2024년')를 빼지만, 답이 연도인 문항(unit: year)은 거꾸로 연도만 본다.
    """
    found = [v for v in (parse_number(x) for x in values) if v is not None]
    if found:
        return found
    if year:
        return [float(y) for y in _YEAR_VALUE.findall(answer)]
    return numbers_in(_YEAR.sub(" ", answer))


def _fmt(nums: list[float]) -> str:
    return "[" + ", ".join(f"{n:g}" for n in nums) + "]"


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
        flat = norm_name(answer)
        missing = [m for m in q["must_include"] if not any(norm_name(alt) in flat for alt in m.split("|"))]
        wrong = [w for w in q.get("must_not_include", []) if norm_name(w) in flat]
        if missing or wrong:
            return False, " · ".join(([f"빠짐: {missing}"] if missing else []) + ([f"틀린 주장: {wrong}"] if wrong else []))
        return True, "필수 내용 포함"

    tol = float(q.get("tolerance", 0))
    cands = _numbers(values, answer, year=q.get("unit") == "year")
    if t == "number":
        if not cands:
            return False, "숫자 없음"
        ok = all(_close(c, float(gold), tol) for c in cands)
        return ok, f"예측 {_fmt(cands)} / 정답 {float(gold):g}"
    if t == "numbers":
        ok = len(cands) == len(gold) and all(_close(p, float(g), tol) for p, g in zip(cands, gold, strict=True))
        return ok, f"예측 {_fmt(cands)} / 정답 {gold}"
    if t == "ordered_list":
        names = _names(values)
        ok = names[: len(gold)] == [norm_name(g) for g in gold] and len(names) == len(gold)
        return ok, f"예측 {values} / 정답 {gold}"
    if t == "set":
        ok = set(_names(values)) == {norm_name(g) for g in gold}
        return ok, f"예측 {values} / 정답 {gold}"
    raise ValueError(f"알 수 없는 answer_type: {t}")


def _value_in_cell(value: str, cell: str) -> bool:
    """제출한 값이 결과 칸 하나와 같은가. 숫자는 제출한 값의 소수 자릿수로 반올림해서 비교한다 (3.24 ↔ 3.2378…)."""
    if norm_name(value) == norm_name(cell):
        return True
    v = _PLAIN_NUMBER.fullmatch(str(value).strip().replace(",", ""))
    c = _PLAIN_NUMBER.fullmatch(str(cell).strip())
    if not (v and c):
        return False
    digits = len(v.group(1) or "")
    return round(float(c.group()), digits) == round(float(v.group()), digits)


def evidence_supports(pred: dict) -> bool:
    """근거 쿼리가 이번 실행에서 성공한 쿼리이고, 다시 실행한 결과(앞 50행)에 제출한 값이 모두 들어 있는가."""
    check = pred.get("evidence_check") or {}
    rows = check.get("result")
    values = [str(v) for v in pred.get("values") or []]
    if not (check.get("executed_in_run") and check.get("runs") and isinstance(rows, list) and values):
        return False
    cells = [str(c) for row in rows for c in row]
    return all(any(_value_in_cell(v, c) for c in cells) for v in values)
