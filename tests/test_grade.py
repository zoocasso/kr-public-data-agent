import json
from pathlib import Path

import pytest
import yaml

from eval.grade import grade, numbers_in, parse_number

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS = yaml.safe_load((ROOT / "eval" / "questions.yaml").read_text())["questions"]
GOLD = json.loads((ROOT / "eval" / "gold.json").read_text())


@pytest.mark.parametrize(
    "text, value",
    [
        ("570019537000", 570019537000),
        ("1,569", 1569),
        ("5,700억", 570_000_000_000),
        ("약 5,700억 원", 570_000_000_000),
        ("1조 6,514억", 1_651_400_000_000),
        ("3.24년", 3.24),
        ("없음", None),
    ],
)
def test_parse_number(text, value):
    assert parse_number(text) == value


def test_numbers_in_sentence():
    assert numbers_in("2022년 총 연구비는 약 5,700억 원입니다") == [2022, 570_000_000_000]


def _perfect(q):
    if q["answer_type"] == "refuse":
        return {"refused": True}
    if q["answer_type"] == "text":
        return {"refused": False, "answer": " ".join(m.split("|")[0] for m in q["must_include"])}
    g = GOLD[q["id"]]
    return {"refused": False, "values": [str(v) for v in (g if isinstance(g, list) else [g])]}


@pytest.mark.parametrize("q", QUESTIONS, ids=[q["id"] for q in QUESTIONS])
def test_gold_answers_grade_correct(q):
    ok, why = grade(q, GOLD.get(q["id"]), _perfect(q))
    assert ok, why


def test_common_wrong_answers_grade_wrong():
    by_id = {q["id"]: q for q in QUESTIONS}
    # 행 수를 과제 수로 센 경우
    assert not grade(by_id["r10"], GOLD["r10"], {"values": ["4220"]})[0]
    # IS NULL 로만 센 경우
    assert not grade(by_id["r13"], GOLD["r13"], {"values": ["0"]})[0]
    # 순서가 틀린 경우
    assert not grade(by_id["r15"], GOLD["r15"], {"values": list(reversed(GOLD["r15"]))})[0]
    # 동점 중 하나만 답한 경우
    assert not grade(by_id["r32"], GOLD["r32"], {"values": GOLD["r32"][:1]})[0]
    # 없는 데이터를 지어낸 경우
    assert not grade(by_id["r33"], None, {"refused": False, "values": ["12345"]})[0]
    # 답할 수 있는데 거절한 경우
    assert not grade(by_id["r01"], GOLD["r01"], {"refused": True})[0]
    # 금액은 1% 안이면 정답
    assert grade(by_id["r03"], GOLD["r03"], {"values": [], "answer": "약 5,700억 원"})[0]
