"""
상장사 감사보고서 전수 인덱서 (2단계).

  python indexer.py --year 2025 [--mode both|api|viewer] [--markets Y,K,N] [--workers 1] [--interval 1.0] [--no-cfs] [--limit 20] [--redo]

1단계 api   : OpenDART list.json(A001 사업보고서, 전체회사)으로 해당 사업연도 사업보고서 전수 수집(시장구분 포함) 후,
              accnutAdtorNmNdAdtOpinion(회계감사인의 명칭 및 감사의견)을 회사당 1회 호출 → 감사인·감사의견·강조사항·핵심감사사항.
              한 번의 호출이 당기·전기·전전기 3개 연도를 주므로 FY-1, FY-2도 함께 채워진다. 2,700사 ≈ 10분. (공식 API, 차단 위험 없음)
2단계 viewer: 각 사업보고서 첨부 '감사보고서'/'연결감사보고서' 본문을 DART 뷰어에서 읽어 감사의견·KAM·강조/기타사항·계속기업·
              내부회계관리제도 의견을 문단 단위로 구조화. 회사당 요청 6~7건, 기본 1초 간격·1스레드 → 약 4~5시간. 연속 실패 시 자동 대기(차단 회피).

결과: data/audit_index.sqlite → MCP 도구 screen_audit_reports / audit_index_status
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server as s  # noqa: E402

DB = s.DATA_DIR / "audit_index.sqlite"
LOCK = s.DATA_DIR / "indexer.lock"

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_index (
  corp_code TEXT, corp_name TEXT, stock_code TEXT, corp_cls TEXT, bsns_year INTEGER, fy_end TEXT, fs_div TEXT,
  rcept_no TEXT, dcm_no TEXT, rcept_dt TEXT, report_nm TEXT,
  auditor TEXT, opinion TEXT, kam_count INTEGER, kam_titles TEXT,
  has_emphasis INTEGER, emphasis_text TEXT, has_other INTEGER, other_text TEXT,
  going_concern INTEGER, gc_text TEXT,
  ic_type TEXT, ic_opinion TEXT, ic_material_weakness INTEGER, ic_text TEXT,
  report_text TEXT, status TEXT, error TEXT, indexed_at TEXT, source TEXT,
  PRIMARY KEY (corp_code, bsns_year, fs_div)
);
CREATE INDEX IF NOT EXISTS ix_audit_year ON audit_index (bsns_year, fs_div, corp_cls);
CREATE TABLE IF NOT EXISTS runs (
  bsns_year INTEGER PRIMARY KEY, started TEXT, updated TEXT, total INTEGER, done INTEGER, errors INTEGER, status TEXT, note TEXT
);
"""
COLS = ["corp_code", "corp_name", "stock_code", "corp_cls", "bsns_year", "fy_end", "fs_div", "rcept_no", "dcm_no", "rcept_dt", "report_nm",
        "auditor", "opinion", "kam_count", "kam_titles", "has_emphasis", "emphasis_text", "has_other", "other_text", "going_concern", "gc_text",
        "ic_type", "ic_opinion", "ic_material_weakness", "ic_text", "report_text", "status", "error", "indexed_at", "source"]


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB, timeout=60, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(audit_index)")}
    if "source" not in cols:
        con.execute("ALTER TABLE audit_index ADD COLUMN source TEXT")
        con.execute("UPDATE audit_index SET source='viewer' WHERE status='ok'")
        con.commit()
    return con


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def upsert(con: sqlite3.Connection, row: dict) -> None:
    con.execute(f"INSERT OR REPLACE INTO audit_index ({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))})", [row.get(c) for c in COLS])


def set_run(con: sqlite3.Connection, year: int, **kw) -> None:
    con.execute("INSERT OR IGNORE INTO runs (bsns_year, started, updated, total, done, errors, status, note) VALUES (?,?,?,0,0,0,'',' ')",
                (year, now(), now()))
    kw["updated"] = now()
    con.execute(f"UPDATE runs SET {', '.join(k + '=?' for k in kw)} WHERE bsns_year=?", list(kw.values()) + [year])
    con.commit()


