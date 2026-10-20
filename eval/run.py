"""평가 실행: 질문셋을 각 시스템에 돌리고 채점해서 결과 표를 만든다.

    python -m eval.run                                   # 전체, 세 시스템
    python -m eval.run --systems agent --ids r01,r13     # 일부만
    python -m eval.run --model claude-sonnet-5-5 --effort low

결과: eval/results/<시각>_<모델>_<effort>/ 에 predictions.jsonl, summary.md
"""

from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

from agent import llm
from agent.db import Database
from agent.loop import run_agent
from agent.tools import Toolbox

from .baselines import closed_book, oneshot_sql
from .grade import grade

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "eval"
SYSTEMS = ("closed_book", "oneshot_sql", "agent")
CATEGORIES = ("aggregate", "grain", "topk", "join", "refuse", "docs")

_local = threading.local()


def _toolbox() -> Toolbox:
    if not hasattr(_local, "toolbox"):
        _local.toolbox = Toolbox(Database())
    return _local.toolbox


def _run_one(system: str, q: dict, client, model: str, effort: str) -> dict:
    tb = _toolbox()
    if system == "agent":
        r = run_agent(q["question"], tb, model=model, effort=effort, client=client)
        pred = r.to_dict()
        pred["refused"] = r.status == "refused"
    elif system == "closed_book":
        pred = closed_book(q["question"], client=client, model=model, effort=effort)
    else:
        pred = oneshot_sql(q["question"], client=client, model=model, effort=effort, toolbox=tb, answer_type=q["answer_type"])
    return pred


def _pct(n: int, d: int) -> str:
    return "–" if d == 0 else f"{100 * n / d:.0f}% ({n}/{d})"


def summarize(rows: list[dict], questions: list[dict], systems: list[str], meta: dict) -> str:
    by_sys = defaultdict(list)
    for r in rows:
        by_sys[r["system"]].append(r)
    qcat = {q["id"]: q["category"] for q in questions}

    lines = [
        f"# 평가 결과 {meta['started'][:16]}",
        "",
        f"- 모델 `{meta['model']}`, effort `{meta['effort']}`, 질문 {len(questions)}개",
        "- 데이터 스냅숏: data/raw/MANIFEST.json, 정답: eval/gold.json",
        "",
        "## 요약",
        "",
        "| 지표 | " + " | ".join(systems) + " |",
        "|---|" + "---|" * len(systems),
    ]

    def row(label, fn):
        lines.append(f"| {label} | " + " | ".join(fn(by_sys[s]) for s in systems) + " |")

    answerable = lambda rs: [r for r in rs if qcat[r["id"]] != "refuse"]  # noqa: E731
    unanswerable = lambda rs: [r for r in rs if qcat[r["id"]] == "refuse"]  # noqa: E731
    row("전체 정답률", lambda rs: _pct(sum(r["correct"] for r in rs), len(rs)))
    row("답할 수 있는 질문 정답률", lambda rs: _pct(sum(r["correct"] for r in answerable(rs)), len(answerable(rs))))
    row("거절해야 할 질문을 거절", lambda rs: _pct(sum(r["refused"] for r in unanswerable(rs)), len(unanswerable(rs))))
    row("답할 수 있는데 거절 (낮을수록 좋음)", lambda rs: _pct(sum(r["refused"] for r in answerable(rs)), len(answerable(rs))))
    for c in CATEGORIES:
        row(
            f"└ {c}",
            lambda rs, c=c: _pct(sum(r["correct"] for r in rs if qcat[r["id"]] == c), sum(1 for r in rs if qcat[r["id"]] == c)),
        )
    row("응답 시간 중앙값(초)", lambda rs: f"{statistics.median(r['latency_s'] for r in rs):.1f}" if rs else "–")
    row("질문당 비용($)", lambda rs: f"{sum(r['usage']['cost_usd'] or 0 for r in rs) / max(len(rs), 1):.4f}")
    row("총 비용($)", lambda rs: f"{sum(r['usage']['cost_usd'] or 0 for r in rs):.3f}")

    agent = by_sys.get("agent", [])
    if agent:
        calls = sum(r.get("tool_calls", 0) for r in agent)
        failed = sum(1 for r in agent for t in r.get("trace", []) if not t["ok"])
        had_err = [r for r in agent if r.get("sql_errors", 0) > 0]
        answered = [r for r in agent if r["status"] == "answered" and r.get("evidence_sql")]
        verified = [r for r in answered if (r.get("evidence_check") or {}).get("executed_in_run")]
        lines += [
            "",
            "## 에이전트 동작",
            "",
            f"- 질문당 도구 호출 평균 {calls / len(agent):.1f}회, 도구 호출 성공률 {_pct(calls - failed, calls)}",
            f"- SQL 오류가 한 번 이상 난 질문 {len(had_err)}개 중 최종 정답 {sum(r['correct'] for r in had_err)}개",
            f"- 근거 쿼리가 실제 실행된 쿼리와 일치 {_pct(len(verified), len(answered))}",
            "- 종료 상태: " + ", ".join(f"{k} {v}" for k, v in sorted(_count(r["status"] for r in agent).items())),
        ]

    lines += ["", "## 문항별", "", "| id | 분류 | 질문 | " + " | ".join(systems) + " |", "|---|---|---|" + "---|" * len(systems)]
    mark = {(r["system"], r["id"]): ("✓" if r["correct"] else "✗") for r in rows}
    for q in questions:
        cells = " | ".join(mark.get((s, q["id"]), "") for s in systems)
        lines.append(f"| {q['id']} | {q['category']} | {q['question']} | {cells} |")
    return "\n".join(lines) + "\n"


