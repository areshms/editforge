#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source ~/.hermes/tools/editforge-venv/bin/activate

IN="$ROOT/test_input.mp4"
OUT="$ROOT/test_output.mp4"

if [ ! -f "$IN" ]; then
  echo "Test input missing; run ./create_test_video.sh first" && exit 1
fi

echo "Running EditForge smoke test..."
time python "$ROOT/editforge.py" "$IN" --instruction 'cut silence + captions on' --out "$OUT" -v

echo "Output: $OUT"
"${HOME}/.hermes/tools/ffmpeg-9.0.1-darwin-arm64/ffmpeg" -i "$OUT" -hide_banner 2>&1 | grep -E 'Duration|Stream' || true