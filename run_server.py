"""
OpenDART MCP 부트스트랩 (표준 라이브러리만 사용).

플러그인/저장소 폴더에서 바로 실행하면 사용자 홈의 ~/.opendart-mcp/venv 에 가상환경을 만들고
requirements.txt 를 설치한 뒤 server.py 를 같은 stdio 로 실행한다. 두 번째 실행부터는 바로 서버가 뜬다.

  python run_server.py            # MCP 서버 실행 (Claude Code 가 호출)
  python run_server.py --setup    # 가상환경·의존성만 설치 (첫 설치 시 권장: 설치가 1~2분 걸려 MCP 시작 제한시간을 넘길 수 있음)
  python run_server.py --check    # 설치 상태 점검
환경변수: OPENDART_API_KEY(필수), OPENDART_DATA_DIR(선택, 기본 ~/.opendart-mcp/data), OPENDART_HOME(선택, 기본 ~/.opendart-mcp)
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("OPENDART_HOME") or Path.home() / ".opendart-mcp")
VENV = HOME / "venv"
REQ = HERE / "requirements.txt"
STAMP = VENV / ".requirements.sha256"
PY = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def log(msg: str) -> None:
    print(f"[opendart-mcp] {msg}", file=sys.stderr, flush=True)


def req_hash() -> str:
    return hashlib.sha256(REQ.read_bytes()).hexdigest()


def ensure_env() -> None:
    if sys.version_info < (3, 10):
        sys.exit(f"Python 3.10 이상이 필요합니다 (현재 {sys.version.split()[0]}).")
    HOME.mkdir(parents=True, exist_ok=True)
    if not PY.exists():
        log(f"가상환경 생성: {VENV}")
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV)], stdout=sys.stderr)
    if not STAMP.exists() or STAMP.read_text().strip() != req_hash():
        log("의존성 설치 중 (mcp, httpx, beautifulsoup4, lxml) ... 1~2분")
        subprocess.check_call([str(PY), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-r", str(REQ)], stdout=sys.stderr)
        STAMP.write_text(req_hash())
        log("설치 완료")


def main() -> None:
    if "--check" in sys.argv:
        print(f"python: {sys.executable} ({sys.version.split()[0]})")
        print(f"venv  : {VENV} {'OK' if PY.exists() else '없음'}")
        print(f"deps  : {'OK' if STAMP.exists() and STAMP.read_text().strip() == req_hash() else '미설치/변경됨'}")
        print(f"data  : {os.environ.get('OPENDART_DATA_DIR') or HOME / 'data'}")
        print(f"key   : {'설정됨' if os.environ.get('OPENDART_API_KEY') else '없음 (OPENDART_API_KEY)'}")
        return
    ensure_env()
    if "--setup" in sys.argv:
        print("설치 완료. Claude Code를 다시 시작하면 opendart 도구를 쓸 수 있습니다.")
        return
    env = dict(os.environ)
    env.setdefault("OPENDART_DATA_DIR", str(HOME / "data"))
    env.setdefault("PYTHONUTF8", "1")
    # stdio 를 그대로 넘겨 MCP 통신을 중계한다.
    proc = subprocess.run([str(PY), "-X", "utf8", str(HERE / "server.py")] + [a for a in sys.argv[1:] if a not in ("--setup", "--check")], env=env)
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
