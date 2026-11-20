"""
스크리닝 인덱스 DB를 배포용 zip으로 내보낸다 (GitHub Release 자산용).

  python scripts/export_index.py [--db <경로>] [--out <폴더>]

인덱서가 실행 중이어도 sqlite 백업 API로 일관된 스냅샷을 만든다. 오류 행(status='error')은 제외하고 VACUUM 해 크기를 줄인다.
출력: <out>/audit_index.sqlite.zip  (download_audit_index 도구가 받는 파일명)
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as s  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(s.DATA_DIR / "audit_index.sqlite"))
    ap.add_argument("--out", default=str(s.DATA_DIR / "release"))
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    snap = out / "audit_index.sqlite"
    snap.unlink(missing_ok=True)

    src = sqlite3.connect(a.db, timeout=60)
    dst = sqlite3.connect(snap)
    src.backup(dst)
    src.close()
    dst.execute("DELETE FROM audit_index WHERE status='error'")
    dst.execute("DELETE FROM runs WHERE status NOT IN ('done','api-done','blocked') AND status NOT LIKE 'api-done%'")
    dst.commit()
    dst.execute("VACUUM")
    stats = dst.execute("SELECT bsns_year, source, COUNT(*) FROM audit_index WHERE status IN ('ok','api') GROUP BY bsns_year, source ORDER BY 1 DESC, 2").fetchall()
    dst.close()

    zpath = out / "audit_index.sqlite.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.write(snap, "audit_index.sqlite")
        z.writestr("README.txt", f"OpenDART MCP 스크리닝 인덱스 스냅샷 {dt.date.today()}\n" +
                   "\n".join(f"FY{y} {src_}: {n}건" for y, src_, n in stats) + "\n")
    snap.unlink()
    print(f"{zpath}  ({os.path.getsize(zpath) / 1e6:.1f} MB)")
    for y, src_, n in stats:
        print(f"  FY{y} {src_}: {n}")


if __name__ == "__main__":
    main()
