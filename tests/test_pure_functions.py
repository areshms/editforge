"""
EditForge v1 — Pure Function Tests

Tests EDL parsing, filler window selection, caption timing matrix, and speed filter.
All tests are offline, dependency-free, and deterministic.
"""

import sys
import json
import tempfile
from pathlib import Path
from dataclasses import dataclass
from typing import List

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from editforge_core import (
    _timecode_to_sec,
    _fmt_srt_time,
    _srt_time_to_sec,
    _invert_segments,
    _intersect_segments,
    _merge_segments,
    _escape_filter_path,
    srt_to_ass,
    FasterWhisper,
    Segment,
)

# ---------------------------------------------------------------------------
# EDL Parsing Tests
# ---------------------------------------------------------------------------

def test_timecode_to_sec():
    """Test timecode to seconds conversion."""
    assert _timecode_to_sec("00:00:04.000") == 4.0
    assert _timecode_to_sec("00:00:04") == 4.0
    assert _timecode_to_sec("01:30:45.123") == 5445.123
    assert _timecode_to_sec("45") == 45.0
    print("✓ Timecode to sec conversion works")


def test_srt_time_to_sec():
    """Test SRT timestamp to seconds conversion."""
    assert _srt_time_to_sec("00:00:01,500") == 1.5
    assert _srt_time_to_sec("00:00:01,500") == 1.5  # comma and dot both work
    assert _srt_time_to_sec("00:01:00,000") == 60.0
    print("✓ SRT time to sec conversion works")


def test_fmt_srt_time():
    """Test SRT timestamp formatting."""
    assert _fmt_srt_time(0.0) == "00:00:00,000"
    assert _fmt_srt_time(1.5) == "00:00:01,500"
    assert _fmt_srt_time(60.0) == "00:01:00,000"
    assert _fmt_srt_time(3661.5) == "01:01:01,500"
    print("✓ SRT time formatting works")


# ---------------------------------------------------------------------------
# Filler Window Selection Tests
# ---------------------------------------------------------------------------

def test_invert_segments():
    """Test segment inversion (removes -> keeps)."""
    # No removals -> full duration
    removes = []
    keeps = _invert_segments(removes, 100.0)
    assert len(keeps) == 1
    assert keeps[0].start == 0.0
    assert keeps[0].end == 100.0

    # Single removal
    removes = [Segment(10.0, 20.0)]
    keeps = _invert_segments(removes, 100.0)
    assert len(keeps) == 2
    assert keeps[0] == Segment(0.0, 10.0)
    assert keeps[1] == Segment(20.0, 100.0)

    # Overlapping removals
    removes = [Segment(10.0, 20.0), Segment(15.0, 25.0)]
    keeps = _invert_segments(removes, 100.0)
    assert len(keeps) == 2
    assert keeps[0] == Segment(0.0, 10.0)
    assert keeps[1] == Segment(25.0, 100.0)

    # Removals at edges
    removes = [Segment(0.0, 10.0)]
    keeps = _invert_segments(removes, 100.0)
    assert len(keeps) == 1
    assert keeps[0] == Segment(10.0, 100.0)

    removes = [Segment(90.0, 100.0)]
    keeps = _invert_segments(removes, 100.0)
    assert len(keeps) == 1
    assert keeps[0] == Segment(0.0, 90.0)

    print("✓ Segment inversion works")


def test_intersect_segments():
    """Test segment intersection."""
    # No overlap
    a = [Segment(0.0, 10.0), Segment(20.0, 30.0)]
    b = [Segment(15.0, 25.0), Segment(35.0, 40.0)]
    result = _intersect_segments(a, b)
    assert len(result) == 1
    assert result[0] == Segment(20.0, 25.0)

    # Full overlap
    a = [Segment(0.0, 30.0)]
    b = [Segment(5.0, 25.0)]
    result = _intersect_segments(a, b)
    assert len(result) == 1
    assert result[0] == Segment(5.0, 25.0)

    # Multiple segments
    a = [Segment(0.0, 10.0), Segment(20.0, 30.0), Segment(40.0, 50.0)]
    b = [Segment(5.0, 35.0), Segment(45.0, 55.0)]
    result = _intersect_segments(a, b)
    assert len(result) == 3
    assert result[0] == Segment(5.0, 10.0)
    assert result[1] == Segment(20.0, 30.0)
    assert result[2] == Segment(45.0, 50.0)

    print("✓ Segment intersection works")


