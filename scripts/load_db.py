"""data/raw/ 의 원본 파일을 정리해서 Postgres 에 적재한다 (매번 전체 재적재).

    python scripts/load_db.py

정리 규칙
- KEIT: "미부여" 기관코드 → NULL, 참여기관 쉼표 목록 → keit_project_participants 행으로 분리
- KIMST: 연구책임자(마스킹된 개인 이름) 칼럼은 fetch_data.py 가 받을 때 지운다
- 빈 문자열 → NULL
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pandas as pd
import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"


def _read_csv(path: Path) -> pd.DataFrame:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp949"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError(f"인코딩을 알 수 없음: {path}")
    df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
    return df.apply(lambda s: s.str.strip()).replace({"": None})


def _one(pattern: str) -> Path:
    found = sorted(RAW.glob(pattern))
    if len(found) != 1:
        raise FileNotFoundError(f"{pattern}: {len(found)}개 발견 (fetch_data.py 먼저 실행)")
    return found[0]


def load_keit() -> tuple[pd.DataFrame, pd.DataFrame]:
    path = _one("keit/*.xlsx")
    xls = pd.ExcelFile(path)
    df = pd.read_excel(xls, xls.sheet_names[0], dtype=str)
    df = df.apply(lambda s: s.str.strip()).replace({"": None, "미부여": None})
    projects = pd.DataFrame(
        {
            "project_no": df["과제번호"].astype("int64"),
            "support_year": df["지원년도"].astype("int16"),
            "title": df["과제명"],
            "start_date": df["개발시작일"],
            "end_date": df["개발종료일"],
            "lead_org": df["주관기관"],
            "lead_org_code": df["주관기관_기관코드"],
            "field_detail": df["세부분야"],
            "tech_field": df["기술분야"],
            "program_category": df["사업분류"],
            "sub_program": df["내역사업명"],
            "ministry": df["주관부처"],
            "ministry_code": df["주관부처_기관코드"],
        }
    )
    parts = (
        df[["과제번호", "참여기관"]]
        .dropna()
        .query("참여기관 != '없음'")
        .assign(org_name=lambda d: d["참여기관"].str.split(","))
        .explode("org_name")
        .assign(org_name=lambda d: d["org_name"].str.strip())
        .query("org_name != ''")
        .rename(columns={"과제번호": "project_no"})[["project_no", "org_name"]]
        .astype({"project_no": "int64"})
        .drop_duplicates()
    )
    return projects, parts


def load_kimst() -> pd.DataFrame:
    df = _read_csv(_one("kimst/*.csv"))
    return pd.DataFrame(
        {
            "budget_year": df["예산년도"].astype("int16"),
            "program_name": df["사업명"],
            "project_title": df["과제명"],
            "role": df["구분"],
            "org_name": df["연구기관"],
            "org_type": df["기관유형"],
            "start_date": df["당해년도 연구기간 시작일"],
            "end_date": df["당해년도 연구기간 종료일"],
            "total_budget_krw": df["총 연구비"].astype("int64"),
            "gov_grant_krw": df["정부출연금"].astype("int64"),
        }
    )


def load_ketep() -> pd.DataFrame:
    df = _read_csv(_one("ketep/*.csv"))
    return pd.DataFrame(
        {
            "base_year": df["기준연도"].astype("int16"),
            "detail_program": df["세부사업"],
            "sub_program": df["내역사업"],
            "account_name": df["회계명"],
            "project_title": df["과제명"],
            "org_role": df["수행기관 역할구분"],
            "org_name": df["수행기관명"],
            "org_type": df["수행기관 기관유형"],
            "org_region": df["수행기관 지역"],
        }
    )


def _copy(cur: psycopg.Cursor, table: str, df: pd.DataFrame) -> None:
    cols = ", ".join(df.columns)
    with cur.copy(f"COPY {table} ({cols}) FROM STDIN") as cp:
        for row in df.itertuples(index=False, name=None):
            cp.write_row([None if pd.isna(v) else v for v in row])
    print(f"  {table}: {len(df):,} rows")


def main() -> None:
    load_dotenv(ROOT / ".env")
    keit, keit_parts = load_keit()
    kimst = load_kimst()
    ketep = load_ketep()
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn, conn.cursor() as cur:
        cur.execute(
            "TRUNCATE keit_project_participants, keit_projects, kimst_project_budgets, ketep_project_orgs RESTART IDENTITY"
        )
        _copy(cur, "keit_projects", keit)
        _copy(cur, "keit_project_participants", keit_parts)
        _copy(cur, "kimst_project_budgets", kimst)
        _copy(cur, "ketep_project_orgs", ketep)
        cur.execute("ANALYZE")
    print("done")


if __name__ == "__main__":
    main()
