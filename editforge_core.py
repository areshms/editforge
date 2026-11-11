#!/usr/bin/env python3
"""
EditForge Core — AI Video Editing Agent Service v0

Rule-based instruction parsing -> edit plan -> tool execution chain.
Uses:
- auto-editor for silence removal
- faster-whisper for transcript + filler-word cuts + captions
- scenedetect for scene map metadata
- pinned FFmpeg (libass enabled) for rendering
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Pinned ffmpeg: never fall back to brew 8.1
FFMPEG = Path(os.environ.get("EDITFORGE_FFMPEG", os.path.expanduser("~/.hermes/tools/ffmpeg-9.0.1-darwin-arm64/ffmpeg")))

# Default env
DEFAULT_VENV = os.path.expanduser("~/.hermes/tools/editforge-venv")
if not Path(DEFAULT_VENV).exists():
    DEFAULT_VENV = os.path.expanduser("~/.hermes/tools/clipforge-venv")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("editforge")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Instruction:
    intent: str
    params: dict = field(default_factory=dict)


@dataclass
class Segment:
    start: float
    end: float


@dataclass
class EditPlan:
    instructions: List[Instruction]
    input_path: Path
    output_path: Path
    work_dir: Path
    timings: Dict[str, float] = field(default_factory=dict)
    scene_list: List[Segment] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Instruction parser
# ---------------------------------------------------------------------------

class InstructionParser:
    """Rule-based NL -> structured intents."""

    INTENTS = {
        "cut-silence": {
            "aliases": [r"cut\s+silence", r"remove\s+silence", r"silence\s+cut", r"trim\s+silence"],
        },
        "cut-filler-words": {
            "aliases": [r"cut\s+filler", r"remove\s+filler", r"filler\s+words?", r"um\s+uh"],
        },
        "make-vertical-reframe": {
            "aliases": [r"vertical", r"9:16", r"portrait", r"reframe", r"reformat"],
        },
        "captions-on": {
            "aliases": [r"captions?\s+(on|add)", r"add\s+captions?", r"burn\s+in\s+captions?", r"subtitles?\s+on"],
        },
        "captions-off": {
            "aliases": [r"captions?\s+(off|remove)", r"remove\s+captions?", r"subtitles?\s+off"],
        },
        "trim-range": {
            "aliases": [r"trim", r"clip\s+from", r"from\s+(.+)\s+to\s+(.+)"],
        },
        "speedup": {
            "aliases": [r"speed\s*up", r"speedup", r"fast(?:er)?\b", r"\bx(\d+(?:\.\d+)?)\b"],
        },
        "scene-map": {
            "aliases": [r"scene\s+map", r"scenes?", r"detect\s+scenes", r"scene\s+detect"],
        },
    }

    FILLER_WORDS = ["um", "uh", "like", "you know", "actually", "basically", "so", "literally", "right"]

    @classmethod
    def parse(cls, text: str) -> List[Instruction]:
        text_lower = text.lower()
        instructions: List[Instruction] = []

        # Silence
        if cls._matches("cut-silence", text_lower):
            instructions.append(Instruction("cut-silence"))

        # Filler
        if cls._matches("cut-filler-words", text_lower):
            instructions.append(Instruction("cut-filler-words", {"words": cls.FILLER_WORDS}))

        # Vertical / reframe
        if cls._matches("make-vertical-reframe", text_lower):
            instructions.append(Instruction("make-vertical-reframe"))

        # Captions on/off
        if cls._matches("captions-on", text_lower):
            instructions.append(Instruction("captions-on"))
        elif cls._matches("captions-off", text_lower):
            instructions.append(Instruction("captions-off"))

        # Trim
        trim_params = cls._extract_trim(text_lower)
        if trim_params:
            instructions.append(Instruction("trim-range", trim_params))

        # Speedup
        speed_params = cls._extract_speed(text_lower)
        if speed_params:
            instructions.append(Instruction("speedup", speed_params))

        # Scene map
        if cls._matches("scene-map", text_lower):
            instructions.append(Instruction("scene-map"))

        return instructions

    @classmethod
    def _matches(cls, intent: str, text: str) -> bool:
        for pat in cls.INTENTS[intent]["aliases"]:
            if re.search(pat, text):
                return True
        return False

    @classmethod
    def _extract_trim(cls, text: str) -> Optional[Dict[str, float]]:
        # "from 00:01:00 to 00:05:00" or "trim 00:01:00 to 00:05:00"
        m = re.search(r"(?:from|trim)?\s*(\d{1,2}:\d{2}:\d{2}|\d{1,2}:\d{2})\s+(?:to|through|-)\s+(\d{1,2}:\d{2}:\d{2}|\d{1,2}:\d{2})", text)
        if m:
            return {"start": cls._ts_to_sec(m.group(1)), "end": cls._ts_to_sec(m.group(2))}
        # numeric seconds
        m = re.search(r"trim\s+(\d+(?:\.\d+)?)s?\s+(?:to|through|-)\s+(\d+(?:\.\d+)?)s?", text)
        if m:
            return {"start": float(m.group(1)), "end": float(m.group(2))}
        return None

    @classmethod
    def _extract_speed(cls, text: str) -> Optional[Dict[str, float]]:
        m = re.search(r"\bx(\d+(?:\.\d+)?)\b", text)
        if m:
            return {"factor": float(m.group(1))}
        if re.search(r"speed\s*up|speedup|faster", text):
            return {"factor": 2.0}
        return None

    @staticmethod
    def _ts_to_sec(ts: str) -> float:
        parts = ts.split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])


# ---------------------------------------------------------------------------
# Plan generator
# ---------------------------------------------------------------------------

class PlanGenerator:
    """Order intents into a deterministic execution plan."""

    ORDER = ["trim-range", "cut-silence", "cut-filler-words", "make-vertical-reframe", "captions-on", "captions-off", "speedup", "scene-map"]

    @classmethod
    def generate(cls, instructions: List[Instruction], input_path: Path, output_path: Path, work_dir: Path) -> EditPlan:
        sorted_instr = sorted(instructions, key=lambda i: cls.ORDER.index(i.intent) if i.intent in cls.ORDER else 99)
        return EditPlan(instructions=sorted_instr, input_path=input_path, output_path=output_path, work_dir=work_dir)


# ---------------------------------------------------------------------------
# Tool wrappers
# ---------------------------------------------------------------------------

class FFmpegRunner:
    """Thin wrapper around the pinned ffmpeg binary."""

    @classmethod
    def ensure(cls):
        if not FFMPEG.exists():
            raise FileNotFoundError(f"Pinned ffmpeg not found: {FFMPEG}")

    @classmethod
    def run(cls, args: List[str], **kwargs) -> subprocess.CompletedProcess:
        cls.ensure()
        cmd = [str(FFMPEG), *args]
        logger.info("ffmpeg cmd: %s", " ".join(cmd))
        return subprocess.run(cmd, capture_output=True, text=True, **kwargs)

    @classmethod
    def probe_duration(cls, path: Path) -> float:
        cls.ensure()
        cmd = [str(FFMPEG), "-i", str(path), "-hide_banner"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        m = re.search(r"Duration:\s+(\d+):\s*(\d+):\s*(\d+\.\d+)", r.stderr)
        if not m:
            raise RuntimeError(f"Could not probe duration for {path}")
        h, mn, s = m.groups()
        return int(h) * 3600 + int(mn) * 60 + float(s)

    @classmethod
    def get_streams(cls, path: Path) -> Tuple[bool, bool]:
        cls.ensure()
        cmd = [str(FFMPEG), "-i", str(path), "-hide_banner"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        has_video = "Video:" in r.stderr
        has_audio = "Audio:" in r.stderr
        return has_video, has_audio


class AutoEditor:
    """Wrap auto-editor for silence detection via Premiere XML EDL."""

    @classmethod
    def _binary(cls) -> Path:
        candidates: List[Path] = []
        # 1. auto-editor pip package downloads a platform binary under its package dir
        try:
            import auto_editor
            candidates.append(Path(auto_editor.__file__).parent / "bin" / "auto-editor")
        except Exception:
            pass
        # 2. Known venv layout
        venv = Path(DEFAULT_VENV)
        candidates.append(venv / "lib" / "python3.11" / "site-packages" / "auto_editor" / "bin" / "auto-editor")
        candidates.append(venv / "lib" / "python3.12" / "site-packages" / "auto_editor" / "bin" / "auto-editor")
        candidates.append(venv / "lib" / "python3.13" / "site-packages" / "auto_editor" / "bin" / "auto-editor")
        for c in candidates:
            if c.exists():
                return c
        # 3. Fallback to PATH
        p = shutil.which("auto-editor")
        if p:
            return Path(p)
        raise FileNotFoundError("auto-editor binary not found")

    @classmethod
    def silence_segments(cls, input_path: Path, work_dir: Path) -> List[Segment]:
        """Return segments to KEEP after removing silence."""
        edl_path = work_dir / "autoeditor_premiere.xml"
        bin_path = cls._binary()
        cmd = [
            str(bin_path),
            str(input_path),
            "--export", "premiere",
            "--output", str(edl_path),
        ]
        logger.info("auto-editor cmd: %s", " ".join(cmd))
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            logger.error("auto-editor stderr: %s", r.stderr[-2000:])
            raise RuntimeError("auto-editor failed")
        return cls._load_keep_segments(edl_path, input_path)

    @classmethod
    def _load_keep_segments(cls, edl_path: Path, input_path: Path) -> List[Segment]:
        duration = FFmpegRunner.probe_duration(input_path)
        if not edl_path.exists():
            logger.warning("auto-editor EDL missing, falling back to single keep segment")
            return [Segment(0.0, duration)]
        import xml.etree.ElementTree as ET
        tree = ET.parse(edl_path)
        root_xml = tree.getroot()
        # Find first video track
        video_track = root_xml.find(".//sequence/media/video/track") or root_xml.find(".//{xmeml}sequence/{xmeml}media/{xmeml}video/{xmeml}track", {"xmeml": "xmeml"})
        if video_track is None:
            logger.warning("No video track in EDL, falling back to single keep segment")
            return [Segment(0.0, duration)]
        timebase_el = root_xml.find(".//sequence/rate/timebase") or root_xml.find(".//{xmeml}sequence/{xmeml}rate/{xmeml}timebase", {"xmeml": "xmeml"})
        fps = float(timebase_el.text) if timebase_el is not None and timebase_el.text else 30.0
        keeps: List[Segment] = []
        for clip in video_track.findall("clipitem"):
            inn = clip.find("in")
            out = clip.find("out")
            if inn is not None and inn.text and out is not None and out.text:
                keeps.append(Segment(int(inn.text) / fps, int(out.text) / fps))
        if not keeps:
            keeps = [Segment(0.0, duration)]
        return keeps


class FasterWhisper:
    """Wrap faster-whisper for transcript + filler detection + captions."""

    MODEL = "base"

    @classmethod
    def _ensure_model(cls):
        # faster-whisper auto-downloads on first use
        pass

    @staticmethod
    def _speed_filter(factor: float) -> float:
        """Validate speed factor and return valid ffmpeg atempo value."""
        factor = float(factor)
        if not 0.5 <= factor <= 2.0:
            raise ValueError(f"Speed factor must be between 0.5 and 2.0, got {factor}")
        return factor

    @classmethod
    def _run_transcribe(cls, input_path: Path, srt_path: Path, work_dir: Path) -> List[dict]:
        venv_python = Path(DEFAULT_VENV) / "bin/python"
        words_json = work_dir / "transcript_words.json"
        script = work_dir / "transcribe.py"
        script.write_text(f"""
