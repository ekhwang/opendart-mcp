"""
OpenDART MCP server for Claude Code.

금융감독원 OpenDART API + DART 전자공시 뷰어를 결합해
회사 검색(고유번호 변환), 공시목록, 재무제표, 감사인/감사의견 요약,
감사보고서 본문(핵심감사사항·강조사항·계속기업·내부회계) 열람을 제공한다.

환경변수: OPENDART_API_KEY (필수), OPENDART_DATA_DIR (선택, 캐시 폴더)
"""
from __future__ import annotations

import datetime as dt
import difflib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

import logging

import httpx
from bs4 import BeautifulSoup
try:
    from mcp.server.mcpserver import MCPServer as FastMCP  # mcp >= 2.x
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP

API_KEY = os.environ.get("OPENDART_API_KEY", "").strip()
BASE = "https://opendart.fss.or.kr/api"
DART = "https://dart.fss.or.kr"
_HERE = Path(__file__).resolve().parent
# 데이터 폴더: 환경변수 > (기존 설치 호환) 서버 폴더의 data > 사용자 홈의 .opendart-mcp/data (플러그인 설치 시 기본)
DATA_DIR = Path(os.environ.get("OPENDART_DATA_DIR") or ((_HERE / "data") if (_HERE / "data").is_dir() else Path.home() / ".opendart-mcp" / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
# 미리 구축된 상장사 감사보고서 인덱스(sqlite zip) 배포 위치. GitHub Release 최신 자산.
INDEX_RELEASE_URL = os.environ.get("OPENDART_INDEX_URL") or "https://github.com/ekhwang/opendart-mcp/releases/latest/download/audit_index.sqlite.zip"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}
http = httpx.Client(timeout=httpx.Timeout(60.0, connect=20.0), headers=HEADERS, follow_redirects=True,
                    limits=httpx.Limits(max_keepalive_connections=0, max_connections=8))  # DART는 keep-alive 재사용 시 연결을 끊는 경우가 많다
_rate_lock = __import__("threading").Lock()
_last_req = [0.0]
MIN_INTERVAL = float(os.environ.get("OPENDART_MIN_INTERVAL", "0.35"))  # 전체 스레드 합산 요청 간 최소 간격(초)


def _throttle() -> None:
    with _rate_lock:
        wait = _last_req[0] + MIN_INTERVAL - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_req[0] = time.time()
logging.getLogger("httpx").setLevel(logging.WARNING)


def _get(url: str, retries: int = 6, **kw) -> httpx.Response:
    """GET with retry/backoff (DART viewer는 간헐적으로 5xx/429/타임아웃을 낸다)."""
    last: Exception | None = None
    for i in range(retries):
        try:
            _throttle()
            r = http.get(url, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
            return r
        except (httpx.HTTPError, httpx.TransportError) as e:
            last = e
            time.sleep(min(2.0 * (2 ** i), 60))
    raise DartError(f"요청 실패 {url}: {last}")

# 통칭·영문·과거 사명 → DART 등록 회사명 (corpCode.xml에는 현재 사명만 있음)
ALIASES = {
    "네이버": "NAVER", "naver": "NAVER", "카카오": "카카오", "엘지": "LG", "에스케이": "SK", "케이티": "KT", "kt&g": "케이티앤지",
    "현대차": "현대자동차", "기아차": "기아", "포스코": "POSCO홀딩스", "포스코홀딩스": "POSCO홀딩스", "삼바": "삼성바이오로직스",
    "두산중공업": "두산에너빌리티", "셀트리온헬스케어": "셀트리온", "엘지화학": "LG화학", "엘지전자": "LG전자", "에스케이하이닉스": "SK하이닉스",
    "sk하이닉스": "SK하이닉스", "하이닉스": "SK하이닉스", "삼성sds": "삼성에스디에스", "현대중공업": "HD현대중공업", "현대오일뱅크": "HD현대오일뱅크",
    "한국조선해양": "HD한국조선해양", "두산인프라코어": "HD현대인프라코어", "현대건설기계": "HD현대건설기계", "대우조선해양": "한화오션",
    "대우건설": "대우건설", "쿠팡": "쿠팡", "하나은행": "하나은행", "국민은행": "국민은행", "kb금융": "KB금융", "신한지주": "신한지주",
    "메리츠금융": "메리츠금융지주", "lg엔솔": "LG에너지솔루션", "엘지에너지솔루션": "LG에너지솔루션", "삼성화재": "삼성화재해상보험",
    "삼성생명": "삼성생명보험", "한화생명": "한화생명보험", "교보생명": "교보생명보험", "동부화재": "DB손해보험", "현대해상": "현대해상화재보험",
    "sk텔레콤": "SK텔레콤", "skt": "SK텔레콤", "kt": "KT", "lg유플러스": "LG유플러스", "유플러스": "LG유플러스", "크래프톤": "크래프톤",
    "엔씨": "엔씨소프트", "넷마블": "넷마블", "하이브": "하이브", "대한항공": "대한항공", "아시아나": "아시아나항공", "에스오일": "S-Oil",
    "s오일": "S-Oil", "gs칼텍스": "GS칼텍스", "지에스칼텍스": "GS칼텍스", "한진칼": "한진칼", "cj": "CJ", "씨제이": "CJ", "이마트": "이마트",
    "신세계": "신세계", "롯데쇼핑": "롯데쇼핑", "오리온": "오리온", "농심": "농심", "셀트리온": "셀트리온", "알테오젠": "알테오젠",
    "에코프로": "에코프로", "포스코퓨처엠": "포스코퓨처엠", "포스코케미칼": "포스코퓨처엠", "삼성물산": "삼성물산", "제일모직": "삼성물산",
    "한화": "한화", "두산": "두산", "효성": "효성", "코오롱": "코오롱", "동국제강": "동국홀딩스", "kcc": "KCC", "lg디스플레이": "LG디스플레이",
    "엘지디스플레이": "LG디스플레이", "삼성디스플레이": "삼성디스플레이", "sk이노베이션": "SK이노베이션", "sk온": "에스케이온", "삼성sdi": "삼성SDI",
}

INSTRUCTIONS = """
DART(금융감독원 전자공시) 조회 도구. 회사는 이름·종목코드·DART 고유번호 중 무엇으로든 지정할 수 있다(내부에서 변환).

도메인 지식(라우팅 규칙):
- 사업보고서(A001)는 상장법인 등 '사업보고서 제출대상법인'만 제출한다. 상장사의 감사보고서·연결감사보고서·내부회계관리제도운영보고서는
  사업보고서의 '첨부서류'로 공시되므로 get_audit_report / list_document_toc(사업보고서 rcept_no)로 접근한다.
- 비상장 외부감사대상 회사는 감사보고서(F001)/연결감사보고서(F002)를 별도 공시한다(결산일 후 약 4개월 내). get_audit_report가 자동으로 이 경로를 탄다.
- 12월 결산법인의 사업보고서는 다음 해 3월 말까지 제출된다. 결산월이 다르면 결산일 + 90일.
- reprt_code: 11011 사업보고서(연간), 11012 반기보고서, 11013 1분기보고서, 11014 3분기보고서. fs_div: CFS 연결, OFS 별도.
- 내부회계관리제도: 상장사는 외부감사인의 '감사'(자산 1천억 미만 소규모 상장사는 유예로 '검토'), 비상장 외감대상은 '검토'. 본문의 의견 문단을 읽어 판별할 것.
- OpenDART 일일 호출 한도 20,000건. 조회 결과 없음(013)은 오류가 아니라 '해당 데이터 없음'이다.
- 조건별 스크리닝(예: 올해 의견거절 회사 전체)은 OpenDART로는 불가능하다. 대신 search_disclosures를 corp 없이 기간+유형(F001 등)으로 돌려 목록을 얻고 개별 조회하라(기간 3개월 제한).

권장 순서: search_company → (search_disclosures | get_financials | get_audit_summary | get_audit_report | read_document).
"""

mcp = FastMCP("opendart", instructions=INSTRUCTIONS)


class DartError(Exception):
    pass


STATUS_MSG = {
    "010": "등록되지 않은 인증키", "011": "사용할 수 없는 인증키", "012": "접근할 수 없는 IP",
    "013": "조회된 데이터 없음", "014": "파일이 존재하지 않음", "020": "요청 제한(일 20,000건) 초과",
    "021": "조회 가능한 회사 개수 초과(최대 100건)", "100": "필드 값이 부적절", "101": "부적절한 접근",
    "800": "시스템 점검 중", "900": "정의되지 않은 오류", "901": "인증키 사용기한 만료",
}


def api(endpoint: str, **params) -> dict:
    if not API_KEY:
        raise DartError("OPENDART_API_KEY 환경변수가 설정되지 않았습니다.")
    p = {"crtfc_key": API_KEY}
    p.update({k: v for k, v in params.items() if v not in (None, "")})
    r = _get(f"{BASE}/{endpoint}.json", params=p)
    r.raise_for_status()
    d = r.json()
    st = d.get("status")
    if st == "013":
        return {"status": "013", "message": d.get("message"), "list": []}
    if st != "000":
        raise DartError(f"OpenDART 오류 {st} ({STATUS_MSG.get(st, '')}): {d.get('message')}")
    return d


# ---------------------------------------------------------------- corp codes
def _norm(s: str) -> str:
    s = s or ""
    s = re.sub(r"주식회사|\(주\)|㈜|유한회사|\(유\)", "", s)
    return re.sub(r"[^0-9a-zA-Z가-힣&]", "", s).lower()


def load_corp_codes(force: bool = False) -> list[dict]:
    f = DATA_DIR / "corp_codes.json"
    if not force and f.exists() and time.time() - f.stat().st_mtime < 7 * 86400:
        return json.loads(f.read_text("utf-8"))
    r = http.get(f"{BASE}/corpCode.xml", params={"crtfc_key": API_KEY})
    r.raise_for_status()
    try:
        z = zipfile.ZipFile(io.BytesIO(r.content))
    except zipfile.BadZipFile:
        raise DartError("corpCode.xml 다운로드 실패: " + r.text[:300])
    root = ET.fromstring(z.read(z.namelist()[0]))
    rows = []
    for el in root.iter("list"):
        rows.append({
            "corp_code": (el.findtext("corp_code") or "").strip(),
            "corp_name": (el.findtext("corp_name") or "").strip(),
            "corp_eng_name": (el.findtext("corp_eng_name") or "").strip(),
            "stock_code": (el.findtext("stock_code") or "").strip(),
            "modify_date": (el.findtext("modify_date") or "").strip(),
        })
    f.write_text(json.dumps(rows, ensure_ascii=False), "utf-8")
    return rows


def find_corps(query: str, limit: int = 10, listed_only: bool = False) -> list[dict]:
    rows = load_corp_codes()
    q = (query or "").strip()
    if re.fullmatch(r"\d{8}", q):
        return [r for r in rows if r["corp_code"] == q]
    if re.fullmatch(r"[0-9A-Za-z]{6}", q) and any(c.isdigit() for c in q):
        hits = [r for r in rows if r["stock_code"] == q.upper()]
        if hits:
            return hits
    nq = _norm(ALIASES.get(_norm(q), q))
    if not nq:
        return []
    exact, prefix, contains, eng = [], [], [], []
    for r in rows:
        if listed_only and not r["stock_code"]:
            continue
        n = _norm(r["corp_name"])
        if n == nq:
            exact.append(r)
        elif n.startswith(nq):
            prefix.append(r)
        elif nq in n:
            contains.append(r)
        elif r["corp_eng_name"] and nq in _norm(r["corp_eng_name"]):
            eng.append(r)
    tiers = [(0, exact), (1, prefix), (2, contains), (3, eng)]
    scored = [((0 if r["stock_code"] else 1, t, len(r["corp_name"]), r["corp_name"]), r) for t, grp in tiers for r in grp]
    out = [r for _, r in sorted(scored, key=lambda x: x[0])]
    if not out:
        names = {}
        for r in rows:
            if listed_only and not r["stock_code"]:
                continue
            names.setdefault(_norm(r["corp_name"]), r)
        for m in difflib.get_close_matches(nq, list(names.keys()), n=limit, cutoff=0.6):
            out.append(names[m])
    return out[:limit]


def resolve(corp: str) -> dict:
    hits = find_corps(corp, limit=5)
    if not hits:
        raise DartError(f"'{corp}'에 해당하는 회사를 찾을 수 없습니다. search_company로 먼저 확인하세요.")
    r = dict(hits[0])
    if len(hits) > 1 and _norm(hits[0]["corp_name"]) != _norm(ALIASES.get(_norm(corp), corp)):
        r["_note"] = "후보 여러 개 중 첫 번째를 사용: " + ", ".join(
            f"{h['corp_name']}({h['corp_code']}{',' + h['stock_code'] if h['stock_code'] else ''})" for h in hits)
    return r


def _fmt_corp(r: dict) -> str:
    mk = "상장" if r.get("stock_code") else "비상장"
    s = f"{r['corp_name']} | corp_code={r['corp_code']} | {mk}"
    if r.get("stock_code"):
        s += f" | 종목코드={r['stock_code']}"
    if r.get("corp_eng_name"):
        s += f" | {r['corp_eng_name']}"
    return s


def _amt(v) -> str:
    if v in (None, "", "-"):
        return "-"
    try:
        return f"{int(str(v).replace(',', '')):,}"
    except ValueError:
        return str(v)


def _dedupe(rows: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in rows:
        k = json.dumps(r, sort_keys=True, ensure_ascii=False)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def _md_table(rows: list[dict], cols: list[tuple[str, str]], amount_cols: set[str] = frozenset()) -> str:
    rows = _dedupe(rows)
    if not rows:
        return "(데이터 없음)"
    head = "| " + " | ".join(c[1] for c in cols) + " |\n|" + "---|" * len(cols) + "\n"
    body = ""
    for r in rows:
        cells = []
        for k, _ in cols:
            v = r.get(k, "")
            v = _amt(v) if k in amount_cols else str(v if v is not None else "").replace("|", "/").replace("\n", " ")
            cells.append(v)
        body += "| " + " | ".join(cells) + " |\n"
    return head + body


# ---------------------------------------------------------------- DART viewer
_toc_cache: dict[str, dict] = {}
_text_cache: dict[str, str] = {}


def fetch_toc(rcept_no: str, dcm_no: Optional[str] = None) -> dict:
    key = f"{rcept_no}:{dcm_no or ''}"
    if key in _toc_cache:
        return _toc_cache[key]
    url = f"{DART}/dsaf001/main.do?rcpNo={rcept_no}" + (f"&dcmNo={dcm_no}" if dcm_no else "")
    html = _get(url).text
    nodes = []
    for m in re.finditer(r"var (node\d+) = \{\};(.*?)(?=var node\d+ = \{\};|function |</script>)", html, re.S):
        var, body = m.group(1), m.group(2)
        d = dict(re.findall(var + r"\['(\w+)'\]\s*=\s*\"((?:[^\"\\]|\\.)*)\"", body))
        if "text" in d and d.get("rcpNo"):
            d["depth"] = int(var[4:])
            d["text"] = re.sub(r"\s+", " ", d["text"]).strip()
            nodes.append(d)
    atts = []
    m = re.search(r'<select[^>]*id="att"[^>]*>(.*?)</select>', html, re.S)
    if m:
        for v, t in re.findall(r'<option[^>]*value="([^"]*)"[^>]*>(.*?)</option>', m.group(1), re.S):
            v = v.replace("&amp;", "&")
            mm = re.search(r"rcpNo=(\d+)&dcmNo=(\d+)", v)
            if mm:
                title = re.sub(r"\s+", " ", t.replace("&nbsp;", " ")).strip()
                title = re.sub(r"^\d{4}\.\d{2}\.\d{2}\s*", "", title)
                atts.append({"rcept_no": mm.group(1), "dcm_no": mm.group(2), "title": title})
    tm = re.search(r"<title>(.*?)</title>", html, re.S)
    doc = {"url": url, "title": re.sub(r"\s+", " ", tm.group(1)).strip() if tm else "", "nodes": nodes, "attachments": atts}
    if not nodes:
        doc["error"] = "목차를 찾지 못했습니다(접수번호 오류 또는 DART 페이지 구조 변경)."
    _toc_cache[key] = doc
    return doc


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for t in soup.find_all("table"):
        if t.find_parent("table") is not None:
            continue
        rows = []
        for tr in t.find_all("tr"):
            cells = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"])]
            if any(cells):
                rows.append("| " + " | ".join(cells) + " |")
        t.replace_with(soup.new_string("\n" + "\n".join(rows) + "\n"))
    text = soup.get_text("\n").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def node_text(n: dict) -> str:
    url = (f"{DART}/report/viewer.do?rcpNo={n['rcpNo']}&dcmNo={n['dcmNo']}&eleId={n['eleId']}"
           f"&offset={n['offset']}&length={n['length']}&dtd={n['dtd']}")
    t = _text_cache.get(url)
    if t is None:
        t = html_to_text(_get(url).text)
        _text_cache[url] = t
    return t


def select_nodes(nodes: list[dict], section: Optional[str]) -> list[dict]:
    if not section:
        return nodes
    s = section.strip()
    for i, n in enumerate(nodes):
        if s == n.get("id") or _norm(s) in _norm(n["text"]):
            out = [n]
            for m in nodes[i + 1:]:
                if m["depth"] <= n["depth"]:
                    break
                out.append(m)
            return out
    return []


def doc_text(nodes: list[dict]) -> str:
    parts = []
    for n in nodes:
        t = node_text(n)
        if t:
            parts.append(f"\n## {n['text']}\n{t}")
    return "\n".join(parts).strip()


def snippets(text: str, query: str, context: int = 400, max_hits: int = 15) -> str:
    terms = [t for t in re.split(r"[,\s]+", query) if t]
    out, used = [], []
    for term in terms:
        for m in re.finditer(re.escape(term), text, re.I):
            a, b = max(0, m.start() - context), min(len(text), m.end() + context)
            if any(abs(a - u) < context for u in used):
                continue
            used.append(a)
            out.append(f"--- [{term}] @{m.start()} ---\n…{text[a:b]}…")
            if len(out) >= max_hits:
                return "\n".join(out)
    return "\n".join(out) if out else f"'{query}' 검색 결과 없음 (본문 {len(text):,}자)"


# ---------------------------------------------------------------- audit report parsing
HEADER_RE = re.compile(
    r"^(감사의견|한정의견|부적정의견|의견거절|검토의견|[가-힣 ]*의견\s*근거|계속기업\s*관련\s*(중요한\s*)?불확실성|강조사항|기타사항|핵심감사사항|"
    r"(연결)?재무제표에\s*대한\s*경영진과\s*지배기구의\s*책임|(연결)?재무제표\s*감사에\s*대한\s*감사인의\s*책임|감사인의\s*책임|경영진[가-힣 ]*책임|기타\s*보고\s*책임|"
    r"내부회계관리제도[가-힣 ]*의견|[가-힣 ]*내부회계관리제도[가-힣 ]*)(\s*[-–:：]\s*.*)?$")


def split_sections(text: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, list[str]]] = [("(서두)", [])]
    for line in text.split("\n"):
        s = line.strip()
        if 2 <= len(s) <= 60 and not s.endswith((".", "다", "|")) and HEADER_RE.match(s):
            sections.append((s, []))
        else:
            sections[-1][1].append(line)
    return [(h, "\n".join(b).strip()) for h, b in sections if "\n".join(b).strip() or h != "(서두)"]


def classify_opinion(text: str) -> str:
    t = re.sub(r"\s+", "", text or "")
    if "의견을표명하지아니" in t or "의견거절" in t:
        return "의견거절"
    if "부적정의견" in t or "공정하게표시하고있지아니" in t:
        return "부적정"
    if "한정의견" in t or ("제외하고" in t and "공정하게표시" in t):
        return "한정"
    if "공정하게표시하고있습니다" in t or "적정" in t:
        return "적정"
    return "판독불가"


def extract_kam_titles(body: str) -> tuple[list[str], bool]:
    """핵심감사사항 문단에서 개별 KAM 제목 추출. (제목 목록, 'KAM 없음' 선언 여부)"""
    lines = [l.strip() for l in body.split("\n") if l.strip()]
    flat_all = re.sub(r"\s+", "", body)
    if re.search(r"핵심감사사항이없다고|보고해야할핵심감사사항이없|핵심감사사항은없습니다|핵심감사사항이없습니다", flat_all):
        return [], True
    titles = []
    for i in range(len(lines) - 1):
        l, nxt = lines[i], lines[i + 1]
        l = re.sub(r"^[\(\[]?\d+[\)\].]?\s*|^[가-하]\.\s*|^[①-⑳]\s*", "", l).strip()
        flat = re.sub(r"\s+", "", l)
        if len(l) > 90 or l.startswith(("|", "·", "ㆍ", "-", "•", "※")):
            continue
        if flat.startswith("핵심감사사항") or re.search(r"(다|니다|습니다|음|함|됨)\.?$|[.:;,]$", flat):
            continue
        if "전문가적판단" in flat or "별도의의견" in flat or "감사절차는다음과" in flat:
            continue
        nflat = re.sub(r"\s+", "", nxt)
        if nflat.startswith("핵심감사사항으로결정") or (len(nxt) > 120 and not nxt.startswith("|")):
            titles.append(l)
    return titles, False


def summarize_audit_text(text: str) -> dict:
    secs = split_sections(text)
    op_txt = next((b for h, b in secs if h.startswith(("감사의견", "한정의견", "부적정의견", "의견거절")) and "근거" not in h), "")
    kam = [b for h, b in secs if h.startswith("핵심감사사항")]
    kam_titles: list[str] = []
    kam_none = False
    for b in kam:
        t, none = extract_kam_titles(b)
        kam_titles += t
        kam_none = kam_none or none
    gc_hdr = any("계속기업" in h for h, _ in secs)
    flat = re.sub(r"\s+", "", text)
    gc_txt = "계속기업관련중요한불확실성" in flat or "존속능력에유의적의문" in flat
    return {
        "opinion": classify_opinion(op_txt or text[:3000]),
        "has_kam": bool(kam) and not kam_none,
        "kam_titles": kam_titles,
        "has_emphasis": any(h.startswith("강조사항") for h, _ in secs),
        "has_other_matter": any(h.startswith("기타사항") for h, _ in secs),
        "going_concern_uncertainty": gc_hdr or gc_txt,
        "sections": secs,
    }


def classify_internal_control(text: str) -> tuple[str, str, bool]:
    """내부회계관리제도 문단 → (감사/검토 구분, 의견, 중요한 취약점 여부)."""
    if not text or not text.strip():
        return "없음", "없음", False
    t = re.sub(r"\s+", "", text)
    ic_type = "감사" if ("내부회계관리제도를감사하였" in t or "내부회계관리제도감사보고서" in t) else ("검토" if "검토" in t else "불명")
    strong_mw = bool(re.search(r"(다음|아래)의중요한취약점|중요한취약점이발견되었|중요한취약점이존재합니다|중요한취약점으로판단", t))
    if "의견을표명하지아니" in t or "의견거절" in t:
        op = "의견거절"
    elif not strong_mw and re.search(r"효과적으로설계및운영되고있습니다|효과적으로설계되어운영되고있습니다|발견되지아니하였습니다|발견되지않았습니다", t):
        op = "적정"
    elif strong_mw or re.search(r"효과적으로설계및운영되고있지않|효과적으로설계및운영되지않|효과적으로설계되어운영되고있지않습니다|부적정의견", t):
        op = "비적정"
    else:
        op = "판독불가"
    return ic_type, op, (op == "비적정" or strong_mw)


def extract_auditor(text: str) -> str:
    """감사보고서 말미 서명부의 회계법인명(마지막 등장)."""
    tail = (text or "")[-3000:] or (text or "")
    hits = re.findall(r"((?:[가-힣A-Za-z]\s?){1,20}회\s?계\s?법\s?인|(?:[가-힣]\s?){2,20}감\s?사\s?반)", tail)
    if not hits:
        hits = re.findall(r"((?:[가-힣A-Za-z]\s?){1,20}회\s?계\s?법\s?인)", text or "")
    return re.sub(r"\s+", "", hits[-1]).lstrip("|") if hits else ""


def _fy_window(bsns_year: int, acc_mt: str) -> tuple[str, str]:
    m = int(acc_mt or 12)
    fye = dt.date(bsns_year, m, 1)
    return fye.strftime("%Y%m%d"), (fye + dt.timedelta(days=430)).strftime("%Y%m%d")


def find_annual_report(cc: str, bsns_year: int, acc_mt: str) -> Optional[dict]:
    bgn, end = _fy_window(bsns_year, acc_mt)
    lst = api("list", corp_code=cc, bgn_de=bgn, end_de=end, pblntf_detail_ty="A001", page_count=100)["list"]
    tag = f"({bsns_year}.{int(acc_mt or 12):02d})"
    cand = [x for x in lst if tag in x["report_nm"]]
    return cand[0] if cand else None  # 최신(정정 포함) 우선


def find_standalone_audit_report(cc: str, bsns_year: int, acc_mt: str, consolidated: bool) -> Optional[dict]:
    bgn, end = _fy_window(bsns_year, acc_mt)
    lst = api("list", corp_code=cc, bgn_de=bgn, end_de=end, pblntf_ty="F", page_count=100)["list"]
    tag = f"({bsns_year}.{int(acc_mt or 12):02d})"
    want = "연결감사보고서" if consolidated else "감사보고서"
    for x in lst:
        nm = re.sub(r"^\[.*?\]\s*", "", x["report_nm"])
        if tag in nm and nm.startswith(want) and (consolidated or not nm.startswith("연결")):
            return x
    return None


# ================================================================ TOOLS
@mcp.tool()
def search_company(query: str, limit: int = 10, listed_only: bool = False) -> str:
    """회사명(한글/영문 일부), 종목코드(6자리), DART 고유번호(8자리)로 회사를 찾아 corp_code를 돌려준다.
    상장사가 먼저 나온다. 다른 도구의 corp 인자에는 여기서 얻은 corp_code나 정확한 회사명을 넣으면 된다."""
    hits = find_corps(query, limit=limit, listed_only=listed_only)
    if not hits:
        return f"'{query}' 검색 결과 없음. 과거 사명이나 약칭일 수 있으니 다른 표기로 다시 시도하세요."
    return "\n".join(_fmt_corp(h) for h in hits)


@mcp.tool()
def get_company_profile(corp: str) -> str:
    """기업개황: 정식명칭, 대표자, 법인구분(Y유가/K코스닥/N코넥스/E기타), 설립일, 결산월, 업종코드, 주소, 홈페이지 등."""
    r = resolve(corp)
    d = api("company", corp_code=r["corp_code"])
    cls = {"Y": "유가증권(코스피)", "K": "코스닥", "N": "코넥스", "E": "기타(비상장)"}.get(d.get("corp_cls"), d.get("corp_cls"))
    lines = [
        f"회사명: {d.get('corp_name')} ({d.get('corp_name_eng')})",
        f"corp_code: {d.get('corp_code')}  종목코드: {d.get('stock_code') or '-'}  시장: {cls}",
        f"대표자: {d.get('ceo_nm')}", f"설립일: {d.get('est_dt')}  결산월: {d.get('acc_mt')}월", f"업종코드: {d.get('induty_code')}",
        f"법인등록번호: {d.get('jurir_no')}  사업자번호: {d.get('bizr_no')}", f"주소: {d.get('adres')}",
        f"홈페이지: {d.get('hm_url')}  전화: {d.get('phn_no')}",
    ]
    if r.get("_note"):
        lines.append("※ " + r["_note"])
    return "\n".join(lines)


@mcp.tool()
def search_disclosures(corp: Optional[str] = None, bgn_de: Optional[str] = None, end_de: Optional[str] = None,
                       pblntf_ty: Optional[str] = None, pblntf_detail_ty: Optional[str] = None,
                       corp_cls: Optional[str] = None, keyword: Optional[str] = None,
                       last_reprt_at: str = "N", page_no: int = 1, page_count: int = 100) -> str:
    """공시 목록 조회. corp를 생략하면 전체 회사 대상(이 경우 기간은 최대 3개월, 기본 최근 3개월). corp가 있으면 기본 최근 1년.
    날짜는 YYYYMMDD. pblntf_ty: A정기공시 B주요사항 C발행 D지분 E기타 F외부감사관련 G펀드 H자산유동화 I거래소 J공정위.
    pblntf_detail_ty 예: A001 사업보고서, A002 반기보고서, A003 분기보고서, F001 감사보고서, F002 연결감사보고서,
    B001 주요사항보고서, D001 주식등의대량보유상황보고서, I001 수시공시. corp_cls: Y유가 K코스닥 N코넥스 E기타.
    keyword는 보고서명 필터(부분일치). last_reprt_at='Y'면 정정 전 원본 제외(최종본만)."""
    params: dict = {}
    hdr = ""
    if corp:
        r = resolve(corp)
        params["corp_code"] = r["corp_code"]
        hdr = f"[{r['corp_name']} {r['corp_code']}] "
        if r.get("_note"):
            hdr += "(" + r["_note"] + ") "
    today = dt.date.today()
    if not end_de:
        end_de = today.strftime("%Y%m%d")
    if not bgn_de:
        bgn_de = (dt.datetime.strptime(end_de, "%Y%m%d").date() - dt.timedelta(days=90 if not corp else 365)).strftime("%Y%m%d")
    d = api("list", bgn_de=bgn_de, end_de=end_de, pblntf_ty=pblntf_ty, pblntf_detail_ty=pblntf_detail_ty,
            corp_cls=corp_cls, last_reprt_at=last_reprt_at, page_no=page_no, page_count=min(page_count, 100), **params)
    items = d.get("list", [])
    if keyword:
        items = [x for x in items if keyword in x.get("report_nm", "")]
    total = d.get("total_count", len(items))
    head = f"{hdr}{bgn_de}~{end_de} 공시 {total}건 (page {page_no}/{d.get('total_page', 1)})\n"
    rows = [{**x, "mk": {"Y": "유", "K": "코", "N": "넥", "E": "기"}.get(x.get("corp_cls"), "")} for x in items]
    return head + _md_table(rows, [("rcept_dt", "접수일"), ("corp_name", "회사"), ("mk", "시장"), ("report_nm", "보고서명"),
                                   ("rcept_no", "접수번호"), ("flr_nm", "제출인"), ("rm", "비고")])


@mcp.tool()
def get_financials(corp: str, bsns_year: int, reprt_code: str = "11011", fs_div: str = "CFS",
                   sj_div: Optional[str] = None, account_filter: Optional[str] = None, brief: bool = False) -> str:
    """재무제표 계정 조회(XBRL 표준계정 기반, 2015년 이후 사업보고서 제출회사).
    reprt_code 11011 사업보고서/11012 반기/11013 1분기/11014 3분기. fs_div CFS 연결, OFS 별도.
    sj_div로 재무제표 종류 필터: BS 재무상태표, IS 손익계산서, CIS 포괄손익계산서, CF 현금흐름표, SCE 자본변동표.
    account_filter: 계정명 부분일치(쉼표로 여러 개, 예 '매출액,영업이익,당기순이익').
    brief=True면 주요계정만(fnlttSinglAcnt). 주석 본문은 get_audit_report(sections='notes') 또는 read_document를 쓴다."""
    r = resolve(corp)
    ep = "fnlttSinglAcnt" if brief else "fnlttSinglAcntAll"
    d = api(ep, corp_code=r["corp_code"], bsns_year=str(bsns_year), reprt_code=reprt_code, fs_div=fs_div)
    rows = d.get("list", [])
    if brief:
        rows = [x for x in rows if x.get("fs_div") == fs_div]
    if sj_div:
        rows = [x for x in rows if x.get("sj_div") == sj_div.upper()]
    if account_filter:
        terms = [t.strip() for t in account_filter.split(",") if t.strip()]
        rows = [x for x in rows if any(t in x.get("account_nm", "") for t in terms)]
    if not rows:
        return (f"[{r['corp_name']}] {bsns_year}년 {reprt_code} {fs_div} 재무데이터 없음. "
                f"비상장사이거나 연결재무제표 미작성(OFS로 재시도)일 수 있습니다. 비상장사는 get_audit_report로 감사보고서 본문을 읽으세요.")
    head = (f"[{r['corp_name']} {r['corp_code']}] {bsns_year}년 reprt_code={reprt_code} {fs_div} ({len(rows)}계정, 단위 원)\n"
            f"당기={rows[0].get('thstrm_nm', '')} 전기={rows[0].get('frmtrm_nm', '')} 전전기={rows[0].get('bfefrmtrm_nm', '')}\n")
    if r.get("_note"):
        head += "※ " + r["_note"] + "\n"
    return head + _md_table(rows, [("sj_div", "구분"), ("account_nm", "계정명"), ("thstrm_amount", "당기"),
                                   ("frmtrm_amount", "전기"), ("bfefrmtrm_amount", "전전기")],
                            amount_cols={"thstrm_amount", "frmtrm_amount", "bfefrmtrm_amount"})


@mcp.tool()
def compare_financials(corps: str, bsns_year: int, reprt_code: str = "11011", fs_div: str = "CFS") -> str:
    """여러 상장회사(쉼표 구분, 최대 10개)의 주요계정(매출액·영업이익·당기순이익·자산·부채·자본 등)을 한 번에 비교."""
    names = [c.strip() for c in corps.split(",") if c.strip()][:10]
    rs = [resolve(c) for c in names]
    d = api("fnlttMultiAcnt", corp_code=",".join(x["corp_code"] for x in rs), bsns_year=str(bsns_year), reprt_code=reprt_code)
    rows = [x for x in d.get("list", []) if x.get("fs_div") == fs_div]
    if not rows:
        return "데이터 없음 (상장사 주요계정만 제공됩니다)."
    rows.sort(key=lambda x: (x.get("corp_code"), x.get("ord", "")))
    return _md_table(rows, [("corp_code", "corp_code"), ("sj_nm", "재무제표"), ("account_nm", "계정명"), ("thstrm_amount", "당기"),
                            ("frmtrm_amount", "전기")], amount_cols={"thstrm_amount", "frmtrm_amount"}) + \
        "\n" + "\n".join(f"{x['corp_code']}={x['corp_name']}" for x in rs)


@mcp.tool()
def get_audit_summary(corp: str, bsns_year: int, reprt_code: str = "11011") -> str:
    """사업보고서 기재 기준 감사인·감사의견·강조사항·핵심감사사항 요약, 감사용역(보수·시간), 비감사용역 계약 현황.
    사업보고서 제출회사(주로 상장사)만 가능. 감사보고서 '본문'(문단 전체)은 get_audit_report를 사용."""
    r = resolve(corp)
    cc = r["corp_code"]
    out = [f"[{r['corp_name']} {cc}] {bsns_year}년 (reprt_code={reprt_code})"]
    if r.get("_note"):
        out.append("※ " + r["_note"])
    a = api("accnutAdtorNmNdAdtOpinion", corp_code=cc, bsns_year=str(bsns_year), reprt_code=reprt_code)["list"]
    out.append("\n### 회계감사인의 명칭 및 감사의견")
    out.append(_md_table(a, [("bsns_year", "사업연도"), ("adtor", "감사인"), ("adt_opinion", "감사의견"),
                             ("adt_reprt_spcmnt_matter", "특기사항(구)"), ("emphs_matter", "강조사항 등"), ("core_adt_matter", "핵심감사사항")]))
    b = api("adtServcCnclsSttus", corp_code=cc, bsns_year=str(bsns_year), reprt_code=reprt_code)["list"]
    out.append("\n### 감사용역 체결현황")
    out.append(_md_table(b, [("bsns_year", "사업연도"), ("adtor", "감사인"), ("cn", "내용"), ("mendng_amt", "보수(구양식)"),
                             ("tot_reqre_time", "총소요시간(구양식)"), ("adt_cntrct_dtls_mendng", "계약보수(백만원)"), ("adt_cntrct_dtls_time", "계약시간"),
                             ("real_exc_dtls_mendng", "실제보수(백만원)"), ("real_exc_dtls_time", "실제시간")]))
    c = api("accnutAdtorNonAdtServcCnclsSttus", corp_code=cc, bsns_year=str(bsns_year), reprt_code=reprt_code)["list"]
    out.append("\n### 회계감사인과의 비감사용역 계약체결 현황")
    out.append(_md_table(c, [("bsns_year", "사업연도"), ("cntrct_cncls_de", "계약일"), ("servc_cn", "용역내용"), ("servc_exc_pd", "기간"),
                             ("servc_mendng", "보수"), ("rm", "비고")]))
    return "\n".join(out)


REPORT_ITEMS = {
    "증자감자": "irdsSttus", "배당": "alotMatter", "자기주식": "tesstkAcqsDspsSttus", "최대주주": "hyslrSttus", "최대주주변동": "hyslrChgSttus",
    "소액주주": "mrhlSttus", "임원": "exctvSttus", "직원": "empSttus", "임원개인보수": "hmvAuditIndvdlBySttus", "임원전체보수": "hmvAuditAllSttus",
    "개인별보수": "indvdlByPay", "타법인출자": "otrCprInvstmntSttus", "주식총수": "stockTotqySttus", "채무증권발행": "detScritsIsuAcmslt",
    "기업어음미상환": "entrprsBilScritsNrdmpBlce", "단기사채미상환": "srtpdPsndbtNrdmpBlce", "회사채미상환": "cprndNrdmpBlce",
    "신종자본증권미상환": "newCaplScritsNrdmpBlce", "조건부자본증권미상환": "cndlCaplScritsNrdmpBlce", "공모자금사용": "pssrpCptalUseDtls",
    "사모자금사용": "prvsrpCptalUseDtls", "사외이사": "outcmpnyDrctrNdChangeSttus",
}


@mcp.tool()
def get_report_item(corp: str, bsns_year: int, item: str, reprt_code: str = "11011") -> str:
    """정기보고서(사업보고서 등) 주요정보 항목 조회. item 중 하나:
    증자감자, 배당, 자기주식, 최대주주, 최대주주변동, 소액주주, 임원, 직원, 임원개인보수, 임원전체보수, 개인별보수, 타법인출자, 주식총수,
    채무증권발행, 기업어음미상환, 단기사채미상환, 회사채미상환, 신종자본증권미상환, 조건부자본증권미상환, 공모자금사용, 사모자금사용, 사외이사."""
    ep = REPORT_ITEMS.get(item.strip())
    if not ep:
        return "지원 항목: " + ", ".join(REPORT_ITEMS)
    r = resolve(corp)
    rows = api(ep, corp_code=r["corp_code"], bsns_year=str(bsns_year), reprt_code=reprt_code)["list"]
    if not rows:
        return f"[{r['corp_name']}] {bsns_year}년 '{item}' 데이터 없음."
    skip = {"rcept_no", "corp_cls", "corp_code", "corp_name", "stck_knd"}
    cols = [(k, k) for k in rows[0].keys() if k not in skip]
    return f"[{r['corp_name']} {r['corp_code']}] {bsns_year}년 {item} ({len(rows)}행)\n" + _md_table(rows, cols)


@mcp.tool()
def list_document_toc(rcept_no: str, dcm_no: Optional[str] = None) -> str:
    """공시서류의 목차(섹션)와 첨부서류(감사보고서, 연결감사보고서, 내부회계관리제도운영보고서, 정관 등) 목록.
    rcept_no는 search_disclosures의 접수번호. dcm_no를 주면 그 첨부문서의 목차를 본다.
    이후 read_document(rcept_no, dcm_no, section=...)로 본문을 읽는다."""
    doc = fetch_toc(rcept_no, dcm_no)
    out = [f"{doc['title']}  ({doc['url']})"]
    if doc.get("error"):
        out.append(doc["error"])
    out.append("\n## 목차 (id | 제목)")
    for n in doc["nodes"]:
        out.append(f"{'  ' * (n['depth'] - 1)}{n['id']} | {n['text']}")
    if doc["attachments"]:
        out.append("\n## 첨부서류 (dcm_no | 제목)")
        for a in doc["attachments"]:
            out.append(f"{a['dcm_no']} | {a['title']}")
    return "\n".join(out)


@mcp.tool()
def read_document(rcept_no: str, dcm_no: Optional[str] = None, section: Optional[str] = None,
                  query: Optional[str] = None, max_chars: int = 15000, offset: int = 0, context_chars: int = 400) -> str:
    """공시서류 본문을 텍스트(표는 | 구분)로 읽는다. section은 목차 제목 일부(예 '주석', 'II. 사업의 내용', '독립된 감사인')
    또는 목차 id. query를 주면 전체 본문에서 키워드(쉼표/공백 구분) 주변 발췌만 반환(긴 주석에서 특정 항목 찾기에 적합).
    길면 max_chars 단위로 잘리며 offset으로 이어서 읽는다. 첨부서류(감사보고서 등)는 dcm_no를 함께 준다."""
    doc = fetch_toc(rcept_no, dcm_no)
    if doc.get("error"):
        return doc["error"]
    nodes = select_nodes(doc["nodes"], section)
    if not nodes:
        return f"'{section}' 섹션 없음. 목차: " + " / ".join(f"{n['id']}:{n['text']}" for n in doc["nodes"])
    text = doc_text(nodes)
    if query:
        return f"[{doc['title']}] 검색 '{query}' (대상 {len(text):,}자)\n" + snippets(text, query, context_chars)
    total = len(text)
    chunk = text[offset:offset + max_chars]
    more = f"\n\n…(계속: offset={offset + max_chars} 로 재호출, 전체 {total:,}자)" if offset + max_chars < total else ""
    return f"[{doc['title']}] {offset:,}~{min(total, offset + max_chars):,}/{total:,}자\n{chunk}{more}"


@mcp.tool()
def get_audit_report(corp: str, bsns_year: int, consolidated: bool = False,
                     sections: str = "summary,opinion,basis,kam,emphasis,other,going_concern,internal_control",
                     max_chars: int = 30000) -> str:
    """감사보고서 '본문'을 찾아 문단별로 돌려준다. 상장사는 사업보고서 첨부 감사보고서, 비상장사는 별도 공시된 감사보고서(F001/F002)를 자동 선택.
    consolidated=True면 연결감사보고서. sections(쉼표 구분): summary(의견·KAM제목·강조/계속기업 플래그), opinion(감사의견),
    basis(의견근거), kam(핵심감사사항), emphasis(강조사항), other(기타사항), going_concern(계속기업 불확실성), responsibilities(책임 문단),
    internal_control(내부회계관리제도 감사/검토의견), notes(재무제표 주석 전문, 매우 김), full(독립된 감사인의 감사보고서 전문).
    결과 상단의 rcept_no/dcm_no로 read_document(query=...)를 호출하면 주석 등을 키워드로 더 찾을 수 있다."""
    r = resolve(corp)
    cc = r["corp_code"]
    prof = api("company", corp_code=cc)
    acc_mt = prof.get("acc_mt") or "12"
    want = set(s.strip() for s in sections.split(","))
    head = [f"# {prof.get('corp_name')} ({cc}) {bsns_year}년 {'연결' if consolidated else '별도'}감사보고서"]
    if r.get("_note"):
        head.append("※ " + r["_note"])

    disc = find_annual_report(cc, bsns_year, acc_mt)
    rcp = dcm = None
    if disc:
        toc = fetch_toc(disc["rcept_no"])
        target = "연결감사보고서" if consolidated else "감사보고서"
        att = next((a for a in toc["attachments"] if _norm(a["title"]) == _norm(target)), None)
        if att:
            rcp, dcm = att["rcept_no"], att["dcm_no"]
            head.append(f"출처: {disc['report_nm']} (접수 {disc['rcept_dt']}) 첨부 '{att['title']}'  rcept_no={rcp} dcm_no={dcm}")
        else:
            head.append(f"사업보고서({disc['rcept_no']})에 '{target}' 첨부가 없음. 첨부목록: " + ", ".join(a["title"] for a in toc["attachments"]))
    if not rcp:
        sd = find_standalone_audit_report(cc, bsns_year, acc_mt, consolidated)
        if sd:
            rcp, dcm = sd["rcept_no"], None
            head.append(f"출처: 별도 공시 {sd['report_nm']} (접수 {sd['rcept_dt']}) rcept_no={rcp}")
        else:
            bgn, end = _fy_window(bsns_year, acc_mt)
            return "\n".join(head) + (f"\n{bsns_year}년(결산월 {acc_mt}) 사업보고서/감사보고서 공시를 찾지 못했습니다(검색기간 {bgn}~{end}). "
                                      f"미제출·연결 미작성·결산기 변경·외감 비대상일 수 있습니다. search_disclosures(corp, pblntf_ty='A' 또는 'F')로 확인하세요.")
    head.append(f"뷰어: {DART}/dsaf001/main.do?rcpNo={rcp}" + (f"&dcmNo={dcm}" if dcm else ""))
    doc = fetch_toc(rcp, dcm)
    nodes = doc["nodes"]
    rep_nodes = [n for n in nodes if "독립된" in n["text"] or ("감사인" in n["text"] and "보고서" in n["text"])] or \
                [n for n in nodes if n["depth"] == 1 and "주석" not in n["text"] and "재무제표" not in n["text"]][:2]
    report_text = doc_text(rep_nodes)
    info = summarize_audit_text(report_text)
    out = list(head)

    if "summary" in want:
        out.append("\n## 요약")
        out.append(f"- 감사의견(본문 판독): {info['opinion']}")
        kam_s = (("있음 " + str(len(info["kam_titles"])) + "건 - " + " / ".join(info["kam_titles"])) if info["kam_titles"]
                 else "있음 (제목 자동판독 실패 - kam 섹션 참조)") if info["has_kam"] else "없음"
        out.append(f"- 핵심감사사항: {kam_s}")
        out.append(f"- 강조사항 문단: {'있음' if info['has_emphasis'] else '없음'}   기타사항 문단: {'있음' if info['has_other_matter'] else '없음'}")
        out.append(f"- 계속기업 관련 중요한 불확실성 언급: {'있음' if info['going_concern_uncertainty'] else '없음'}")
        ic_nodes = [n for n in nodes if "내부회계" in n["text"]]
        ic_type, ic_op, ic_mw = classify_internal_control(doc_text(ic_nodes) if ic_nodes else "")
        out.append(f"- 내부회계관리제도: {ic_type} / {ic_op}" + (" / 중요한 취약점 언급" if ic_mw else ""))
        out.append(f"- 감사인(서명부 판독): {extract_auditor(report_text) or '판독불가'}")
        out.append("- 섹션: " + " / ".join(h for h, _ in info["sections"]))

    def pick(pred):
        return [(h, b) for h, b in info["sections"] if pred(h)]

    groups = [
        ("opinion", "감사의견", lambda h: h.startswith(("감사의견", "한정의견", "부적정의견", "의견거절")) and "근거" not in h),
        ("basis", "의견근거", lambda h: "근거" in h),
        ("going_concern", "계속기업 관련 중요한 불확실성", lambda h: "계속기업" in h),
        ("kam", "핵심감사사항", lambda h: h.startswith("핵심감사사항")),
        ("emphasis", "강조사항", lambda h: h.startswith("강조사항")),
        ("other", "기타사항", lambda h: h.startswith("기타사항")),
        ("responsibilities", "책임 문단", lambda h: "책임" in h),
    ]
    for key, label, pred in groups:
        if key in want:
            secs = pick(pred)
            out.append(f"\n## {label}")
            if secs:
                for h, b in secs:
                    out.append(f"### {h}\n{b}")
            elif key == "going_concern" and info["going_concern_uncertainty"]:
                out.append("(별도 문단 없음 — 본문 중 계속기업 불확실성 언급은 있음, sections='full'로 확인)")
            else:
                out.append("(해당 문단 없음)")
    if "internal_control" in want:
        ic = [n for n in nodes if "내부회계" in n["text"]]
        out.append("\n## 내부회계관리제도 감사 또는 검토의견")
        out.append(doc_text(ic) if ic else "(해당 섹션 없음 — 사업보고서 첨부 '내부회계관리제도운영보고서' 또는 본문 확인)")
    if "full" in want:
        out.append("\n## 독립된 감사인의 감사보고서 전문\n" + report_text)
    if "notes" in want:
        nt = [n for n in nodes if "주석" in n["text"]]
        out.append("\n## 재무제표 주석\n" + (doc_text(nt) if nt else "(주석 섹션 없음)"))
    res = "\n".join(out)
    if len(res) > max_chars:
        res = res[:max_chars] + (f"\n\n…(잘림: 전체 {len(res):,}자. sections를 줄이거나 "
                                 f"read_document(rcept_no={rcp}, dcm_no={dcm}, section/query)로 나눠 읽으세요)")
    return res


@mcp.tool()
def download_original_document(rcept_no: str, max_chars: int = 20000, offset: int = 0, query: Optional[str] = None) -> str:
    """OpenDART 공식 '공시서류원본파일'(document.xml)을 내려받아 텍스트로 반환(뷰어 스크래핑이 막힐 때의 대안).
    본문 문서만 포함되며 첨부서류는 들어있지 않다."""
    r = http.get(f"{BASE}/document.xml", params={"crtfc_key": API_KEY, "rcept_no": rcept_no})
    try:
        z = zipfile.ZipFile(io.BytesIO(r.content))
    except zipfile.BadZipFile:
        return "원본 다운로드 실패: " + r.text[:300]
    texts = []
    for name in z.namelist():
        raw = z.read(name)
        try:
            s = raw.decode("utf-8")
        except UnicodeDecodeError:
            s = raw.decode("cp949", "replace")
        texts.append(f"## 파일 {name}\n" + html_to_text(s))
    text = "\n".join(texts)
    if query:
        return snippets(text, query)
    chunk = text[offset:offset + max_chars]
    more = f"\n…(계속: offset={offset + max_chars}, 전체 {len(text):,}자)" if offset + max_chars < len(text) else ""
    return chunk + more


@mcp.tool()
def refresh_corp_codes() -> str:
    """DART 고유번호 목록(corpCode.xml)을 강제로 다시 내려받는다(신규 상장·사명 변경 반영). 보통은 7일마다 자동 갱신."""
    rows = load_corp_codes(force=True)
    return f"{len(rows):,}개 회사 로드 완료 (상장 {sum(1 for r in rows if r['stock_code']):,}개)."


# ================================================================ 상장사 감사보고서 인덱스 (스크리닝)
INDEX_DB = DATA_DIR / "audit_index.sqlite"


def _idx() -> sqlite3.Connection:
    if not INDEX_DB.exists():
        raise DartError("인덱스 DB가 없습니다. download_audit_index()로 배포본을 내려받거나 start_audit_index(bsns_year)로 구축하세요.")
    con = sqlite3.connect(INDEX_DB, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def _indexer_running() -> Optional[int]:
    lock = DATA_DIR / "indexer.lock"
    if not lock.exists():
        return None
    try:
        pid = int(lock.read_text().strip() or 0)
    except ValueError:
        return None
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return pid if pid and str(pid) in out else None


@mcp.tool()
def start_audit_index(bsns_year: int, mode: str = "both", markets: str = "Y,K,N", include_consolidated: bool = True,
                      interval_sec: float = 1.0, limit: int = 0) -> str:
    """상장사 감사보고서 전수 인덱스 구축을 백그라운드로 시작한다(사업연도 단위). 이미 완료된 회사는 건너뛴다.
    mode='api'   : OpenDART 공식 API만 사용(회사당 1회 호출, 약 10분). 감사인·감사의견·강조사항·KAM(사업보고서 기재 기준). 당기+전기+전전기 3개 연도가 함께 채워진다.
    mode='viewer': DART 뷰어에서 감사보고서 본문을 읽어 계속기업 불확실성·기타사항·내부회계관리제도 의견까지 구조화(회사당 6~7요청, 1초 간격 → 약 4~5시간, 과속 시 DART가 IP를 차단하므로 interval_sec을 줄이지 말 것).
    mode='both'  : api 후 viewer(기본). markets: Y유가 K코스닥 N코넥스. limit은 테스트용(앞 N개 회사만).
    진행률은 audit_index_status, 조회는 screen_audit_reports. 인덱스는 data/audit_index.sqlite에 영구 저장된다."""
    pid = _indexer_running()
    if pid:
        return f"이미 인덱서가 실행 중입니다(pid {pid}). audit_index_status로 진행률을 확인하세요."
    here = Path(__file__).resolve().parent
    cmd = [sys.executable, "-X", "utf8", str(here / "indexer.py"), "--year", str(bsns_year), "--mode", mode, "--markets", markets,
           "--workers", "1", "--interval", str(max(interval_sec, 0.5))]
    if not include_consolidated:
        cmd.append("--no-cfs")
    if limit:
        cmd += ["--limit", str(limit)]
    log = DATA_DIR / f"index_{bsns_year}.log"
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    env = {**os.environ, "OPENDART_API_KEY": API_KEY, "PYTHONUTF8": "1"}
    with open(log, "ab") as fh:
        p = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=flags, env=env, cwd=str(here))
    return (f"FY{bsns_year} 인덱싱 시작 (pid {p.pid}, 모드 {mode}, 시장 {markets}, 연결 {'포함' if include_consolidated else '제외'}). 로그: {log}\n"
            f"잠시 후 audit_index_status()로 확인하세요.")


@mcp.tool()
def download_audit_index(url: Optional[str] = None, force: bool = False) -> str:
    """미리 구축된 상장사 감사보고서 인덱스 DB(zip)를 GitHub Release에서 내려받아 설치한다(수 시간 걸리는 start_audit_index 대신 사용).
    기존 DB가 있으면 force=True일 때만 교체. 내려받은 뒤에는 start_audit_index로 최신 연도만 추가하면 된다."""
    if _indexer_running():
        return "인덱서가 실행 중입니다. 끝난 뒤 다시 시도하세요."
    if INDEX_DB.exists() and not force:
        return f"이미 인덱스 DB가 있습니다({INDEX_DB}, {INDEX_DB.stat().st_size / 1e6:.1f} MB). 교체하려면 force=True."
    src = url or INDEX_RELEASE_URL
    tmp = DATA_DIR / "audit_index.download"
    try:
        with http.stream("GET", src, timeout=600) as r:
            if r.status_code != 200:
                return f"다운로드 실패 HTTP {r.status_code}: {src}"
            with open(tmp, "wb") as fh:
                for chunk in r.iter_bytes(1 << 20):
                    fh.write(chunk)
    except httpx.HTTPError as e:
        return f"다운로드 실패: {e}"
    try:
        with zipfile.ZipFile(tmp) as z:
            names = [n for n in z.namelist() if n.endswith(".sqlite")]
            if not names:
                return "zip 안에 .sqlite 파일이 없습니다."
            for stale in (INDEX_DB, INDEX_DB.with_name(INDEX_DB.name + "-wal"), INDEX_DB.with_name(INDEX_DB.name + "-shm")):
                stale.unlink(missing_ok=True)
            with z.open(names[0]) as src_f, open(INDEX_DB, "wb") as dst:
                while True:
                    buf = src_f.read(1 << 20)
                    if not buf:
                        break
                    dst.write(buf)
    except zipfile.BadZipFile:
        # zip이 아니면 sqlite 원본으로 간주
        tmp.replace(INDEX_DB)
    finally:
        tmp.unlink(missing_ok=True)
    con = sqlite3.connect(INDEX_DB)
    yrs = con.execute("SELECT bsns_year, COUNT(*) FROM audit_index WHERE status IN ('ok','api') GROUP BY bsns_year ORDER BY 1 DESC").fetchall()
    con.close()
    return f"설치 완료: {INDEX_DB} ({INDEX_DB.stat().st_size / 1e6:.1f} MB)\n연도별 건수: " + ", ".join(f"FY{y}={n}" for y, n in yrs)


@mcp.tool()
def audit_index_status(tail_lines: int = 8) -> str:
    """감사보고서 인덱스 구축 상태: 연도별 수집 건수·오류, 실행 중 여부, 최근 로그."""
    out = []
    pid = _indexer_running()
    out.append(f"인덱서 실행 중: {'예 (pid ' + str(pid) + ')' if pid else '아니오'}")
    if not INDEX_DB.exists():
        out.append("인덱스 DB 없음. download_audit_index()로 배포본을 내려받거나 start_audit_index(bsns_year)로 직접 구축하세요.")
        return "\n".join(out)
    con = _idx()
    runs = con.execute("SELECT * FROM runs ORDER BY bsns_year DESC").fetchall()
    if runs:
        out.append("\n| 사업연도 | 상태 | 회사 | 처리 | 오류 | 시작 | 갱신 |\n|---|---|---|---|---|---|---|")
        for r in runs:
            out.append(f"| {r['bsns_year']} | {r['status']} | {r['total']} | {r['done']} | {r['errors']} | {r['started']} | {r['updated']} |")
    rows = con.execute("""SELECT bsns_year, fs_div, corp_cls, status, COUNT(*) n FROM audit_index
                          GROUP BY bsns_year, fs_div, corp_cls, status ORDER BY bsns_year DESC, fs_div, corp_cls, status""").fetchall()
    if rows:
        out.append("\n| 사업연도 | 구분 | 시장 | 상태 | 건수 |\n|---|---|---|---|---|")
        for r in rows:
            out.append(f"| {r['bsns_year']} | {r['fs_div']} | {r['corp_cls']} | {r['status']} | {r['n']} |")
    con.close()
    logs = sorted(DATA_DIR.glob("index_*.log"), key=lambda p: p.stat().st_mtime)
    if logs:
        lines = logs[-1].read_text("utf-8", "replace").splitlines()[-tail_lines:]
        out.append(f"\n최근 로그({logs[-1].name}):\n" + "\n".join(lines))
    return "\n".join(out)


@mcp.tool()
def screen_audit_reports(bsns_year: int, fs_div: str = "OFS", market: Optional[str] = None, opinion: Optional[str] = None,
                         going_concern: Optional[bool] = None, has_emphasis: Optional[bool] = None, has_other: Optional[bool] = None,
                         has_kam: Optional[bool] = None, ic_opinion: Optional[str] = None, ic_material_weakness: Optional[bool] = None,
                         auditor: Optional[str] = None, keyword: Optional[str] = None, include_text: bool = False, limit: int = 300) -> str:
    """인덱스된 상장사 감사보고서를 조건으로 스크리닝한다(회사명을 몰라도 됨). 먼저 audit_index_status로 해당 연도 인덱스 여부 확인.
    fs_div: OFS 별도 / CFS 연결 / ALL. market: Y유가 K코스닥 N코넥스(쉼표 복수). opinion: 적정/한정/부적정/의견거절/비적정(한정+부적정+의견거절). '-'는 미기재.
    ic_opinion: 적정/비적정/의견거절/없음. auditor: 회계법인명 부분일치(예 '삼일'). keyword: 강조사항·기타사항·계속기업·KAM제목·내부회계·본문 전체에서 부분일치.
    include_text=True면 매칭된 강조사항/계속기업/내부회계 문단 발췌를 함께 반환. 결과 각 행의 rcept_no/dcm_no로 read_document 호출 가능."""
    con = _idx()
    where, args = ["bsns_year=?", "status IN ('ok','api')"], [bsns_year]
    if fs_div.upper() != "ALL":
        where.append("fs_div=?")
        args.append(fs_div.upper())
    if market:
        ms = [m.strip().upper() for m in market.split(",") if m.strip()]
        where.append(f"corp_cls IN ({','.join('?' * len(ms))})")
        args += ms
    if opinion:
        if opinion.strip() == "비적정":
            where.append("opinion IN ('한정','부적정','의견거절')")
        else:
            where.append("opinion=?")
            args.append(opinion.strip())
    for col, val in (("going_concern", going_concern), ("has_emphasis", has_emphasis), ("has_other", has_other),
                     ("ic_material_weakness", ic_material_weakness)):
        if val is not None:
            where.append(f"{col}=?")
            args.append(1 if val else 0)
    if has_kam is not None:
        where.append("kam_count>0" if has_kam else "kam_count=0")
    if ic_opinion:
        where.append("ic_opinion=?")
        args.append(ic_opinion.strip())
    if auditor:
        where.append("auditor LIKE ?")
        args.append(f"%{auditor.strip()}%")
    if keyword:
        where.append("(kam_titles LIKE ? OR emphasis_text LIKE ? OR other_text LIKE ? OR gc_text LIKE ? OR ic_text LIKE ? OR report_text LIKE ?)")
        args += [f"%{keyword.strip()}%"] * 6
    cond = " AND ".join(where)
    rows = con.execute(f"SELECT * FROM audit_index WHERE {cond} ORDER BY corp_cls, corp_name LIMIT ?", args + [limit]).fetchall()
    total = con.execute(f"SELECT COUNT(*) FROM audit_index WHERE {cond}", args).fetchone()[0]
    con.close()
    mk = {"Y": "유가", "K": "코스닥", "N": "코넥스", "E": "기타"}
    out = [f"FY{bsns_year} {fs_div} 스크리닝 결과 {total}건" + (f" (상위 {limit}건 표시)" if total > limit else "")]
    out.append("출처: viewer=감사보고서 본문 파싱(계속기업·기타사항·내부회계 포함), api=사업보고서 기재 요약(OpenDART API, 내부회계·기타사항 없음)")
    out.append("| 회사 | 시장 | 감사인 | 의견 | KAM | 강조 | 기타 | 계속기업 | 내부회계 | 출처 | rcept_no | dcm_no |\n|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        ic = (f"{r['ic_type']}/{r['ic_opinion']}" + ("(취약점)" if r["ic_material_weakness"] else "")) if r["ic_type"] else "?"
        oth = "?" if r["has_other"] is None else ("O" if r["has_other"] else "-")
        out.append(f"| {r['corp_name']} | {mk.get(r['corp_cls'], r['corp_cls'])} | {r['auditor'] or ''} | {r['opinion']} | {r['kam_count']} | "
                   f"{'O' if r['has_emphasis'] else '-'} | {oth} | {'O' if r['going_concern'] else '-'} | {ic} | {r['source'] or ''} | "
                   f"{r['rcept_no'] or ''} | {r['dcm_no'] or ''} |")
    if include_text:
        for r in rows[:50]:
            parts = [t for t in (r["gc_text"], r["emphasis_text"], r["other_text"]) if t]
            if r["ic_material_weakness"] and r["ic_text"]:
                parts.append("[내부회계] " + r["ic_text"][:1500])
            if parts:
                out.append(f"\n### {r['corp_name']} ({r['fs_div']})\n" + "\n".join(p[:2500] for p in parts))
    return "\n".join(out)


if __name__ == "__main__":
    mcp.run()
