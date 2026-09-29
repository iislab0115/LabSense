#!/usr/bin/env python3
"""
CN Core Homecam .media file converter
====================================
Converts .media files to MP4 (H.264 + AAC) with automatic A/V sync.

A/V sync method: FPS = video_frames / audio_duration
  Audio duration is derived from the count of 20ms PCM chunks, giving
  a precise denominator so video and audio lengths always match exactly.

Usage:
  python media_converter.py "C:\\2026\\04\\06" -o output.mp4 --split-hours
  python media_converter.py "C:\\2026\\04\\06" -o output.mp4 --split-hours --ffmpeg "C:\\ffmpeg\\bin\\ffmpeg.exe"

Requirements:
  - Python 3.7+
  - FFmpeg (auto-detected or specified via --ffmpeg)
"""

import struct
import os
import sys
import shutil
import subprocess
import argparse
import time
from pathlib import Path
from typing import List, Optional, Tuple


# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────

CHUNK_HEADER_SIZE = 24
AUDIO_SAMPLE_RATE = 16000
AUDIO_SAMPLE_FORMAT = "s16le"
AUDIO_CHANNELS = 1
AUDIO_CHUNK_DURATION_S = 0.02  # 20ms per chunk (640 bytes @ 16kHz 16-bit mono)
VIDEO_CHUNK_TYPES = frozenset((0, 1))  # 0=I-frame, 1=P-frame
AUDIO_CHUNK_TYPE = 3
MAX_CHUNK_SIZE = 500_000


# ──────────────────────────────────────────────
# FFmpeg discovery
# ──────────────────────────────────────────────

def find_ffmpeg(user_path: Optional[str] = None) -> Optional[str]:
    if user_path:
        if os.path.isfile(user_path):
            return user_path
        for sub in ("ffmpeg.exe", "ffmpeg", os.path.join("bin", "ffmpeg.exe")):
            candidate = os.path.join(user_path, sub)
            if os.path.isfile(candidate):
                return candidate
        print(f"WARNING: FFmpeg not found at specified path: {user_path}")

    result = shutil.which("ffmpeg")
    if result:
        return result

    script_dir = os.path.dirname(os.path.abspath(__file__))
    for name in ("ffmpeg.exe", "ffmpeg"):
        candidate = os.path.join(script_dir, name)
        if os.path.isfile(candidate):
            return candidate

    if sys.platform == "win32":
        search_dirs = [
            os.path.expanduser(r"~\Downloads"),
            os.path.expanduser(r"~\Desktop"),
            r"C:\\",
            r"C:\Program Files",
            r"C:\tools",
        ]
        for search_dir in search_dirs:
            if not os.path.isdir(search_dir):
                continue
            try:
                for item in os.listdir(search_dir):
                    if item.lower().startswith("ffmpeg"):
                        for sub in (os.path.join("bin", "ffmpeg.exe"), "ffmpeg.exe"):
                            candidate = os.path.join(search_dir, item, sub)
                            if os.path.isfile(candidate):
                                return candidate
            except PermissionError:
                continue

    return None


# ──────────────────────────────────────────────
# .media file format parser
# ──────────────────────────────────────────────

def count_frames(filepath: str) -> Tuple[int, int]:
    """
    Fast scan: read chunk headers only, skip over payloads.
    Returns (video_frame_count, audio_chunk_count).

    ~2x faster than parse_and_extract for the planning pass because
    it avoids loading payload bytes into memory.
    """
    v_count = 0
    a_count = 0
    with open(filepath, "rb") as f:
        while True:
            header = f.read(CHUNK_HEADER_SIZE)
            if len(header) < CHUNK_HEADER_SIZE:
                break
            chunk_type, chunk_size = struct.unpack_from("<II", header)
            if chunk_type not in (*VIDEO_CHUNK_TYPES, AUDIO_CHUNK_TYPE):
                break
            if chunk_size == 0 or chunk_size > MAX_CHUNK_SIZE:
                break
            if chunk_type in VIDEO_CHUNK_TYPES:
                v_count += 1
            else:
                a_count += 1
            f.seek(chunk_size, 1)  # skip payload
    return v_count, a_count