# ------------------------------------------------------------------ 사업보고서 수집
def quarter_windows(year: int) -> list[tuple[str, str]]:
    """사업연도 year의 사업보고서가 접수될 수 있는 기간: year.01 ~ (year+1).12 를 3개월 창으로."""
    out = []
    for y in (year, year + 1):
        for m in (1, 4, 7, 10):
            a = dt.date(y, m, 1)
            b = dt.date(y + (1 if m == 10 else 0), 1 if m == 10 else m + 3, 1) - dt.timedelta(days=1)
            if a > dt.date.today():
                break
            out.append((a.strftime("%Y%m%d"), min(b, dt.date.today()).strftime("%Y%m%d")))
    return out


def collect_reports(year: int, markets: set[str]) -> list[dict]:
    reps: dict[str, dict] = {}
    tag = f"({year}."
    for bgn, end in quarter_windows(year):
        page = 1
        while True:
            d = s.api("list", bgn_de=bgn, end_de=end, pblntf_detail_ty="A001", page_no=page, page_count=100, last_reprt_at="N")
            for x in d.get("list", []):
                if x.get("corp_cls") not in markets or tag not in x.get("report_nm", ""):
                    continue
                cur = reps.get(x["corp_code"])
                if cur is None or (x["rcept_dt"], x["rcept_no"]) > (cur["rcept_dt"], cur["rcept_no"]):
                    reps[x["corp_code"]] = x
            if page >= int(d.get("total_page", 1) or 1):
                break
            page += 1
        log(f"  {bgn}~{end}: 누적 {len(reps)}개 회사")
    return sorted(reps.values(), key=lambda x: x["corp_name"])


def base_row(rep: dict, year: int) -> dict:
    return {
        "corp_code": rep["corp_code"], "corp_name": rep["corp_name"], "stock_code": rep.get("stock_code", ""), "corp_cls": rep["corp_cls"],
        "bsns_year": year, "fy_end": (rep["report_nm"].split("(")[-1].rstrip(")") if "(" in rep["report_nm"] else ""),
        "rcept_no": rep["rcept_no"], "rcept_dt": rep["rcept_dt"], "report_nm": rep["report_nm"], "indexed_at": now(),
    }


# ------------------------------------------------------------------ 1단계: OpenDART API
def _norm_opinion(v: str) -> str:
    t = re.sub(r"\s+", "", v or "")
    for k in ("의견거절", "부적정", "한정", "적정"):
        if k in t:
            return k
    return t or "-"


def _is_none(v: str) -> bool:
    t = re.sub(r"[\s.]", "", v or "")
    return t in ("", "-", "해당사항없음", "해당없음", "없음", "해당사항없읍")