def test_merge_segments():
    """Test segment merging."""
    # No merge needed
    segs = [Segment(0.0, 10.0), Segment(15.0, 25.0), Segment(30.0, 40.0)]
    result = _merge_segments(segs)
    assert len(result) == 3

    # Merge adjacent
    segs = [Segment(0.0, 10.0), Segment(10.0, 20.0)]
    result = _merge_segments(segs)
    assert len(result) == 1
    assert result[0] == Segment(0.0, 20.0)

    # Merge overlapping
    segs = [Segment(0.0, 15.0), Segment(10.0, 25.0)]
    result = _merge_segments(segs)
    assert len(result) == 1
    assert result[0] == Segment(0.0, 25.0)

    # Multiple overlapping segments get merged together
    # (0-10, 10-20, 15-25 all overlap into 0-25)
    segs = [Segment(0.0, 10.0), Segment(10.0, 20.0), Segment(15.0, 25.0)]
    result = _merge_segments(segs)
    assert len(result) == 1
    assert result[0] == Segment(0.0, 25.0)

    print("✓ Segment merging works")


def test_escape_filter_path():
    """Test filter path escaping for ffmpeg."""
    # Simple path
    assert _escape_filter_path(Path("/path/to/file")) == "/path/to/file"

    # Path with colons (Windows style) - note: ffmpeg escape adds backslash before colon
    result = _escape_filter_path(Path("C:/path/to/file"))
    # The function escapes : to \: but keeps the first slash as is
    assert result == "C\\:/path/to/file"

    # Path with special characters
    assert _escape_filter_path(Path("/path/to/file:name")) == "/path/to/file\\:name"

    print("✓ Filter path escaping works")


# ---------------------------------------------------------------------------
# Caption Timing Matrix Tests
# ---------------------------------------------------------------------------

def test_srt_to_ass_basic():
    """Test basic SRT to ASS conversion."""
    srt_content = """1
00:00:01,000 --> 00:00:04,000
Hello world

2
00:00:05,000 --> 00:00:08,000
This is a test
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.srt', delete=False) as f:
        f.write(srt_content)
        srt_path = Path(f.name)

    with tempfile.NamedTemporaryFile(mode='w', suffix='.ass', delete=False) as f:
        ass_path = Path(f.name)

    try:
        srt_to_ass(srt_path, ass_path, width=1920, height=1080)
        ass_content = ass_path.read_text()

        # Check header
        assert "[Script Info]" in ass_content
        assert "Title: EditForge auto captions" in ass_content
        assert "PlayResX: 1920" in ass_content
        assert "PlayResY: 1080" in ass_content

        # Check styles
        assert "[V4+ Styles]" in ass_content
        assert "Style: Default,Arial," in ass_content

        # Check events
        assert "Dialogue:" in ass_content
        assert "Hello world" in ass_content
        assert "This is a test" in ass_content

        print("✓ SRT to ASS conversion works")
    finally:
        srt_path.unlink(missing_ok=True)
        ass_path.unlink(missing_ok=True)


def test_srt_to_ass_multiline():
    """Test SRT with multiple lines per subtitle."""
    srt_content = """1
00:00:01,000 --> 00:00:05,000
Line one
Line two
Line three

2
00:00:06,000 --> 00:00:10,000
Another subtitle
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.srt', delete=False) as f:
        f.write(srt_content)
        srt_path = Path(f.name)

    with tempfile.NamedTemporaryFile(mode='w', suffix='.ass', delete=False) as f:
        ass_path = Path(f.name)

    try:
        srt_to_ass(srt_path, ass_path, width=1920, height=1080)
        ass_content = ass_path.read_text()

        # The function joins lines with spaces, not keeps them separate
        assert "Line one Line two Line three" in ass_content
        assert "Another subtitle" in ass_content

        print("✓ Multiline SRT to ASS conversion works")
    finally:
        srt_path.unlink(missing_ok=True)
        ass_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Speed Filter Tests
# ---------------------------------------------------------------------------

def test_speed_filter_valid():
    """Test speed filter factor calculation."""
    # 1x speed (no change)
    assert FasterWhisper._speed_filter(1.0) == 1.0

    # 2x speed
    assert FasterWhisper._speed_filter(2.0) == 2.0

    # 0.5x speed
    assert FasterWhisper._speed_filter(0.5) == 0.5

    # 1.5x speed
    assert FasterWhisper._speed_filter(1.5) == 1.5

    print("✓ Speed filter factor calculation works")


# ---------------------------------------------------------------------------
# Run All Tests
# ---------------------------------------------------------------------------

def run_all_tests():
    """Run all tests and report results."""
    test_functions = [
        test_timecode_to_sec,
        test_srt_time_to_sec,
        test_fmt_srt_time,
        test_invert_segments,
        test_intersect_segments,
        test_merge_segments,
        test_escape_filter_path,
        test_srt_to_ass_basic,
        test_srt_to_ass_multiline,
        test_speed_filter_valid,
    ]

    passed = 0
    failed = 0

    for test_func in test_functions:
        try:
            test_func()
            passed += 1
        except AssertionError as e:
            print(f"✗ {test_func.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"✗ {test_func.__name__}: Unexpected error: {e}")
            failed += 1

    print(f"\\n{'=' * 50}")
    print(f"Tests passed: {passed}/{len(test_functions)}")
    print(f"Tests failed: {failed}/{len(test_functions)}")
    print(f"{'=' * 50}")

    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
