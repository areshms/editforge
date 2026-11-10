#!/usr/bin/env python3
"""Generate a 5-minute test video with alternating silence/tone segments."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
OUT = ROOT / "test_input.mp4"
DURATION = 300  # 5 minutes
FFMPEG = Path.home() / ".hermes/tools/ffmpeg-9.0.1-darwin-arm64/ffmpeg"

def main():
    # Alternating 10s tone / 10s silence envelope.
    audio_filter = "volume='if(lt(mod(t,20),10),0.7,0.01)':eval=frame"
    video_filter = "drawtext=fontfile=/System/Library/Fonts/Helvetica.ttc:fontsize=48:fontcolor=white:x=30:y=30:text='EditForge test %{pts\\:hms}',format=yuv420p"
    cmd = [
        str(FFMPEG), "-y",
        "-f", "lavfi", "-i", f"sine=frequency=500:duration={DURATION}",
        "-f", "lavfi", "-i", f"color=c=gray:s=1280x720:d={DURATION}",
        "-filter_complex", f"[0:a]{audio_filter}[a];[1:v]{video_filter}[v]",
        "-map", "[v]",
        "-map", "[a]",
        "-t", str(DURATION),
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        str(OUT),
    ]
    print("Running:", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    print(r.stderr[-1500:])
    if r.returncode != 0:
        print("FAILED", file=sys.stderr)
        sys.exit(1)
    print(f"Created {OUT} ({DURATION}s)")

if __name__ == "__main__":
    main()