import faster_whisper, sys
model = faster_whisper.download_model('{cls.MODEL}')
m = faster_whisper.WhisperModel('{cls.MODEL}', device='cpu', compute_type='int8')
segments, info = m.transcribe(sys.argv[1], word_timestamps=True, vad_filter=True)
words = []
for seg in segments:
    for w in seg.words:
        words.append({{'start': w.start, 'end': w.end, 'word': w.word.strip()}})
import json
with open(sys.argv[2], 'w') as f:
    json.dump(words, f, indent=2)
""")
        cmd = [str(venv_python), str(script), str(input_path), str(words_json)]
        logger.info("faster-whisper cmd: %s", " ".join(cmd))
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            logger.error("faster-whisper stderr: %s", r.stderr[-2000:])
            raise RuntimeError("faster-whisper failed")
        words = json.loads(words_json.read_text())
        return words

    @classmethod
    def transcribe_to_srt(cls, input_path: Path, srt_path: Path, work_dir: Path) -> List[dict]:
        words = cls._run_transcribe(input_path, srt_path, work_dir)
        # Build simple SRT from words (group by ~3-5 seconds)
        groups: List[List[dict]] = []
        cur: List[dict] = []
        for w in words:
            if cur and w["end"] - cur[0]["start"] > 4.0:
                groups.append(cur)
                cur = []
            cur.append(w)
        if cur:
            groups.append(cur)
        srt_lines = []
        for i, g in enumerate(groups, 1):
            start = g[0]["start"]
            end = g[-1]["end"]
            text = " ".join(x["word"] for x in g).strip()
            if text:
                srt_lines.append(f"{i}\n{_fmt_srt_time(start)} --> {_fmt_srt_time(end)}\n{text}\n")
        srt_path.write_text("\n".join(srt_lines))
        return words

    @classmethod
    def detect_filler_segments(cls, words: List[dict], filler_words: List[str], duration: float) -> List[Segment]:
        """Return segments to KEEP (inverse of filler occurrences)."""
        filler_words = [w.lower() for w in filler_words]
        removes: List[Segment] = []
        for w in words:
            if w["word"].lower().rstrip(",.;:!?") in filler_words:
                removes.append(Segment(w["start"], w["end"]))
        return _invert_segments(removes, duration)


class SceneDetector:
    """Wrap scenedetect for scene map metadata."""

    @classmethod
    def detect(cls, input_path: Path, work_dir: Path) -> List[Segment]:
        venv_python = Path(DEFAULT_VENV) / "bin/python"
        csv_path = work_dir / "scenes.csv"
        cmd = [
            str(venv_python), "-m", "scenedetect",
            "-i", str(input_path),
            "detect-content", "-o", str(work_dir),
            "list-scenes", "--filename", str(csv_path.name),
        ]
        logger.info("scenedetect cmd: %s", " ".join(cmd))
        r = subprocess.run(cmd, capture_output=True, text=True)
        # scenedetect exit 0 even when no scenes? tolerate non-zero.
        scenes: List[Segment] = []
        if csv_path.exists():
            for line in csv_path.read_text().splitlines()[1:]:
                parts = line.split(",")
                if len(parts) >= 3:
                    try:
                        start = _timecode_to_sec(parts[1].strip())
                        end = _timecode_to_sec(parts[2].strip())
                        scenes.append(Segment(start, end))
                    except ValueError:
                        continue
        if not scenes:
            duration = FFmpegRunner.probe_duration(input_path)
            scenes = [Segment(0.0, duration)]
        return scenes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_srt_time(seconds: float) -> str:
    ms = int(round((seconds % 1) * 1000))
    s = int(seconds) % 60
    m = int(seconds // 60) % 60
    h = int(seconds // 3600)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _timecode_to_sec(tc: str) -> float:
    # "00:00:04.000" or "00:00:04"
    tc = tc.strip()
    if ":" not in tc:
        return float(tc)
    parts = tc.split(":")
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    if len(parts) == 2:
        m, s = parts
        return int(m) * 60 + float(s)
    return float(parts[0])


def _invert_segments(removes: List[Segment], duration: float) -> List[Segment]:
    if not removes:
        return [Segment(0.0, duration)]
    removes = sorted(removes, key=lambda s: s.start)
    merged: List[Segment] = []
    for seg in removes:
        if merged and seg.start <= merged[-1].end:
            merged[-1].end = max(merged[-1].end, seg.end)
        else:
            merged.append(Segment(seg.start, seg.end))
    keeps: List[Segment] = []
    cur = 0.0
    for seg in merged:
        if seg.start > cur:
            keeps.append(Segment(cur, seg.start))
        cur = max(cur, seg.end)
    if cur < duration:
        keeps.append(Segment(cur, duration))
    return keeps


def _intersect_segments(a: List[Segment], b: List[Segment]) -> List[Segment]:
    res: List[Segment] = []
    i = j = 0
    while i < len(a) and j < len(b):
        s = max(a[i].start, b[j].start)
        e = min(a[i].end, b[j].end)
        if e > s:
            res.append(Segment(s, e))
        if a[i].end < b[j].end:
            i += 1
        else:
            j += 1
    return res


def _merge_segments(segs: List[Segment]) -> List[Segment]:
    if not segs:
        return []
    segs = sorted(segs, key=lambda s: s.start)
    merged = [segs[0]]
    for s in segs[1:]:
        if s.start <= merged[-1].end:
            merged[-1].end = max(merged[-1].end, s.end)
        else:
            merged.append(s)
    return merged


def _write_concat_demuxer(segments: List[Segment], media_path: Path, concat_path: Path):
    lines = []
    for seg in segments:
        lines.append(f"file '{_escape(media_path)}'")
        lines.append(f"inpoint {seg.start:.6f}")
        lines.append(f"outpoint {seg.end:.6f}")
    concat_path.write_text("\n".join(lines))


def _escape(p: Path) -> str:
    return str(p).replace("'", "'\\''")


def _escape_filter_path(p: Path) -> str:
    # Escape for ffmpeg filtergraph path literal
    s = str(p).replace("\\", "/")
    s = s.replace(":", "\\:")
    s = s.replace("'", "\\'")
    return s


# ---------------------------------------------------------------------------
# ASS caption conversion (SRT -> ASS, burn)
# ---------------------------------------------------------------------------

def srt_to_ass(srt_path: Path, ass_path: Path, width: int = 1920, height: int = 1080):
    """Minimal SRT->ASS conversion."""
    header = f"""[Script Info]
