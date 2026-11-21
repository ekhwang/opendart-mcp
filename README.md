# OpenDART MCP — Claude Code용 DART 전자공시 도구

금융감독원 OpenDART API와 DART 전자공시 뷰어를 결합한 MCP 서버입니다. 각자 PC에서 **본인의 OpenDART 인증키**로 독립 실행되며, 외부 서버를 거치지 않습니다.

OpenDART API만으로는 할 수 없는 것들을 처리합니다.

- **감사보고서 본문** 문단별 열람: 감사의견·의견근거·핵심감사사항(KAM)·강조사항·기타사항·계속기업 불확실성·내부회계관리제도 감사/검토의견·중요한 취약점
- **회사명 → DART 고유번호** 자동 변환 (약칭·영문·종목코드·일부 과거 사명)
- **상장/비상장 라우팅**: 상장사는 사업보고서 첨부 감사보고서, 비상장 외감법인은 별도 공시 감사보고서(F001)를 자동 선택
- **상장사 전수 인덱스 기반 스크리닝**: "코스닥에서 계속기업 불확실성이 언급된 회사", "삼일회계법인이 감사한 강조사항 기재 코스피 기업", "내부회계 비적정 회사" 같은 조건 검색

## 설치 (Claude Code)

### 준비물

1. **Python 3.10 이상** — [python.org](https://www.python.org/downloads/)에서 설치할 때 "Add python.exe to PATH" 체크. 터미널에서 `python --version`이 나오면 됩니다.
2. **OpenDART 인증키** — [opendart.fss.or.kr](https://opendart.fss.or.kr) 회원가입 → 인증키 신청/관리 → 즉시 발급(무료, 40자). 호출 한도(일 20,000건)가 키별이므로 반드시 본인 키를 쓰세요.

### 설치

Claude Code 입력창에서:

```
/plugin marketplace add ekhwang/opendart-mcp
/plugin install opendart@opendart-mcp
```

설치 중 "OpenDART API 인증키" 입력창이 뜨면 발급받은 키를 붙여넣습니다. 그 다음 터미널(PowerShell)에서 1회 초기화를 실행합니다(의존성 설치 약 30초, 이걸 생략하면 첫 실행 때 MCP 시작 제한시간을 넘길 수 있습니다).

```powershell
python "$env:USERPROFILE\.claude\plugins\cache\opendart-mcp\opendart\1.0.0\run_server.py" --setup
```

경로의 `1.0.1`은 설치된 플러그인 버전입니다(`claude plugin list`로 확인). Claude Code를 다시 열면 `opendart` 도구가 보입니다. 가상환경과 데이터는 `~/.opendart-mcp/` 아래에 생성됩니다.

### 미리 구축된 스크리닝 인덱스 내려받기 (권장)

상장사 전수 인덱스를 직접 구축하면 5시간 이상 걸립니다. Claude Code 대화에서 아래처럼 말하면 Release에 올려둔 DB를 내려받아 바로 스크리닝할 수 있습니다.

```
opendart의 download_audit_index 도구로 인덱스 DB를 내려받아줘
```

이후 새 사업연도가 나오면 `start_audit_index(연도)`로 그 연도만 추가하면 됩니다.

### 플러그인을 쓰지 않는 설치 (claude mcp add)

```powershell
git clone https://github.com/ekhwang/opendart-mcp.git
python opendart-mcp\run_server.py --setup
claude mcp add -s user opendart -e OPENDART_API_KEY=<발급받은키> -- python -X utf8 <절대경로>\opendart-mcp\run_server.py
```

## 도구

| 도구 | 설명 |
|---|---|
| `search_company` | 회사명(약칭·영문·일부 과거사명)·종목코드·고유번호 → corp_code |
| `get_company_profile` | 기업개황 (대표자, 설립일, 결산월, 시장구분, 주소 등) |
| `search_disclosures` | 공시목록 (회사별 또는 전체회사, 유형·기간·보고서명 필터) |
| `get_financials` | 재무제표 전계정 (연결/별도, 연간/분기, 계정명 필터) |
| `compare_financials` | 여러 상장사 주요계정 비교 |
| `get_audit_summary` | 사업보고서 기재 감사인·감사의견·KAM·감사보수·시간·비감사용역 |
| `get_audit_report` | **감사보고서 본문** 문단별 (감사의견/근거/KAM/강조/기타/계속기업/내부회계) |
| `get_report_item` | 사업보고서 주요정보 (배당, 임원, 직원, 최대주주, 타법인출자 등 22종) |
| `list_document_toc` | 공시서류 목차 + 첨부서류(감사보고서, 정관, 내부회계운영보고서 등) |
| `read_document` | 공시서류·첨부 본문 읽기 (섹션 지정, 키워드 발췌, 페이지 단위) |
| `download_original_document` | OpenDART 공식 원본파일(document.xml) 텍스트 |
| `refresh_corp_codes` | 고유번호 목록 강제 갱신 |
| `download_audit_index` | 미리 구축된 스크리닝 인덱스 DB를 Release에서 내려받기 |
| `start_audit_index` | 사업연도별 상장사 감사보고서 전수 인덱싱 (백그라운드) |
| `audit_index_status` | 인덱싱 진행률·건수·오류 |
| `screen_audit_reports` | **조건별 스크리닝**: 시장·감사의견·계속기업·강조/기타사항·KAM·내부회계의견·감사인·키워드 |

## 사용 예 (Claude Code 대화)

- "현대차 기업개황 정리해줘"
- "최근 한 달 LG에너지솔루션 공시 목록"
- "네이버 2025년 연결 재무제표 유형자산 주석 알려줘"
- "삼성전자 최근 5개년 감사인·감사보수·감사시간"
- "SK하이닉스 2025년 별도 감사보고서 핵심감사사항 정리해줘"
- "(주)ABC 최근 3개년 계속기업 불확실성 문단이 기재됐는지"
- "(주)ABC 2025년 내부회계관리제도 중요한 취약점 내용"
- "2025년 코스닥에서 계속기업 불확실성이 언급된 회사 전부 찾아줘"
- "삼일회계법인이 감사했고 강조사항이 있는 코스피 기업 목록"
- "올해 내부회계관리제도 의견이 비적정인 회사"

## 스크리닝 인덱스 구조

인덱스는 2단계로 만들어집니다.

| 단계 | 소스 | 내용 | 소요 | 비고 |
|---|---|---|---|---|
| api | OpenDART 공식 API | 감사인·감사의견·강조사항·KAM (사업보고서 기재 기준). 회사당 1회 호출로 당기·전기·전전기 3개 연도 | 1~1.5시간 | 차단 위험 없음 |
| viewer | DART 뷰어 감사보고서 본문 | 위 항목 + 기타사항·계속기업 불확실성 문단·내부회계관리제도 감사/검토 의견·중요한 취약점·본문 전문 | 4~5시간 (1초 간격) | **과속 금지**: 동시 요청·짧은 간격은 DART가 IP를 수 시간 차단합니다. 연속 실패 시 자동 15분 대기, 3회 반복 시 중단 |

```
download_audit_index()                  # 배포본 내려받기 (권장)
start_audit_index(2025)                 # api → viewer 순서로 직접 구축. 중단돼도 재실행 시 완료분은 건너뜀
start_audit_index(2025, mode="api")     # 빠른 요약 인덱스만
audit_index_status()                    # 진행률
screen_audit_reports(2025, market="K", opinion="비적정")
screen_audit_reports(2025, going_concern=True, include_text=True)
screen_audit_reports(2025, ic_opinion="비적정")
screen_audit_reports(2025, auditor="삼일", has_emphasis=True, market="Y")
```

결과의 `출처` 열이 api인 행은 기타사항·내부회계 열이 `?`로 표시됩니다. 판독은 규칙 기반이라 '판독불가'가 나올 수 있으며, 그 경우 결과의 `rcept_no/dcm_no`로 `read_document`를 호출해 원문을 확인하세요.

## 파일 구성

| 파일 | 역할 |
|---|---|
| `server.py` | MCP 서버 본체 (stdio) |
| `indexer.py` | 상장사 감사보고서 전수 인덱서 |
| `run_server.py` | 부트스트랩: 가상환경·의존성 자동 설치 후 서버 실행 (표준 라이브러리만 사용) |
| `register.py` | 플러그인 없이 `~/.claude.json`에 직접 등록할 때 사용 |
| `.claude-plugin/` | Claude Code 플러그인·마켓플레이스 메타데이터 |
| `~/.opendart-mcp/` | (설치 후 생성) `venv/` 가상환경, `data/` 고유번호 캐시·인덱스 DB·로그 |

## 한계

- 과거 사명 변환은 내장 별칭표(`ALIASES`)에 있는 것만 지원합니다. 필요한 사명은 `server.py`의 표에 추가하세요.
- 본문 열람·인덱싱은 DART 뷰어(dart.fss.or.kr) HTML을 파싱하므로 DART 페이지 구조가 바뀌면 수정이 필요합니다.
- 인덱스 대상은 사업보고서를 제출한 상장사(유가·코스닥·코넥스)입니다. 비상장 외감법인은 개별 `get_audit_report`로 조회합니다.
- 설치가 막힌 PC(Python 설치 불가)에서는 쓸 수 없습니다.

## 라이선스

MIT. 공시 데이터의 저작권과 이용조건은 금융감독원 DART/OpenDART 이용약관을 따릅니다.
