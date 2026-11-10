# EditForge v0

AI video editing agent service: **send video + plain-English instruction → edited output**.

Built as a separate repo from `clip-forge`.

## Supported intents v0

| Intent | Description | Backend |
|--------|-------------|---------|
| `cut-silence` | Auto-remove silent/filler pauses | `auto-editor` |
| `cut-filler-words` | Remove um/uh/like/etc. via transcript regex | `faster-whisper` |
| `make-vertical-reframe` | Convert to 9:16 vertical via center crop | pinned `ffmpeg` libass build |
| `captions-on` | Burn captions into video | `faster-whisper` → SRT → ASS → `ffmpeg` libass |
| `captions-off` | No-op (output is already burned/clean) | — |
| `trim-range` | Trim between two timestamps | pinned `ffmpeg` |
| `speedup` | Speed up video/audio (default 2x) | pinned `ffmpeg` |

## Install

```bash
python -m venv ~/.hermes/tools/editforge-venv
source ~/.hermes/tools/editforge-venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
./editforge.py in.mp4 --instruction 'cut silence + captions on' --out out.mp4
./editforge.py in.mp4 --instruction 'trim 00:01:00 to 00:05:00 + make vertical + speedup x2' --out out.mp4
```

## Pinned ffmpeg

`editforge_core.py` hardcodes `~/.hermes/tools/ffmpeg-9.0.1-darwin-arm64/ffmpeg`, the libass-enabled build. It will not use Homebrew ffmpeg 8.1.

## Test video

Run `./create_test_video.sh` to generate a 5–10 minute local test asset, then `./run_smoke_test.sh` for an end-to-end smoke.
