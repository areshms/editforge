#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$ROOT/test_input.mp4"
DURATION=300  # 5 minutes

# Generate a 1280x720 test video:
# - 5s tone + silence + voice-like noise segments to exercise silence-cut
# - on-screen text timestamps to verify trim/reframe/caption intent visually

"${HOME}/.hermes/tools/ffmpeg-9.0.1-darwin-arm64/ffmpeg" -y \
  -f lavfi -i "sine=frequency=500:duration=${DURATION}" \
  -f lavfi -i "color=c=gray:s=1280x720:d=${DURATION}" \
  -filter_complex \
"[0:a]asplit=3[a1][a2][a3];\
 [a1]volume=0.01[asilence];\
 [a2]aecho=0.8:0.9:1000:0.3, volume=0.7[avoice];\
 [a3]volume=0.05[afaint];\
 [1:v]drawtext=fontfile=/System/Library/Fonts/Helvetica.ttc:fontsize=48:fontcolor=white:x=30:y=30:text='EditForge test %{pts\:hms}',format=yuv420p[v]\" \
  -map "[v]" -map "[asilence]" -t ${DURATION} -c:v libx264 -preset fast -crf 23 -c:a aac -b:a 128k "$OUT"

echo "Created $OUT (duration ${DURATION}s)"