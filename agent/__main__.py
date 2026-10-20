"""명령줄에서 질문 하나 던지기.

python -m agent "2024년에 지원한 KEIT 과제는 몇 건이야?"
python -m agent -v "..."     # 도구 호출 과정까지 출력
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from .db import Database
from .loop import run_agent
from .tools import Toolbox

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser(prog="python -m agent")
    ap.add_argument("question")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--model")
    ap.add_argument("--effort")
    ap.add_argument("--json", action="store_true", help="결과 전체를 JSON 으로 출력")
    args = ap.parse_args()

    def on_event(kind, data):
        if args.verbose and kind == "tool":
            arg = data["input"].get("sql") or data["input"].get("table") or data["input"].get("query") or ""
            mark = "✓" if data["ok"] else "✗"
            print(f"  {mark} {data['tool']}({' '.join(str(arg).split())[:120]}) {data['ms']}ms")

    db = Database()
    try:
        result = run_agent(args.question, Toolbox(db), model=args.model, effort=args.effort, on_event=on_event)
    finally:
        db.close()

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return
    print(f"\n[{result.status}] {result.answer}\n")
    if result.evidence_sql:
        print("근거 쿼리:\n" + result.evidence_sql.strip() + "\n")
    if result.sources:
        print("참고 문서: " + ", ".join(result.sources))
    u = result.usage
    print(
        f"도구 {result.tool_calls}회 (SQL 오류 {result.sql_errors}) · API {u['calls']}회 · {result.latency_s}s · ${u['cost_usd']}"
    )


if __name__ == "__main__":
    main()
