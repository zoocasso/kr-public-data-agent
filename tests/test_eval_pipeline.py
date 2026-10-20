"""평가 파이프라인을 API 호출 없이 끝까지 돌려 본다."""

import json
from pathlib import Path
from types import SimpleNamespace as NS

import yaml

from eval.baselines import closed_book, oneshot_sql
from eval.grade import grade
from eval.run import summarize

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS = {q["id"]: q for q in yaml.safe_load((ROOT / "eval" / "questions.yaml").read_text())["questions"]}
GOLD = json.loads((ROOT / "eval" / "gold.json").read_text())


class JsonClient:
    """구조화 출력 하나를 돌려주는 가짜 클라이언트."""

    def __init__(self, payload):
        self.payload = payload
        self.kwargs = None
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kwargs):
        self.kwargs = kwargs
        usage = NS(input_tokens=3000, output_tokens=200, cache_creation_input_tokens=0, cache_read_input_tokens=0)
        return NS(
            content=[NS(type="text", text=json.dumps(self.payload, ensure_ascii=False))],
            stop_reason="end_turn",
            usage=usage,
            model="claude-opus-5-5",
        )


def test_oneshot_executes_sql_once_and_grades(toolbox):
    q = QUESTIONS["r15"]
    client = JsonClient({"sql": q["gold_sql"], "refused": False, "reason": ""})
    pred = oneshot_sql(
        q["question"], client=client, model="claude-opus-5-5", effort="low", toolbox=toolbox, answer_type=q["answer_type"]
    )
    assert pred["status"] == "answered" and pred["values"] == GOLD["r15"]
    assert grade(q, GOLD["r15"], pred)[0]
    assert "keit_projects" in client.kwargs["system"]  # 스키마가 프롬프트에 들어감
    assert client.kwargs["output_config"]["format"]["type"] == "json_schema"


def test_oneshot_sql_error_is_wrong_not_crash(toolbox):
    q = QUESTIONS["r01"]
    client = JsonClient({"sql": "SELECT count(*) FROM projects", "refused": False, "reason": ""})
    pred = oneshot_sql(q["question"], client=client, model="claude-opus-5-5", effort="low", toolbox=toolbox, answer_type="number")
    assert pred["status"] == "sql_error" and not grade(q, GOLD["r01"], pred)[0]


def test_closed_book_and_summary_table():
    q = QUESTIONS["r33"]
    pred = closed_book(
        q["question"], client=JsonClient({"answer": "모름", "values": [], "refused": True}), model="claude-opus-5-5", effort="low"
    )
    ok, why = grade(q, None, pred)
    rows = [{"id": "r33", "system": "closed_book", "correct": ok, "why": why, **pred}]
    md = summarize(rows, [q], ["closed_book"], {"started": "2026-10-13T00:00", "model": "m", "effort": "low"})
    assert "| 거절해야 할 질문을 거절 | 100% (1/1) |" in md
    assert "| r33 | refuse |" in md