def parse_and_extract(filepath: str) -> Tuple[bytes, bytes, int, int]:
    """
    Full parse: returns (video_bytes, audio_bytes, video_frame_count, audio_chunk_count).
    video_bytes contains concatenated H.265 NAL units.
    audio_bytes contains raw PCM s16le samples.
    """
    with open(filepath, "rb") as f:
        data = f.read()

    video_parts: List[bytes] = []
    audio_parts: List[bytes] = []
    v_count = 0
    a_count = 0

    pos = 0
    while pos + CHUNK_HEADER_SIZE <= len(data):
        chunk_type, chunk_size = struct.unpack_from("<II", data, pos)

        if chunk_type not in (*VIDEO_CHUNK_TYPES, AUDIO_CHUNK_TYPE):
            break
        if chunk_size == 0 or chunk_size > MAX_CHUNK_SIZE:
            break
        if pos + CHUNK_HEADER_SIZE + chunk_size > len(data):
            break

        payload = data[pos + CHUNK_HEADER_SIZE : pos + CHUNK_HEADER_SIZE + chunk_size]

        if chunk_type in VIDEO_CHUNK_TYPES:
            video_parts.append(payload)
            v_count += 1
        else:
            audio_parts.append(payload)
            a_count += 1

        pos += CHUNK_HEADER_SIZE + chunk_size

    return b"".join(video_parts), b"".join(audio_parts), v_count, a_count


# ──────────────────────────────────────────────
# File discovery
# ──────────────────────────────────────────────

def find_media_files(root_dir: str) -> List[str]:
    """Find all .media files (case-insensitive) under root_dir, sorted by path."""
    root = Path(root_dir)
    seen: set = set()
    files: List[str] = []

    for pattern in ("*.media", "*.MEDIA", "*.Media"):
        for f in root.rglob(pattern):
            key = str(f).lower()
            if key not in seen:
                seen.add(key)
                files.append(str(f))

    files.sort()
    return files


# ──────────────────────────────────────────────
# Audio filter helper
# ──────────────────────────────────────────────

def make_audio_filter(boost_db: int, highpass: int = 100, lowpass: int = 7000) -> str:
    return f"volume={boost_db}dB,highpass=f={highpass},lowpass=f={lowpass}"


# ──────────────────────────────────────────────
# FFmpeg runner
# ──────────────────────────────────────────────

def run_ffmpeg(ffmpeg_path: str, args: List[str], label: str = "") -> bool:
    cmd = [ffmpeg_path] + args
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            errors="replace",
        )
        last_progress = time.time()
        stderr_lines: List[str] = []
        for line in process.stderr:
            stderr_lines.append(line)
            if "frame=" in line and time.time() - last_progress > 3:
                print(f"   ... {label} {line.strip()[:80]}", end="\r")
                last_progress = time.time()

        process.wait()
        print()

        if process.returncode != 0:
            print(f"   ERROR: FFmpeg failed (exit code {process.returncode}):")
            for errline in stderr_lines[-10:]:
                if errline.strip():
                    print(f"      {errline.rstrip()}")
            return False
        return True

    except FileNotFoundError:
        print(f"ERROR: Cannot run FFmpeg: {ffmpeg_path}")
        print("  Use --ffmpeg to specify the path to ffmpeg.exe")
        return False
    except Exception as e:
        print(f"ERROR: FFmpeg error: {e}")
        return False


# ──────────────────────────────────────────────
# Core: hour-split conversion with dynamic FPS
# ──────────────────────────────────────────────