def api_phase(con: sqlite3.Connection, reps: list[dict], year: int, lock: threading.Lock) -> int:
    """회사당 1회 API 호출. 당기/전기/전전기 → year, year-1, year-2. 뷰어 파싱(ok) 행은 덮어쓰지 않는다."""
    s.MIN_INTERVAL = 0.2
    existing_ok = {(r[0], r[1], r[2]) for r in con.execute("SELECT corp_code, bsns_year, fs_div FROM audit_index WHERE status='ok'")}
    errors = 0
    t0 = time.time()
    for i, rep in enumerate(reps, 1):
        try:
            rows = s.api("accnutAdtorNmNdAdtOpinion", corp_code=rep["corp_code"], bsns_year=str(year), reprt_code="11011")["list"]
        except Exception as e:  # noqa: BLE001
            errors += 1
            with lock:
                upsert(con, {**base_row(rep, year), "fs_div": "OFS", "status": "error", "error": f"API: {e}"[:300], "source": "api"})
            continue
        seen: dict[int, int] = {}
        for r in rows:
            label = re.sub(r"\s+", "", r.get("bsns_year", ""))
            if "전전기" in label:
                y = year - 2
            elif "전기" in label:
                y = year - 1
            elif "당기" in label:
                y = year
            else:
                continue
            k = seen.get(y, 0)
            fs_div = "OFS" if k == 0 else "CFS"  # 같은 연도에 두 행이 오면 두 번째는 연결로 간주
            seen[y] = k + 1
            if k > 1 or (rep["corp_code"], y, fs_div) in existing_ok:
                continue
            emph = r.get("emphs_matter") or r.get("adt_reprt_spcmnt_matter") or ""
            kam = r.get("core_adt_matter") or ""
            has_emph = not _is_none(emph)
            has_kam = not _is_none(kam)
            kam_n = len(re.findall(r"(?:^|\s)\d+[.)]", kam)) if has_kam else 0
            row = {
                **base_row(rep, y), "bsns_year": y, "fs_div": fs_div, "source": "api", "status": "api", "error": "",
                "fy_end": f"{y}.{rep['report_nm'].split('.')[-1].rstrip(')') if '.' in rep['report_nm'] else '12'}" if y != year else base_row(rep, year)["fy_end"],
                "auditor": re.sub(r"\s+", "", r.get("adtor") or ""), "opinion": _norm_opinion(r.get("adt_opinion", "")),
                "kam_count": (kam_n or 1) if has_kam else 0, "kam_titles": kam.strip() if has_kam else "",
                "has_emphasis": int(has_emph), "emphasis_text": emph.strip() if has_emph else "",
                "has_other": None, "other_text": "", "going_concern": int(has_emph and "계속기업" in emph), "gc_text": emph.strip() if (has_emph and "계속기업" in emph) else "",
                "ic_type": None, "ic_opinion": None, "ic_material_weakness": None, "ic_text": "", "report_text": "",
            }
            if y != year:  # 전기/전전기 행은 당해 사업보고서 접수번호가 아니므로 비워 둔다
                row.update({"rcept_no": "", "dcm_no": "", "rcept_dt": "", "report_nm": f"(FY{year} 사업보고서의 {label} 기재)"})
            with lock:
                upsert(con, row)
        if i % 100 == 0 or i == len(reps):
            con.commit()
            set_run(con, year, done=i, errors=errors, status="api")
            el = time.time() - t0
            log(f"  [api] {i}/{len(reps)} 처리, 오류 {errors}, 경과 {el / 60:.1f}분, 남은 예상 {(len(reps) - i) * el / i / 60:.1f}분")
    con.commit()
    return errors


# ------------------------------------------------------------------ 2단계: DART 뷰어 본문
def analyze(rcept_no: str, dcm_no: str) -> dict:
    doc = s.fetch_toc(rcept_no, dcm_no)
    nodes = doc["nodes"]
    if not nodes:
        raise s.DartError("목차 없음")
    rep_nodes = [n for n in nodes if "독립된" in n["text"] or ("감사인" in n["text"] and "보고서" in n["text"])] or \
                [n for n in nodes if n["depth"] == 1 and "주석" not in n["text"] and "재무제표" not in n["text"]][:2]
    report_text = s.doc_text(rep_nodes)
    info = s.summarize_audit_text(report_text)
    secs = info["sections"]
    join = lambda pred: "\n\n".join(f"[{h}]\n{b}" for h, b in secs if pred(h))
    ic_nodes = [n for n in nodes if "내부회계" in n["text"]]
    ic_text = s.doc_text(ic_nodes) if ic_nodes else ""
    ic_type, ic_op, ic_mw = s.classify_internal_control(ic_text)
    gc_text = join(lambda h: "계속기업" in h)
    if not gc_text and info["going_concern_uncertainty"]:
        gc_text = join(lambda h: h.startswith(("강조사항", "감사의견", "의견거절", "한정의견")) and "근거" not in h)
    return {
        "dcm_no": dcm_no, "auditor": s.extract_auditor(report_text), "opinion": info["opinion"],
        "kam_count": len(info["kam_titles"]) if info["kam_titles"] else (1 if info["has_kam"] else 0),
        "kam_titles": " | ".join(info["kam_titles"]),
        "has_emphasis": int(info["has_emphasis"]), "emphasis_text": join(lambda h: h.startswith("강조사항")),
        "has_other": int(info["has_other_matter"]), "other_text": join(lambda h: h.startswith("기타사항")),
        "going_concern": int(info["going_concern_uncertainty"]), "gc_text": gc_text,
        "ic_type": ic_type, "ic_opinion": ic_op, "ic_material_weakness": int(ic_mw), "ic_text": ic_text[:20000],
        "report_text": report_text[:60000], "source": "viewer",
    }


