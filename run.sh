#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────
# run.sh — 가상환경(.venv) 구성 → 의존성 설치 → 봇 실행을 한 번에.
#
#   ./run.sh                 # 일반 실행
#   ./run.sh --debug         # SSE + Workflow JSON 디버그 모두 켜고 실행
#   ./run.sh --debug-sse     # Agent Builder SSE 이벤트만 출력
#   ./run.sh --debug-wf      # 워크플로우 실행/로그 JSON 만 출력
#   ./run.sh --no-install    # 의존성 설치 단계 건너뛰기(빠른 재실행)
#   ./run.sh --recreate      # .venv 를 지우고 새로 생성
#   ./run.sh -h | --help     # 도움말
# ──────────────────────────────────────────────────────────────────────────
set -euo pipefail

# 스크립트가 있는 디렉터리로 이동(어디서 실행해도 동작)
cd "$(dirname "$0")"

VENV_DIR=".venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"
DO_INSTALL=1
RECREATE=0

# ── 인자 파싱 ────────────────────────────────────────────────────────────────
for arg in "$@"; do
  case "$arg" in
    --debug)      export DEBUG_SSE=1; export DEBUG_WF=1 ;;
    --debug-sse)  export DEBUG_SSE=1 ;;
    --debug-wf)   export DEBUG_WF=1 ;;
    --no-install) DO_INSTALL=0 ;;
    --recreate)   RECREATE=1 ;;
    -h|--help)
      cat <<'USAGE'
run.sh — 가상환경(.venv) 구성 → 의존성 설치 → 봇 실행을 한 번에.

  ./run.sh                 일반 실행
  ./run.sh --debug         SSE + Workflow JSON 디버그 모두 켜고 실행
  ./run.sh --debug-sse     Agent Builder SSE 이벤트만 출력
  ./run.sh --debug-wf      워크플로우 실행/로그 JSON 만 출력
  ./run.sh --no-install    의존성 설치 단계 건너뛰기(빠른 재실행)
  ./run.sh --recreate      .venv 를 지우고 새로 생성
  ./run.sh -h | --help     이 도움말
USAGE
      exit 0 ;;
    *)
      echo "알 수 없는 옵션: $arg (도움말: ./run.sh --help)" >&2
      exit 1 ;;
  esac
done

# ── Python 확인 ──────────────────────────────────────────────────────────────
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "❌ '$PYTHON_BIN' 을 찾을 수 없습니다. Python 3.10+ 를 설치하세요." >&2
  exit 1
fi

# ── .venv 구성 ───────────────────────────────────────────────────────────────
if [[ "$RECREATE" == "1" && -d "$VENV_DIR" ]]; then
  echo "♻️  기존 $VENV_DIR 삭제"
  rm -rf "$VENV_DIR"
fi
if [[ ! -d "$VENV_DIR" ]]; then
  echo "📦 가상환경 생성: $PYTHON_BIN -m venv $VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
echo "🐍 venv 활성화: $(python -V) @ $VIRTUAL_ENV"

# ── 의존성 설치 ──────────────────────────────────────────────────────────────
if [[ "$DO_INSTALL" == "1" ]]; then
  echo "⬇️  의존성 설치(requirements.txt)…"
  python -m pip install --upgrade pip -q
  python -m pip install -r requirements.txt -q
fi

# ── .env 확인 ────────────────────────────────────────────────────────────────
if [[ ! -f ".env" ]]; then
  echo "⚠️  .env 가 없습니다. .env.example 을 복사해 값을 채우세요:"
  echo "      cp .env.example .env  &&  \$EDITOR .env"
  if [[ -f ".env.example" ]]; then
    cp .env.example .env
    echo "   (방금 .env.example → .env 로 복사해 두었습니다. 값을 채운 뒤 다시 실행하세요.)"
  fi
  exit 1
fi

# ── 디버그 상태 표시 ─────────────────────────────────────────────────────────
[[ -n "${DEBUG_SSE:-}" ]] && echo "🔎 DEBUG_SSE on — Agent Builder SSE 이벤트 출력"
[[ -n "${DEBUG_WF:-}"  ]] && echo "🔎 DEBUG_WF on — Workflow 실행/로그 JSON 출력"

# ── 실행 ─────────────────────────────────────────────────────────────────────
echo "🚀 봇 시작 (종료: Ctrl-C)"
exec python app.py