def convert_split_synced(
    media_files: List[str],
    output_path: str,
    ffmpeg_path: str,
    audio_boost_db: int = 15,
    highpass: int = 100,
    lowpass: int = 7000,
    crf: int = 23,
    preset: str = "fast",
    verbose: bool = False,
    tmpdir_base: Optional[str] = None,
) -> None:
    """
    Convert media files to MP4, splitting into one file per hour.

    Two-pass approach:
      Pass 1 (count_frames): read headers only to plan hour boundaries.
      Pass 2 (parse_and_extract): read payloads and write temp streams.
    FPS is computed per batch as video_frames / audio_seconds so that
    video and audio durations match exactly.
    """
    if not media_files:
        print("ERROR: No .media files found.")
        return

    total_files = len(media_files)
    base, ext = os.path.splitext(output_path)

    print(f"Found {total_files} .media files")
    print(f"  First: {media_files[0]}")
    print(f"  Last:  {media_files[-1]}")
    print()

    if tmpdir_base is None:
        tmpdir_base = os.path.dirname(os.path.abspath(output_path))

    tmpdir = os.path.join(tmpdir_base, "_media_conv_temp")
    os.makedirs(tmpdir, exist_ok=True)
    print(f"Temp dir: {tmpdir}")
    print()

    try:
        # ── Pass 1: scan files, plan hour batches ──
        print("Pass 1: scanning files and planning batches...")
        start_time = time.time()

        HOUR_SECONDS = 3600.0
        batches: List[dict] = []

        current_batch: dict = {"hour": 0, "files": [], "total_v": 0, "total_a": 0}
        cumulative_audio_s = 0.0

        for idx, filepath in enumerate(media_files):
            try:
                v_count, a_count = count_frames(filepath)

                if v_count == 0 and a_count == 0:
                    if verbose:
                        print(f"  WARNING: empty file: {filepath}")
                    continue

                file_audio_s = a_count * AUDIO_CHUNK_DURATION_S
                next_boundary = (current_batch["hour"] + 1) * HOUR_SECONDS

                if cumulative_audio_s + file_audio_s > next_boundary and current_batch["files"]:
                    batches.append(current_batch)
                    current_batch = {
                        "hour": current_batch["hour"] + 1,
                        "files": [],
                        "total_v": 0,
                        "total_a": 0,
                    }

                current_batch["files"].append((filepath, v_count, a_count))
                current_batch["total_v"] += v_count
                current_batch["total_a"] += a_count
                cumulative_audio_s += file_audio_s

            except Exception as e:
                if verbose:
                    print(f"  WARNING: scan error {filepath}: {e}")
                continue

            if (idx + 1) % 200 == 0 or idx == total_files - 1:
                pct = (idx + 1) / total_files * 100
                elapsed = time.time() - start_time
                eta = elapsed / (idx + 1) * (total_files - idx - 1)
                print(
                    f"  {idx+1}/{total_files} ({pct:.0f}%) - "
                    f"cumulative: {cumulative_audio_s/3600:.1f}h - "
                    f"ETA: {eta:.0f}s"
                )

        if current_batch["files"]:
            batches.append(current_batch)

        total_v = sum(b["total_v"] for b in batches)
        total_a = sum(b["total_a"] for b in batches)
        total_audio_s = total_a * AUDIO_CHUNK_DURATION_S

        print()
        print("Scan complete.")
        print(f"  Total video frames : {total_v:,}")
        print(f"  Total audio chunks : {total_a:,}")
        print(f"  Total audio time   : {total_audio_s:.1f}s ({total_audio_s/3600:.1f}h)")
        print(f"  Batches            : {len(batches)}")
        print()

        print(f"  {'Hour':>4}  {'Files':>6}  {'Video frames':>13}  {'Audio':>10}  {'FPS':>7}")
        print("  " + "-" * 50)
        for b in batches:
            a_dur = b["total_a"] * AUDIO_CHUNK_DURATION_S
            fps = b["total_v"] / a_dur if a_dur > 0 else 15.0
            print(
                f"  {b['hour']:02d}:00  {len(b['files']):>6}  "
                f"{b['total_v']:>13,}  {a_dur/60:>8.1f}min  {fps:>7.2f}"
            )
        print()

        # ── Pass 2: encode each batch ──
        print(f"Pass 2: encoding {len(batches)} batch(es)...")
        print()

        audio_filter = make_audio_filter(audio_boost_db, highpass, lowpass)
        success_count = 0

        for batch_idx, batch in enumerate(batches):
            hour = batch["hour"]
            out_file = f"{base}_{hour:02d}h{ext}"

            batch_audio_s = batch["total_a"] * AUDIO_CHUNK_DURATION_S
            batch_fps = batch["total_v"] / batch_audio_s if batch_audio_s > 0 else 15.0

            print(f"  [{batch_idx+1}/{len(batches)}] {os.path.basename(out_file)}")
            print(
                f"    files={len(batch['files'])}  fps={batch_fps:.4f}  "
                f"duration={batch_audio_s/60:.1f}min"
            )

            batch_video = os.path.join(tmpdir, f"video_{hour:02d}.h265")
            batch_audio = os.path.join(tmpdir, f"audio_{hour:02d}.raw")

            extract_start = time.time()
            with open(batch_video, "wb") as vf, open(batch_audio, "wb") as af:
                for file_idx, (filepath, _, _) in enumerate(batch["files"]):
                    try:
                        v_data, a_data, _, _ = parse_and_extract(filepath)
                        vf.write(v_data)
                        af.write(a_data)
                    except Exception as e:
                        if verbose:
                            print(f"    WARNING: {filepath}: {e}")
                        continue

                    if (file_idx + 1) % 100 == 0:
                        print(f"    extracting: {file_idx+1}/{len(batch['files'])}", end="\r")

            extract_time = time.time() - extract_start
            print(f"    extraction done ({extract_time:.0f}s)        ")

            ffmpeg_args = [
                "-y",
                "-r", f"{batch_fps:.4f}",
                "-i", batch_video,
                "-f", AUDIO_SAMPLE_FORMAT,
                "-ar", str(AUDIO_SAMPLE_RATE),
                "-ac", str(AUDIO_CHANNELS),
                "-i", batch_audio,
                "-c:v", "libx264",
                "-preset", preset,
                "-crf", str(crf),
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", "128k",
                "-af", audio_filter,
                "-shortest",
                out_file,
            ]

            encode_start = time.time()
            success = run_ffmpeg(ffmpeg_path, ffmpeg_args, label=f"{hour:02d}h")
            encode_time = time.time() - encode_start

            try:
                os.remove(batch_video)
                os.remove(batch_audio)
            except Exception:
                pass

            if success and os.path.isfile(out_file):
                size_mb = os.path.getsize(out_file) / 1024 / 1024
                total_time = extract_time + encode_time
                print(f"    Done: {size_mb:.1f}MB in {total_time:.0f}s")
                success_count += 1
            else:
                print("    FAILED: encoding error")

            print()

        print("=" * 55)
        print(f"Conversion complete: {success_count}/{len(batches)} succeeded")
        print(f"Output: {os.path.dirname(os.path.abspath(output_path))}")

    finally:
        print()
        print("Cleaning up temp files...")
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
            print("  Done.")
        except Exception:
            print(f"  WARNING: manual cleanup may be needed: {tmpdir}")


