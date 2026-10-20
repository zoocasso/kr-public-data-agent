"""eval/questions.yaml 의 gold_sql 을 읽기 전용 계정으로 실행해 eval/gold.json 을 만든다.

    python eval/make_gold.py

answer_type 과 결과 모양이 안 맞으면(예: number 인데 여러 행) 실패한다.
gold.json 은 커밋해 두고, 데이터 스냅숏이 바뀌면 다시 만든다.
"""

from __future__ import annotations

import json
import os
import sys
from decimal import Decimal
from pathlib import Path

import psycopg
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "eval"


def _plain(v):
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    return v


def gold_value(q: dict, rows: list[tuple]):
    t = q["answer_type"]
    if t == "number":
        if len(rows) != 1 or len(rows[0]) != 1:
            raise ValueError(f"{q['id']}: number 인데 결과가 {len(rows)}행")
        return _plain(rows[0][0])
    if t == "numbers":
        if len(rows) != 1:
            raise ValueError(f"{q['id']}: numbers 인데 결과가 {len(rows)}행")
        return [_plain(v) for v in rows[0]]
    if t in ("ordered_list", "set"):
        if not rows:
            raise ValueError(f"{q['id']}: 결과 없음")
        return [_plain(r[0]) for r in rows]
    raise ValueError(f"{q['id']}: gold_sql 이 필요 없는 answer_type {t}")


def main() -> None:
    load_dotenv(ROOT / ".env")
    questions = yaml.safe_load((EVAL / "questions.yaml").read_text())["questions"]
    ids = [q["id"] for q in questions]
    if len(ids) != len(set(ids)):
        sys.exit("중복 id 있음")

    gold = {}
    with psycopg.connect(os.environ["AGENT_DATABASE_URL"], autocommit=True) as conn:
        for q in questions:
            if "gold_sql" not in q:
                continue
            rows = conn.execute(q["gold_sql"]).fetchall()
            gold[q["id"]] = gold_value(q, rows)
            print(f"{q['id']:>4}  {q['answer_type']:<12} {str(gold[q['id']])[:90]}")

    (EVAL / "gold.json").write_text(json.dumps(gold, ensure_ascii=False, indent=2) + "\n")
    counts = {}
    for q in questions:
        counts[q["category"]] = counts.get(q["category"], 0) + 1
    print(f"\n{len(questions)}문항, 정답 {len(gold)}개 저장. 분류별: {counts}")


if __name__ == "__main__":
    main()
