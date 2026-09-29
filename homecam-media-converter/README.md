# Homecam

A Python script that converts CN Core home-camera `.media` files to MP4 (H.264 + AAC).

## Features

- **Automatic A/V sync correction**: the frame rate is derived dynamically as
  `FPS = video frame count / audio duration`, so video and audio never drift apart
- **Hourly splitting**: `--split-hours` writes one MP4 per hour
- **Two-pass design**: the first pass reads headers only and plans the batches, the second extracts
  and encodes the actual data, which keeps unnecessary I/O to a minimum
- **FFmpeg auto-discovery**: searches `PATH`, the script directory, `Downloads` and other common
  locations
- **Audio filtering**: `--boost`, `--highpass`, `--lowpass`
- **Encoding quality**: `--crf`, `--preset`

## Requirements

- Python 3.7+
- [FFmpeg](https://github.com/BtbN/FFmpeg-Builds/releases) (`ffmpeg-master-latest-win64-gpl.zip`)

## Installation

Install FFmpeg, then take this script. No additional Python packages are needed.

```bash
git clone https://github.com/iislab0115/LabSense.git
cd LabSense/homecam-media-converter
```

## Usage

```bash
# Hourly split conversion (recommended for long recordings)
python media_converter.py "C:\2026\04\06" -o output.mp4 --split-hours

# Point at a specific FFmpeg binary
python media_converter.py "C:\2026\04\06" -o output.mp4 --split-hours \
    --ffmpeg "C:\ffmpeg\bin\ffmpeg.exe"

# Merge everything into a single file
python media_converter.py "C:\2026\04\06" -o full_day.mp4

# Boost the audio by 25 dB and encode at higher quality
python media_converter.py "C:\2026\04\06" -o output.mp4 --split-hours \
    --boost 25 --crf 18 --preset slow

# Extract the raw streams only, without FFmpeg (.h265 + .raw)
python media_converter.py "C:\2026\04\06" --extract-only -o extracted

# Single-file test
python media_converter.py "C:\2026\04\06\file.media" -o test.mp4
```

## Options

| Option | Default | Description |
|------|--------|------|
| `input` | — | Path to a `.media` file or a folder |
| `-o`, `--output` | `output.mp4` | Output file name |
| `--ffmpeg` | auto-discovered | Path to the FFmpeg executable |
| `--boost` | `15` | Audio gain (dB) |
| `--highpass` | `100` | Audio high-pass filter cutoff (Hz) |
| `--lowpass` | `7000` | Audio low-pass filter cutoff (Hz) |
| `--crf` | `23` | H.264 CRF value (0–51; lower is higher quality) |
| `--preset` | `fast` | FFmpeg encoding preset |
| `--split-hours` | — | Split the output into one file per hour |
| `--extract-only` | — | Extract the streams only (FFmpeg not required) |
| `--tmpdir` | output folder | Where temporary files are written |
| `--test` | — | Single-file test mode |
| `-v`, `--verbose` | — | Verbose logging |

## The `.media` file format

A proprietary format produced by CN Core home-camera devices: a 24-byte chunk header followed by the
payload.

| Chunk type | Value | Contents |
|----------|-------|------|
| I-frame | `0` | H.265 IDR frame |
| P-frame | `1` | H.265 P-frame |
| Audio   | `3` | PCM s16le, 16 kHz, mono, 20 ms (640 bytes) |

## How the A/V sync works

One audio chunk is exactly 20 ms, so:

```
audio_duration = audio_chunk_count × 0.02 s
FPS = video_frame_count / audio_duration
```

Feeding that frame rate to FFmpeg makes the video and audio durations match exactly.