# ──────────────────────────────────────────────
# Single MP4 output
# ──────────────────────────────────────────────

def convert_single_synced(
    media_files: List[str],
    output_path: str,
    ffmpeg_path: str,
    audio_boost_db: int = 15,
    highpass: int = 100,
    lowpass: int = 7000,
    crf: int = 23,
    preset: str = "fast",
    verbose: bool = False,
    tmpdir_base: Optional[str] = None,
) -> None:
    """Convert all media files to a single MP4 with auto FPS calculation."""
    if not media_files:
        print("ERROR: No .media files found.")
        return

    total_files = len(media_files)
    print(f"Found {total_files} .media files")
    print()

    if tmpdir_base is None:
        tmpdir_base = os.path.dirname(os.path.abspath(output_path))

    tmpdir = os.path.join(tmpdir_base, "_media_conv_temp")
    os.makedirs(tmpdir, exist_ok=True)

    try:
        video_path = os.path.join(tmpdir, "video.h265")
        audio_path = os.path.join(tmpdir, "audio.raw")

        total_v = 0
        total_a = 0

        print("Extracting streams...")
        start_time = time.time()

        with open(video_path, "wb") as vf, open(audio_path, "wb") as af:
            for idx, filepath in enumerate(media_files):
                try:
                    v_data, a_data, vc, ac = parse_and_extract(filepath)
                    vf.write(v_data)
                    af.write(a_data)
                    total_v += vc
                    total_a += ac
                except Exception as e:
                    if verbose:
                        print(f"  WARNING: {filepath}: {e}")
                    continue

                if (idx + 1) % 50 == 0 or idx == total_files - 1:
                    pct = (idx + 1) / total_files * 100
                    elapsed = time.time() - start_time
                    eta = elapsed / (idx + 1) * (total_files - idx - 1)
                    print(f"  {idx+1}/{total_files} ({pct:.0f}%) - ETA: {eta:.0f}s")

        audio_dur = total_a * AUDIO_CHUNK_DURATION_S
        fps = total_v / audio_dur if audio_dur > 0 else 15.0

        print()
        print("Extraction complete.")
        print(f"  Video frames : {total_v:,}")
        print(f"  Audio        : {audio_dur:.1f}s ({audio_dur/3600:.1f}h)")
        print(f"  Computed FPS : {fps:.4f}")
        print()

        audio_filter = make_audio_filter(audio_boost_db, highpass, lowpass)

        print("Encoding MP4...")
        ffmpeg_args = [
            "-y",
            "-r", f"{fps:.4f}",
            "-i", video_path,
            "-f", AUDIO_SAMPLE_FORMAT,
            "-ar", str(AUDIO_SAMPLE_RATE),
            "-ac", str(AUDIO_CHANNELS),
            "-i", audio_path,
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", str(crf),
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            "-af", audio_filter,
            "-shortest",
            output_path,
        ]

        success = run_ffmpeg(ffmpeg_path, ffmpeg_args, label="full")
        if success:
            size_mb = os.path.getsize(output_path) / 1024 / 1024
            print(f"Done: {output_path} ({size_mb:.1f}MB)")

    finally:
        print("Cleaning up temp files...")
        shutil.rmtree(tmpdir, ignore_errors=True)
        print("  Done.")


