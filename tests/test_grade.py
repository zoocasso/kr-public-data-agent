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


BY_ID = {q["id"]: q for q in QUESTIONS}


@pytest.mark.parametrize(
    "pred, ok",
    [
        ({"values": ["1569"]}, True),
        ({"values": ["1,569건"]}, True),
        ({"values": ["2024", "1569"]}, False),  # 하나만 맞으면 오답
        ({"values": ["1569", "2024"]}, False),
        ({"values": ["1569", "11900"]}, False),
        ({"values": [], "answer": "2024년 KEIT 과제는 1,569건입니다."}, True),  # 연도 표기는 숫자로 치지 않는다
        ({"values": [], "answer": "1,569건입니다 (전체 11,900건 중)."}, False),
        ({"values": [], "answer": "모르겠습니다."}, False),
    ],
)
def test_a_number_answer_must_not_hedge_with_extra_numbers(pred, ok):
    assert grade(BY_ID["r01"], GOLD["r01"], {"refused": False, **pred})[0] is ok


def test_numbers_must_match_in_count_and_order():
    q, gold = BY_ID["r31"], GOLD["r31"]
    assert grade(q, gold, {"values": [str(v) for v in gold]})[0]
    assert not grade(q, gold, {"values": [str(v) for v in gold] + ["2022"]})[0]
    assert not grade(q, gold, {"values": [str(v) for v in reversed(gold)]})[0]
    assert not grade(q, gold, {"values": [str(gold[0])]})[0]


@pytest.mark.parametrize(
    "answer, ok",
    [
        ("1450000 은 2025-09-30 에 폐지된 코드이고, 지금은 산업통상부(1451000)를 씁니다.", True),
        ("폐 지 된 코드입니다. 후속 코드는 1451000 입니다.", True),  # 공백은 무시
        ("폐지되지 않았고 1451000 과 함께 지금도 씁니다.", False),  # 틀린 주장
        ("1450000 은 아직 유효합니다. 폐지 예정이며 1451000 이 생길 수 있습니다.", False),
        ("산업통상자원부 코드입니다.", False),  # 필수 표현 없음
    ],
)
def test_a_text_answer_needs_the_facts_and_no_contradiction(answer, ok):
    assert grade(BY_ID["r38"], None, {"refused": False, "answer": answer})[0] is ok


def test_evidence_must_show_the_submitted_values():
    from eval.grade import evidence_supports

    check = {"executed_in_run": True, "runs": True, "result": [["1569"]]}
    assert evidence_supports({"values": ["1569"], "evidence_check": check})
    assert evidence_supports({"values": ["1,569"], "evidence_check": check})
    assert not evidence_supports({"values": ["1570"], "evidence_check": check})  # 쿼리 결과와 다른 값
    assert not evidence_supports({"values": ["1569"], "evidence_check": {**check, "executed_in_run": False}})
    assert not evidence_supports({"values": ["1569"], "evidence_check": {**check, "runs": False, "result": "DB 오류"}})
    assert not evidence_supports({"values": [], "evidence_check": check})
    # 제출 값의 소수 자릿수만큼 반올림해서 비교, 이름은 공백·대소문자 무시
    avg = {"executed_in_run": True, "runs": True, "result": [["3.2378076487268417"]]}
    assert evidence_supports({"values": ["3.24"], "evidence_check": avg})
    assert not evidence_supports({"values": ["3.3"], "evidence_check": avg})
    names = {"executed_in_run": True, "runs": True, "result": [["자동차/철도차량", "1200"], ["반도체소자및시스템", "900"]]}
    assert evidence_supports({"values": ["자동차/철도차량", "반도체소자및시스템"], "evidence_check": names})
    assert not evidence_supports({"values": ["자동차/철도차량", "제조융합서비스"], "evidence_check": names})


@pytest.mark.parametrize(
    "answer, ok",
    [
        ("2024년입니다.", True),
        ("2024년이 1,569건으로 가장 많았습니다.", True),  # 연도 문항에서는 건수가 아니라 연도를 본다
        ("2020년입니다.", False),
        ("2024년과 2020년이 많았습니다.", False),
    ],
)
def test_a_year_answer_is_read_from_the_sentence(answer, ok):
    assert grade(BY_ID["r22"], GOLD["r22"], {"refused": False, "values": [], "answer": answer})[0] is ok