def process(rep: dict, year: int, do_cfs: bool) -> list[dict]:
    base = base_row(rep, year)
    rows = []
    try:
        toc = s.fetch_toc(rep["rcept_no"])
    except Exception as e:  # noqa: BLE001
        return [{**base, "fs_div": fd, "status": "error", "error": f"사업보고서 목차 실패: {e}"[:300], "source": "viewer"}
                for fd in (["OFS", "CFS"] if do_cfs else ["OFS"])]
    for fs_div, title in (("OFS", "감사보고서"), ("CFS", "연결감사보고서")):
        if fs_div == "CFS" and not do_cfs:
            continue
        att = next((a for a in toc["attachments"] if s._norm(a["title"]) == s._norm(title)), None)
        if not att:
            rows.append({**base, "fs_div": fs_div, "status": "no_attachment", "source": "viewer",
                         "error": "첨부 없음: " + ", ".join(a["title"] for a in toc["attachments"])[:300]})
            continue
        try:
            rows.append({**base, "fs_div": fs_div, "status": "ok", "error": "", **analyze(att["rcept_no"], att["dcm_no"])})
        except Exception as e:  # noqa: BLE001
            rows.append({**base, "fs_div": fs_div, "dcm_no": att["dcm_no"], "status": "error", "error": str(e)[:300], "source": "viewer"})
    return rows


def viewer_reachable() -> bool:
    try:
        return s._get(f"{s.DART}/dsaf001/main.do?rcpNo=20260310002820", retries=1).status_code == 200
    except Exception:  # noqa: BLE001
        return False


def viewer_phase(con: sqlite3.Connection, reps: list[dict], year: int, lock: threading.Lock, workers: int, interval: float,
                 do_cfs: bool, redo: bool) -> tuple[int, int, str]:
    s.MIN_INTERVAL = interval
    done_keys = set() if redo else {
        (r[0], r[1]) for r in con.execute("SELECT corp_code, fs_div FROM audit_index WHERE bsns_year=? AND status='ok'", (year,))}
    # 기존에 'api'로만 채워진 행은 뷰어 결과로 승격되며, 전기/전전기 API 행은 그대로 둔다.
    todo = [r for r in reps if (r["corp_code"], "OFS") not in done_keys or (do_cfs and (r["corp_code"], "CFS") not in done_keys)]
    log(f"[viewer] 대상 {len(todo)}건 (이미 완료 {len(reps) - len(todo)}건), 간격 {interval}s, 스레드 {workers}")
    done = len(reps) - len(todo)
    errors = 0
    consecutive_fail = 0
    pauses = 0
    t0 = time.time()
    status = "done"
    i = 0
    idx = 0
    while idx < len(todo):
        batch = todo[idx: idx + max(workers, 1) * 5]
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(process, r, year, do_cfs): r for r in batch}
            for f in as_completed(futs):
                i += 1
                rep = futs[f]
                try:
                    rows = f.result()
                except Exception:  # noqa: BLE001
                    rows = [{**base_row(rep, year), "fs_div": "OFS", "status": "error", "error": traceback.format_exc()[-300:], "source": "viewer"}]
                disconnected = all(r.get("status") == "error" and ("disconnected" in (r.get("error") or "") or "요청 실패" in (r.get("error") or ""))
                                   for r in rows)
                consecutive_fail = consecutive_fail + 1 if disconnected else 0
                with lock:
                    for row in rows:
                        if row.get("status") == "error":
                            errors += 1
                        upsert(con, row)
                    done += 1
                    if i % 10 == 0 or i == len(todo):
                        set_run(con, year, done=done, errors=errors, status="viewer")
                    if i % 50 == 0:
                        s._text_cache.clear()
                        s._toc_cache.clear()
                        el = time.time() - t0
                        log(f"  [viewer] {done}/{len(reps)} 처리, 오류 {errors}, 경과 {el / 60:.1f}분, 남은 예상 {(len(todo) - i) * el / i / 60:.1f}분")
        idx += len(batch)
        con.commit()
        if consecutive_fail >= 6:
            pauses += 1
            if pauses > 3:
                status = "blocked"
                log("[viewer] DART 뷰어 접속이 계속 거부됩니다(IP 차단 추정). 2단계를 중단합니다. 몇 시간 뒤 다시 실행하면 이어서 진행됩니다.")
                break
            log(f"[viewer] 연속 실패 {consecutive_fail}건 → DART 차단 회피를 위해 15분 대기 ({pauses}/3)")
            set_run(con, year, status="viewer-paused")
            time.sleep(900)
            consecutive_fail = 0
    return done, errors, status