Title: EditForge auto captions
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,{int(height * 0.05):d},&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,{max(1, int(height * 0.004))},0,2,40,40,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    blocks = []
    cur: List[str] = []
    for line in srt_path.read_text().splitlines():
        line = line.strip()
        if not line:
            if cur:
                blocks.append(cur)
                cur = []
        else:
            cur.append(line)
    if cur:
        blocks.append(cur)

    def _ass_time(sec: float) -> str:
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = int(sec % 60)
        cs = int(round((sec % 1) * 100))
        return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

    for block in blocks:
        if len(block) < 3:
            continue
        timing = block[1]
        text = " ".join(block[2:]).replace("\n", "\\N")
        m = re.match(r"(.+)\s*-->\s*(.+)", timing)
        if not m:
            continue
        start = _srt_time_to_sec(m.group(1))
        end = _srt_time_to_sec(m.group(2))
        events.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{text}")
    ass_path.write_text(header + "\n".join(events))


def _srt_time_to_sec(ts: str) -> float:
    ts = ts.strip()
    if "," in ts:
        ts = ts.replace(",", ".")
    parts = ts.split(":")
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    return int(parts[0]) * 60 + float(parts[1])


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

class FFmpegExecutor:
    """Execute an EditPlan by chaining ffmpeg operations."""

    def __init__(self, plan: EditPlan):
        self.plan = plan
        self.current = plan.input_path
        self.step = 0
        self.has_video, self.has_audio = FFmpegRunner.get_streams(plan.input_path)

    def run(self) -> Path:
        logger.info("Executing plan with %d instruction(s)", len(self.plan.instructions))
        for instr in self.plan.instructions:
            self._apply(instr)
        # Final copy to output
        if self.current != self.plan.output_path:
            self._run_ffmpeg(["-i", str(self.current), "-c", "copy", "-y", str(self.plan.output_path)])
        self.plan.output_path = self.current
        return self.plan.output_path

    def _next_workfile(self, suffix: str) -> Path:
        self.step += 1
        return self.plan.work_dir / f"step{self.step:02d}_{suffix}.mp4"

    def _apply(self, instr: Instruction):
        logger.info("Apply intent: %s", instr.intent)
        t0 = time.time()
        if instr.intent == "trim-range":
            self._trim(instr.params["start"], instr.params["end"])
        elif instr.intent == "cut-silence":
            self._cut_silence()
        elif instr.intent == "cut-filler-words":
            self._cut_filler(instr.params.get("words", []))
        elif instr.intent == "make-vertical-reframe":
            self._vertical_reframe()
        elif instr.intent == "captions-on":
            self._captions_on()
        elif instr.intent == "captions-off":
            # no-op for v0 since output has no separate caption track after burn
            pass
        elif instr.intent == "speedup":
            self._speedup(instr.params.get("factor", 2.0))
        elif instr.intent == "scene-map":
            self._scene_map()
        else:
            logger.warning("Unknown intent: %s", instr.intent)
        self.plan.timings[instr.intent] = time.time() - t0

    def _run_ffmpeg(self, args: List[str]) -> subprocess.CompletedProcess:
        r = FFmpegRunner.run(args)
        if r.returncode != 0:
            logger.error("ffmpeg stderr: %s", r.stderr[-3000:])
            raise RuntimeError(f"ffmpeg failed with args: {args}")
        return r

    def _trim(self, start: float, end: float):
        out = self._next_workfile("trim")
        self._run_ffmpeg(["-i", str(self.current), "-ss", str(start), "-to", str(end),
                          "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                          "-c:a", "aac", "-b:a", "128k", "-y", str(out)])
        self.current = out

    def _cut_silence(self):
        out = self._next_workfile("cut_silence")
        keeps = AutoEditor.silence_segments(self.current, self.plan.work_dir)
        duration = FFmpegRunner.probe_duration(self.current)
        logger.info("auto-editor keep segments: %d", len(keeps))
        if len(keeps) == 1 and abs(keeps[0].start - 0.0) < 0.01 and abs(keeps[0].end - duration) < 0.01:
            # nothing to cut
            shutil.copy(self.current, out)
        else:
            self._concat_segments(keeps, out)
        self.current = out

    def _cut_filler(self, words: List[str]):
        out = self._next_workfile("cut_filler")
        srt_path = self.plan.work_dir / "transcript.srt"
        whisper_words = FasterWhisper.transcribe_to_srt(self.current, srt_path, self.plan.work_dir)
        duration = FFmpegRunner.probe_duration(self.current)
        keeps = FasterWhisper.detect_filler_segments(whisper_words, words, duration)
        logger.info("filler keep segments: %d", len(keeps))
        self._concat_segments(keeps, out)
        self.current = out

    def _vertical_reframe(self):
        out = self._next_workfile("vertical")
        # center crop to 9:16 keeping original height
        vf = "crop=in_h*9/16:in_h:(in_w-in_h*9/16)/2:0"
        self._run_ffmpeg(["-i", str(self.current), "-vf", vf, "-c:v", "libx264",
                          "-preset", "fast", "-crf", "23", "-c:a", "copy", "-y", str(out)])
        self.current = out

    def _captions_on(self):
        out = self._next_workfile("captions")
        srt_path = self.plan.work_dir / "transcript.srt"
        ass_path = self.plan.work_dir / "captions.ass"
        if not srt_path.exists():
            FasterWhisper.transcribe_to_srt(self.current, srt_path, self.plan.work_dir)
        # probe resolution
        cmd = [str(FFMPEG), "-i", str(self.current), "-hide_banner"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        m = re.search(r"Stream #0[^:]*: Video:[^,]+,[^,]+,\s*(\d+)x(\d+)", r.stderr)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
        else:
            w, h = 1920, 1080
        srt_to_ass(srt_path, ass_path, width=w, height=h)
        vf = f"subtitles={_escape_filter_path(ass_path)}"
        self._run_ffmpeg(["-i", str(self.current), "-vf", vf, "-c:v", "libx264",
                          "-preset", "fast", "-crf", "23", "-c:a", "copy", "-y", str(out)])
        self.current = out

    def _scene_map(self):
        scenes = SceneDetector.detect(self.current, self.plan.work_dir)
        self.plan.scene_list = scenes
        scenes_json = self.plan.work_dir / "scenes.json"
        scenes_json.write_text(json.dumps([{"start": s.start, "end": s.end} for s in scenes], indent=2))
        logger.info("scene map: %d scenes written to %s", len(scenes), scenes_json)

    def _speedup(self, factor: float):
        out = self._next_workfile("speedup")
        vf = f"setpts=PTS/{factor}"
        af = f"atempo={factor}"
        # atempo max 2.0, chain if >2
        if factor > 2.0:
            parts = []
            remaining = factor
            while remaining > 2.0:
                parts.append("atempo=2.0")
                remaining /= 2.0
            parts.append(f"atempo={remaining:.4f}")
            af = ",".join(parts)
        self._run_ffmpeg(["-i", str(self.current), "-vf", vf, "-af", af,
                          "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                          "-c:a", "aac", "-b:a", "128k", "-y", str(out)])
        self.current = out

    def _concat_segments(self, keeps: List[Segment], out: Path):
        if not keeps:
            # empty result -> produce 1-second black
            self._run_ffmpeg(["-f", "lavfi", "-i", "color=c=black:s=640x360:d=1",
                              "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                              "-shortest", "-y", str(out)])
            return
        if len(keeps) == 1:
            ss = max(0.0, keeps[0].start)
            to = keeps[0].end
            self._run_ffmpeg(["-i", str(self.current), "-ss", str(ss), "-to", str(to),
                              "-c", "copy", "-avoid_negative_ts", "make_zero", "-y", str(out)])
            return
        concat_txt = self.plan.work_dir / f"concat_{self.step:02d}.txt"
        _write_concat_demuxer(keeps, self.current, concat_txt)
        self._run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(concat_txt),
                          "-c", "copy", "-avoid_negative_ts", "make_zero", "-y", str(out)])


def main():
    parser = argparse.ArgumentParser(description="EditForge — AI Video Editing Agent v0")
    parser.add_argument("input", help="Input video file")
    parser.add_argument("--instruction", required=True, help="Plain-English editing instruction")
    parser.add_argument("--out", default="output.mp4", help="Output video file")
    parser.add_argument("--work-dir", help="Temp working directory (default: auto)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    input_path = Path(args.input).resolve()
    if not input_path.exists():
        print(f"Error: input not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    # Parse
    instrs = InstructionParser.parse(args.instruction)
    if not instrs:
        print(f"Error: no supported intents found in: {args.instruction}", file=sys.stderr)
        print("Supported: cut silence, cut filler, make vertical, captions on/off, trim, speedup", file=sys.stderr)
        sys.exit(1)

    print("Parsed intents:", [i.intent for i in instrs])

    # Work dir
    if args.work_dir:
        work_dir = Path(args.work_dir).resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
    else:
        work_dir = Path(tempfile.mkdtemp(prefix="editforge_"))
    print("Work dir:", work_dir)

    # Plan + execute
    output_path = Path(args.out).resolve()
    plan = PlanGenerator.generate(instrs, input_path, output_path, work_dir)
    executor = FFmpegExecutor(plan)
    start = time.time()
    final = executor.run()
    total = time.time() - start

    # Print report
    print("\n" + "=" * 50)
    print("EditForge v0 complete")
    print(f"Output: {final}")
    print(f"Total time: {total:.2f}s")
    for intent, t in plan.timings.items():
        print(f"  {intent}: {t:.2f}s")


if __name__ == "__main__":
    main()
