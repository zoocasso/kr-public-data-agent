"""data/sources.yaml 의 공공데이터포털 파일데이터를 data/raw/<key>/ 에 내려받는다.

파일 ID(atchFileId)는 제공기관이 새 버전을 올릴 때 바뀌므로, 매번 데이터셋 페이지에서
현재 다운로드 링크를 찾는다. 받은 파일의 sha256 은 data/raw/MANIFEST.json 에 남겨
평가셋 정답이 어떤 스냅숏 기준인지 추적한다.

sources.yaml 의 drop_columns 칼럼(개인 이름 등)은 저장하기 전에 지운다. MANIFEST 에는 받은 원본의
해시(source_sha256)와 저장한 파일의 해시(sha256)를 함께 남긴다.

    python scripts/fetch_data.py            # 전체
    python scripts/fetch_data.py keit       # 일부만
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import html
import io
import json
import re
import sys
import urllib.parse
import urllib.request
import zipfile
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
UA = "Mozilla/5.0 (kr-public-data-agent; +https://github.com/zoocasso)"
DOWNLOAD_RE = re.compile(r"fileDownload\.do\?atchFileId=(FILE_\d+)&(?:amp;)?fileDetailSn=(\d+)")


def _get(url: str) -> tuple[bytes, dict[str, str]]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read(), dict(resp.headers)


def _filename(headers: dict[str, str], fallback: str) -> str:
    cd = headers.get("Content-Disposition", "")
    m = re.search(r'filename="?([^";]+)"?', cd)
    name = urllib.parse.unquote(m.group(1)) if m else fallback
    # 일부 기관은 UTF-8 파일명을 퍼센트 인코딩 없이 보내서 latin-1 로 읽힌다.
    with contextlib.suppress(UnicodeEncodeError, UnicodeDecodeError):
        name = name.encode("latin-1").decode("utf-8")
    return Path(name).name  # 경로 조각 제거


def _portal_description(page_html: str, ds: dict) -> str:
    """데이터셋 페이지의 '파일데이터 정보' 영역을 평문으로 뽑는다 (search_docs 말뭉치용)."""
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page_html, flags=re.S)
    text = re.sub(r"<(?:br|/p|/tr|/li|/h\d)[^>]*>", "\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    lines = [" ".join(line.split()) for line in text.splitlines()]
    body = "\n".join(line for line in lines if line)
    start = body.find("파일데이터명")
    end = body.find("표준 메타데이터", start)
    if start < 0:
        raise RuntimeError(f"{ds['key']}: 설명 영역을 찾지 못함")
    return (
        f"# {ds['title']} (공공데이터포털 설명)\n\n"
        f"- 출처: {ds['page']}\n- 수집일: {date.today().isoformat()}\n\n" + body[start : end if end > 0 else None].strip() + "\n"
    )


def drop_csv_columns(data: bytes, columns: list[str]) -> bytes:
    """CSV 에서 columns 칼럼을 뺀다. 인코딩(UTF-8 BOM 또는 CP949)과 줄바꿈은 원본 그대로 둔다."""
    for enc in ("utf-8-sig", "cp949"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("인코딩을 알 수 없는 CSV")
    newline = "\r\n" if "\r\n" in text[:4096] else "\n"
    rows = [r for r in csv.reader(io.StringIO(text, newline="")) if r]
    header = rows[0]
    missing = [c for c in columns if c not in header]
    if missing:
        raise ValueError(f"없는 칼럼: {missing}")
    keep = [i for i, name in enumerate(header) if name not in columns]
    out = io.StringIO(newline="")
    csv.writer(out, lineterminator=newline).writerows([[r[i] if i < len(r) else "" for i in keep] for r in rows])
    return out.getvalue().encode(enc)


def check_drop_columns(ds: dict, filenames: list[str]) -> list[str]:
    """지울 칼럼 목록. drop_csv_columns 는 CSV 만 다루므로, 다른 형식의 데이터 파일이 섞여 있으면
    지우지 못한 채 저장하지 않도록 아무것도 쓰기 전에 멈춘다 (.md 는 설명 문서)."""
    drop = ds.get("drop_columns") or []
    not_csv = [f for f in filenames if not f.lower().endswith((".csv", ".md"))]
    if drop and not_csv:
        raise ValueError(f"{ds['key']}: drop_columns 는 CSV 에만 쓸 수 있는데 CSV 가 아닌 파일이 있습니다: {not_csv}")
    return drop


def fetch(ds: dict) -> list[dict]:
    page, _ = _get(ds["page"])
    page_html = page.decode("utf-8", "ignore")
    m = DOWNLOAD_RE.search(page_html)
    if not m:
        raise RuntimeError(f"{ds['key']}: 다운로드 링크를 찾지 못함 ({ds['page']})")
    file_id, sn = m.groups()
    url = f"https://www.data.go.kr/cmm/cmm/fileDownload.do?atchFileId={file_id}&fileDetailSn={sn}&insertDataPrcus=N"
    body, headers = _get(url)
    out_dir = RAW / ds["key"]
    out_dir.mkdir(parents=True, exist_ok=True)
    name = _filename(headers, f"{ds['key']}.bin")

    files: dict[str, bytes] = {}
    if name.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                files[Path(info.filename).name] = zf.read(info)
    else:
        files[name] = body
    files["portal_description.md"] = _portal_description(page_html, ds).encode()

    drop = check_drop_columns(ds, list(files))
    records = []
    for fname, data in files.items():
        record = {
            "key": ds["key"],
            "dataset_id": ds["dataset_id"],
            "atch_file_id": file_id,
            "file": f"{ds['key']}/{fname}",
        }
        if drop and fname.lower().endswith(".csv"):
            record["source_sha256"] = hashlib.sha256(data).hexdigest()
            record["dropped_columns"] = drop
            data = drop_csv_columns(data, drop)
        (out_dir / fname).write_bytes(data)
        record |= {
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "retrieved": date.today().isoformat(),
        }
        records.append(record)
        print(f"  {ds['key']}/{fname}  {len(data):,} bytes")
    return records


def main(argv: list[str]) -> None:
    sources = yaml.safe_load((ROOT / "data" / "sources.yaml").read_text())["datasets"]
    wanted = set(argv) or {d["key"] for d in sources}
    manifest_path = RAW / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else []
    manifest = [r for r in manifest if r["key"] not in wanted]
    for ds in sources:
        if ds["key"] in wanted:
            print(f"[{ds['key']}] {ds['title']}")
            manifest.extend(fetch(ds))
    manifest.sort(key=lambda r: r["file"])
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