def _count(it):
    out = defaultdict(int)
    for x in it:
        out[x] += 1
    return dict(out)


def main() -> None:
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser(prog="python -m eval.run")
    ap.add_argument("--systems", default=",".join(SYSTEMS))
    ap.add_argument("--ids", help="쉼표로 구분한 질문 id")
    ap.add_argument("--model", default=llm.model_name())
    ap.add_argument("--effort", default=llm.effort_level())
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    systems = [s for s in args.systems.split(",") if s]
    unknown = set(systems) - set(SYSTEMS)
    if unknown:
        ap.error(f"모르는 시스템: {unknown}")
    questions = yaml.safe_load((EVAL / "questions.yaml").read_text())["questions"]
    if args.ids:
        wanted = set(args.ids.split(","))
        questions = [q for q in questions if q["id"] in wanted]
    gold = json.loads((EVAL / "gold.json").read_text())

    started = datetime.now()
    out_dir = EVAL / "results" / f"{started:%Y%m%d-%H%M}_{args.model}_{args.effort}"
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {"started": started.isoformat(), "model": args.model, "effort": args.effort, "systems": systems}
    client = llm.client(args.model)

    jobs = [(s, q) for q in questions for s in systems]
    rows = []
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.workers) as pool, open(out_dir / "predictions.jsonl", "w") as f:
        futures = {pool.submit(_run_one, s, q, client, args.model, args.effort): (s, q) for s, q in jobs}
        for fut in as_completed(futures):
            s, q = futures[fut]
            try:
                pred = fut.result()
            except Exception as e:  # 한 문항 실패가 전체 실행을 멈추지 않게
                pred = {
                    "status": "harness_error",
                    "answer": f"{type(e).__name__}: {e}",
                    "values": [],
                    "refused": False,
                    "usage": {"cost_usd": 0},
                    "latency_s": 0,
                }
            ok, why = grade(q, gold.get(q["id"]), pred)
            row = {"id": q["id"], "system": s, "correct": ok, "why": why, **pred}
            rows.append(row)
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            print(f"[{len(rows):>3}/{len(jobs)}] {s:<11} {q['id']} {'✓' if ok else '✗'} {why[:70]}")

    order = {q["id"]: i for i, q in enumerate(questions)}
    rows.sort(key=lambda r: (order[r["id"]], systems.index(r["system"])))
    meta["elapsed_s"] = round(time.perf_counter() - t0, 1)
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    summary = summarize(rows, questions, systems, meta)
    (out_dir / "summary.md").write_text(summary)
    print("\n" + summary.split("## 문항별")[0])
    print(f"결과: {out_dir.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