# ──────────────────────────────────────────────
# Extract streams only (no FFmpeg required)
# ──────────────────────────────────────────────

def extract_only(
    media_files: List[str],
    output_dir: str,
    verbose: bool = False,
) -> None:
    os.makedirs(output_dir, exist_ok=True)
    video_path = os.path.join(output_dir, "video.h265")
    audio_path = os.path.join(output_dir, "audio.raw")

    total_files = len(media_files)
    total_v = 0
    total_a = 0

    print(f"Extracting streams from {total_files} files...")
    start_time = time.time()

    with open(video_path, "wb") as vf, open(audio_path, "wb") as af:
        for idx, filepath in enumerate(media_files):
            try:
                v_data, a_data, vc, ac = parse_and_extract(filepath)
                vf.write(v_data)
                af.write(a_data)
                total_v += vc
                total_a += ac
            except Exception:
                continue

            if (idx + 1) % 100 == 0 or idx == total_files - 1:
                pct = (idx + 1) / total_files * 100
                elapsed = time.time() - start_time
                eta = elapsed / (idx + 1) * (total_files - idx - 1)
                print(f"  {idx+1}/{total_files} ({pct:.0f}%) - ETA: {eta:.0f}s")

    audio_dur = total_a * AUDIO_CHUNK_DURATION_S
    fps = total_v / audio_dur if audio_dur > 0 else 15.0

    v_size = os.path.getsize(video_path) / 1024 / 1024
    a_size = os.path.getsize(audio_path) / 1024 / 1024
    print()
    print("Extraction complete.")
    print(f"  {video_path} ({v_size:.1f}MB)")
    print(f"  {audio_path} ({a_size:.1f}MB)")
    print(f"  Frames: {total_v:,}  Audio: {audio_dur:.1f}s")
    print(f"  Computed FPS: {fps:.4f}")
    print()
    print("Manual FFmpeg command:")
    print(
        f'  ffmpeg -r {fps:.4f} -i "{video_path}" '
        f'-f s16le -ar 16000 -ac 1 -i "{audio_path}" '
        f'-c:v libx264 -preset fast -crf 23 -c:a aac -b:a 128k '
        f'-af "volume=15dB,highpass=f=100,lowpass=f=7000" -shortest output.mp4'
    )


# ──────────────────────────────────────────────
# Single-file test
# ──────────────────────────────────────────────

def test_single_file(
    filepath: str,
    output_path: str,
    audio_boost_db: int,
    ffmpeg_path: str,
    highpass: int = 100,
    lowpass: int = 7000,
    crf: int = 23,
    preset: str = "fast",
) -> None:
    print(f"Test conversion: {filepath}")
    vc, ac = count_frames(filepath)
    audio_dur = ac * AUDIO_CHUNK_DURATION_S
    fps = vc / audio_dur if audio_dur > 0 else 15.0
    print(f"  Video: {vc} frames  Audio: {ac} chunks ({audio_dur:.2f}s)")
    print(f"  Computed FPS: {fps:.4f}")
    convert_single_synced(
        [filepath], output_path, ffmpeg_path,
        audio_boost_db=audio_boost_db,
        highpass=highpass, lowpass=lowpass,
        crf=crf, preset=preset,
    )


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="CN Core Homecam .media → MP4 converter (auto A/V sync)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=r"""
Examples:
  # Split by hour (recommended for long recordings)
  python media_converter.py "C:\2026\04\06" -o output.mp4 --split-hours

  # Specify FFmpeg path
  python media_converter.py "C:\2026\04\06" -o output.mp4 --split-hours ^
      --ffmpeg "C:\ffmpeg\bin\ffmpeg.exe"

  # Boost audio 25dB, higher quality encode
  python media_converter.py "C:\2026\04\06" -o output.mp4 --split-hours --boost 25 --crf 18

  # Single combined MP4 (watch disk space for long recordings)
  python media_converter.py "C:\2026\04\06" -o full_day.mp4

  # Extract raw streams only (no FFmpeg needed)
  python media_converter.py "C:\2026\04\06" --extract-only -o extracted

