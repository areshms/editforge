# EditForge v0 — Build Report

Built at `~/projects/editforge` as a separate repo from `clip-forge`.

## Git

- Local repo initialized: `~/projects/editforge`
- Remote: `https://github.com/areshms/editforge` (pushed via `gh`)
- Commit policy: one commit per module (scaffold, test video, auto-editor integration, etc.)

## Environment

- Python venv reused: `~/.hermes/tools/editforge-venv` (already contained all deps)
- Pinned FFmpeg: `~/.hermes/tools/ffmpeg-9.0.1-darwin-arm64/ffmpeg` (libass-enabled, never Homebrew 8.1)
- `gh` authenticated as `areshms`

## Vendored / integrated tools

| Tool | Version | License family | Role |
|------|---------|----------------|------|
| auto-editor | 29.3.1 | Apache-2.0 | Silence detection / EDL generation |
| faster-whisper | 1.2.1 | MIT | Transcript, filler-word timing, caption SRT |
| scenedetect | 0.7.3 | MIT | Scene-map metadata |
| torch | 2.15.0 | BSD/Apache-style | faster-whisper backend (CPU) |
| ffmpeg-python | 0.2.0 | Apache-2.0 | available, not used in v0 |
| pydub | 0.25.1 | MIT | available, not used in v0 |

All dependencies are MIT/Apache-family.

## Supported intents v0

| Intent | How it works | Real timing (5 min test video) |
|--------|--------------|-------------------------------|
| `cut-silence` | auto-editor `-\-export premiere` → XML EDL → pinned ffmpeg concat | 0.14 s |
| `cut-filler-words` | faster-whisper word timestamps → regex match `um/uh/like/...` → concat | 1.31 s (no speech → full keep) |
| `make-vertical-reframe` | ffmpeg `crop=in_h*9/16:in_h:(in_w-in_h*9/16)/2:0` | 3.21 s |
| `captions-on` | faster-whisper → SRT → ASS → pinned ffmpeg `subtitles=` burn | 17.13 s |
| `captions-off` | No-op (output already clean) | — |
| `trim-range` | ffmpeg `-ss` / `-to` re-encode | 1.24 s for 50 s clip |
| `speedup` | ffmpeg `setpts=PTS/N` + chained `atempo` | 1.07 s for 50 s clip |
| `scene-map` | scenedetect `detect-content` + `list-scenes` → `scenes.json` | 15.18 s |

End-to-end smoke (`cut silence + captions on`):

```text
Parsed intents: ['cut-silence', 'captions-on']
Total time: 17.30 s (first run, includes faster-whisper base model download)
  cut-silence: 0.14 s
  captions-on: 17.13 s
Input: 300 s
Output: 130.48 s of kept loud content
```

Subsequent runs with the cached model take ~4.5 s total.

## CLI example

```bash
./editforge.py test_input.mp4 --instruction 'cut silence + captions on' --out out.mp4
./editforge.py test_input.mp4 --instruction 'trim 00:00:10 to 00:01:00 + speedup x2' --out out.mp4
./editforge.py test_input.mp4 --instruction 'make vertical + captions on' --out out.mp4
./editforge.py test_input.mp4 --instruction 'scene map' --out out.mp4
```

## Known gaps / blunt notes

- **No LLM / semantic parsing** — intent matching is pure regex; synonyms outside the patterns fail.
- **No smart reframe** — vertical reframe is a naive center crop, no face / saliency tracking.
- **No multi-segment plans** — each intent executes sequentially; no lookahead or cost-based optimization.
- **No format validation** — assumes input has h264+aac-ish streams; exotic codecs will likely break.
- **No progress reporting** — long jobs (faster-whisper, scenedetect) run silently until they finish.
- **Captions style is hardcoded** — one Arial/white style, no positioning, no speaker labels.
- **cut-filler-words not speech-aware on generated test** — verified path runs; real speech will yield real cuts.
- **scene-map is side-effect only** — writes `scenes.json`, does not feed into cuts automatically.
- **No parallel processing** — faster-whisper and scenedetect run one at a time.
- **No unit tests** — only shell smoke tests.
- **Auto-editor binary uses its own ffmpeg** for analysis; cuts are applied with the pinned ffmpeg.

## Next steps for v1

1. Add an LLM-based intent parser (small local model or API) to handle free-form instructions.
2. Replace regex filler detection with a real transcript token filter and filler-word lexicon file.
3. Implement smart vertical reframe (face/object detection + pan/scan).
4. Add format probing and auto-normalization before the edit chain.
5. Stream progress events for long-running stages.
6. Add a JSON plan API and service wrapper (FastAPI/Flask).
7. Unit tests for parser, segment math, and ASS conversion.