# ------------------------------------------------------------------ main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--mode", choices=["both", "api", "viewer"], default="both")
    ap.add_argument("--markets", default="Y,K,N", help="Y유가 K코스닥 N코넥스 E기타")
    ap.add_argument("--workers", type=int, default=1, help="뷰어 단계 스레드 수(권장 1)")
    ap.add_argument("--interval", type=float, default=1.0, help="뷰어 요청 최소 간격(초)")
    ap.add_argument("--no-cfs", action="store_true", help="연결감사보고서 생략")
    ap.add_argument("--limit", type=int, default=0, help="테스트용: 앞 N개 회사만")
    ap.add_argument("--redo", action="store_true", help="이미 ok인 항목도 다시")
    a = ap.parse_args()

    if LOCK.exists():
        try:
            pid = int(LOCK.read_text().strip() or 0)
        except ValueError:
            pid = 0
        if pid and str(pid) in os.popen(f'tasklist /FI "PID eq {pid}" /NH').read():
            sys.exit(f"다른 인덱서(pid {pid})가 실행 중입니다.")
    LOCK.write_text(str(os.getpid()))
    con = db()
    lock = threading.Lock()
    try:
        markets = set(m.strip().upper() for m in a.markets.split(","))
        log(f"FY{a.year} 사업보고서 수집 (시장 {','.join(sorted(markets))}, 모드 {a.mode})")
        set_run(con, a.year, started=now(), status="collecting", note="")
        reps = collect_reports(a.year, markets)
        if a.limit:
            reps = reps[: a.limit]
        set_run(con, a.year, total=len(reps))
        log(f"사업보고서 {len(reps)}건")
        t0 = time.time()
        api_err = 0
        if a.mode in ("both", "api"):
            api_err = api_phase(con, reps, a.year, lock)
            n_api = con.execute("SELECT COUNT(*) FROM audit_index WHERE bsns_year=? AND status IN ('api','ok')", (a.year,)).fetchone()[0]
            log(f"[api] 완료: FY{a.year} {n_api}건 (전기·전전기 포함 추가 적재), 오류 {api_err}, 소요 {(time.time() - t0) / 60:.1f}분")
            set_run(con, a.year, status="api-done", done=len(reps), errors=api_err)
        if a.mode in ("both", "viewer"):
            if not viewer_reachable():
                log("[viewer] DART 뷰어(dart.fss.or.kr)에 접속할 수 없습니다(차단 또는 점검). 2단계는 건너뜁니다. 나중에 --mode viewer 로 다시 실행하세요.")
                set_run(con, a.year, status="api-done (viewer unreachable)")
            else:
                done, errors, status = viewer_phase(con, reps, a.year, lock, a.workers, a.interval, not a.no_cfs, a.redo)
                set_run(con, a.year, status=status, done=done, errors=errors)
                n_ok = con.execute("SELECT COUNT(*) FROM audit_index WHERE bsns_year=? AND status='ok'", (a.year,)).fetchone()[0]
                log(f"[viewer] {status}: FY{a.year} 본문 인덱스 {n_ok}건(별도+연결), 오류 {errors}, 총 소요 {(time.time() - t0) / 60:.1f}분")
    except Exception:
        set_run(con, a.year, status="failed", note=traceback.format_exc()[-500:])
        raise
    finally:
        LOCK.unlink(missing_ok=True)
        con.close()


if __name__ == "__main__":
    main()