FFmpeg installation:
  1) https://github.com/BtbN/FFmpeg-Builds/releases
  2) Download ffmpeg-master-latest-win64-gpl.zip
  3) Extract and pass: --ffmpeg "<extracted_path>\bin\ffmpeg.exe"
        """,
    )

    parser.add_argument("input", help=".media file or folder path")
    parser.add_argument("-o", "--output", default="output.mp4",
                        help="Output MP4 filename (default: output.mp4)")
    parser.add_argument("--ffmpeg", default=None,
                        help="Path to ffmpeg executable")
    parser.add_argument("--boost", type=int, default=15,
                        help="Audio volume boost in dB (default: 15)")
    parser.add_argument("--highpass", type=int, default=100,
                        help="Audio highpass filter frequency in Hz (default: 100)")
    parser.add_argument("--lowpass", type=int, default=7000,
                        help="Audio lowpass filter frequency in Hz (default: 7000)")
    parser.add_argument("--crf", type=int, default=23,
                        help="H.264 CRF quality value 0-51, lower=better (default: 23)")
    parser.add_argument("--preset", default="fast",
                        choices=["ultrafast", "superfast", "veryfast", "faster",
                                 "fast", "medium", "slow", "slower", "veryslow"],
                        help="FFmpeg encoding preset (default: fast)")
    parser.add_argument("--split-hours", action="store_true",
                        help="Split output into one file per hour")
    parser.add_argument("--extract-only", action="store_true",
                        help="Extract raw streams only (no FFmpeg needed)")
    parser.add_argument("--tmpdir", default=None,
                        help="Temp file location (default: same folder as output)")
    parser.add_argument("--test", action="store_true",
                        help="Test mode: convert a single .media file")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show detailed logs")

    args = parser.parse_args()
    input_path = args.input

    print("=" * 55)
    print("  CN Core Homecam .media → MP4 Converter")
    print("  (auto A/V sync via dynamic FPS)")
    print("=" * 55)
    print()

    # ── extract-only mode ──
    if args.extract_only:
        if os.path.isdir(input_path):
            media_files = find_media_files(input_path)
            if not media_files:
                print(f"ERROR: No .media files found in: {input_path}")
                return
            extract_only(media_files, args.output, verbose=args.verbose)
        else:
            print(f"ERROR: Not a directory: {input_path}")
        return

    # ── locate FFmpeg ──
    ffmpeg_path = find_ffmpeg(args.ffmpeg)
    if not ffmpeg_path:
        print("ERROR: FFmpeg not found!")
        print()
        print("  1) https://github.com/BtbN/FFmpeg-Builds/releases")
        print("     Download: ffmpeg-master-latest-win64-gpl.zip")
        print("  2) Extract the archive")
        print(f'  3) Rerun with: --ffmpeg "<path>\\bin\\ffmpeg.exe"')
        return

    try:
        ver = subprocess.run(
            [ffmpeg_path, "-version"],
            capture_output=True, text=True, errors="replace",
        )
        first_line = ver.stdout.split("\n")[0] if ver.stdout else "?"
        print(f"FFmpeg: {ffmpeg_path}")
        print(f"  {first_line}")
    except Exception:
        print(f"FFmpeg: {ffmpeg_path}")
    print()

    # ── single-file test ──
    if args.test or input_path.lower().endswith(".media"):
        if os.path.isfile(input_path):
            test_single_file(
                input_path, args.output, args.boost, ffmpeg_path,
                highpass=args.highpass, lowpass=args.lowpass,
                crf=args.crf, preset=args.preset,
            )
        else:
            print(f"ERROR: File not found: {input_path}")
        return

    # ── batch folder conversion ──
    if os.path.isdir(input_path):
        media_files = find_media_files(input_path)
        if not media_files:
            print(f"ERROR: No .media files found in: {input_path}")
            return

        common_kwargs = dict(
            audio_boost_db=args.boost,
            highpass=args.highpass,
            lowpass=args.lowpass,
            crf=args.crf,
            preset=args.preset,
            verbose=args.verbose,
            tmpdir_base=args.tmpdir,
        )

        if args.split_hours:
            convert_split_synced(media_files, args.output, ffmpeg_path, **common_kwargs)
        else:
            convert_single_synced(media_files, args.output, ffmpeg_path, **common_kwargs)
    else:
        print(f"ERROR: Path not found: {input_path}")


if __name__ == "__main__":
    main()
