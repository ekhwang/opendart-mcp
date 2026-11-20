"""
Claude Code 사용자 설정(~/.claude.json)에 MCP 서버를 등록한다.

  python register.py                      # opendart 서버 등록(기존 항목 갱신)
  python register.py --key <OpenDART키>   # API 키를 바꿔 등록
  python register.py --mydart <토큰>      # MyDART MCP(원격 HTTP)도 함께 등록
  python register.py --remove mydart      # 항목 제거

등록 후 Claude Code 세션을 새로 열면 도구가 보인다.
"""
import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CFG = Path(os.path.expanduser("~")) / ".claude.json"
PY = HERE / ".venv" / "Scripts" / "python.exe"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", help="OpenDART API 키(생략 시 기존 설정값 또는 OPENDART_API_KEY 환경변수)")
    ap.add_argument("--mydart", help="MyDART MCP 접근 토큰 → mydart 서버(HTTP) 등록")
    ap.add_argument("--remove", help="제거할 서버 이름")
    a = ap.parse_args()

    cfg = json.loads(CFG.read_text("utf-8")) if CFG.exists() else {}
    servers = cfg.setdefault("mcpServers", {})
    shutil.copy(CFG, CFG.with_suffix(f".json.bak-{time.strftime('%Y%m%d%H%M%S')}")) if CFG.exists() else None

    if a.remove:
        servers.pop(a.remove, None)
    else:
        key = a.key or servers.get("opendart", {}).get("env", {}).get("OPENDART_API_KEY") or os.environ.get("OPENDART_API_KEY")
        if not key:
            sys.exit("OpenDART API 키가 필요합니다: --key <키>")
        if not PY.exists():
            sys.exit(f"가상환경이 없습니다: {PY}  (python -m venv .venv && .venv\\Scripts\\pip install mcp httpx beautifulsoup4 lxml)")
        servers["opendart"] = {
            "type": "stdio",
            "command": str(PY),
            "args": ["-X", "utf8", str(HERE / "server.py")],
            "env": {"OPENDART_API_KEY": key, "PYTHONUTF8": "1"},
        }
        if a.mydart:
            servers["mydart"] = {"type": "http", "url": f"https://mydart.eocpa.kr/mcp?key={a.mydart.strip()}"}

    CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    print("등록된 MCP 서버:", ", ".join(servers) or "(없음)")
    print("설정 파일:", CFG)


if __name__ == "__main__":
    main()
