# Homecam

CN Core 홈캠 `.media` 파일을 MP4(H.264 + AAC)로 변환하는 Python 스크립트입니다.

## 특징

- **A/V 자동 싱크 보정**: `FPS = 비디오 프레임 수 / 오디오 시간`으로 동적 계산하여 싱크 불일치 없음
- **시간 단위 분할**: `--split-hours`로 1시간 단위 MP4 분할 출력
- **2-pass 설계**: 1단계에서 헤더만 읽어 배치 계획 수립 → 2단계에서 실제 데이터 추출 및 인코딩 (불필요한 I/O 최소화)
- **FFmpeg 자동 탐색**: PATH, 스크립트 폴더, Downloads 등 자동 검색
- **오디오 필터 조절**: `--boost`, `--highpass`, `--lowpass` 옵션
- **인코딩 품질 조절**: `--crf`, `--preset` 옵션

## 요구사항

- Python 3.7+
- [FFmpeg](https://github.com/BtbN/FFmpeg-Builds/releases) (`ffmpeg-master-latest-win64-gpl.zip`)

## 설치

FFmpeg를 설치한 뒤 이 스크립트만 받으면 됩니다. 별도 Python 패키지 없음.

```bash
git clone https://github.com/<your-username>/homecam-media-converter.git
cd homecam-media-converter
```

## 사용법

```bash
# 시간 단위 분할 변환 (권장 — 대용량 녹화에 적합)
python media_converter.py "C:\2026\04\06" -o output.mp4 --split-hours

# FFmpeg 경로 직접 지정
python media_converter.py "C:\2026\04\06" -o output.mp4 --split-hours \
    --ffmpeg "C:\ffmpeg\bin\ffmpeg.exe"

# 하나의 파일로 합치기
python media_converter.py "C:\2026\04\06" -o full_day.mp4

# 오디오 볼륨 25dB 증폭, 고품질 인코딩
python media_converter.py "C:\2026\04\06" -o output.mp4 --split-hours \
    --boost 25 --crf 18 --preset slow

# FFmpeg 없이 스트림만 추출 (.h265 + .raw)
python media_converter.py "C:\2026\04\06" --extract-only -o extracted

# 단일 파일 테스트
python media_converter.py "C:\2026\04\06\file.media" -o test.mp4
```

## 옵션

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `input` | — | `.media` 파일 또는 폴더 경로 |
| `-o`, `--output` | `output.mp4` | 출력 파일명 |
| `--ffmpeg` | 자동 탐색 | FFmpeg 실행파일 경로 |
| `--boost` | `15` | 오디오 볼륨 증폭 (dB) |
| `--highpass` | `100` | 오디오 고역통과 필터 주파수 (Hz) |
| `--lowpass` | `7000` | 오디오 저역통과 필터 주파수 (Hz) |
| `--crf` | `23` | H.264 CRF 값 (0-51, 낮을수록 고품질) |
| `--preset` | `fast` | FFmpeg 인코딩 프리셋 |
| `--split-hours` | — | 1시간 단위로 파일 분할 |
| `--extract-only` | — | 스트림만 추출 (FFmpeg 불필요) |
| `--tmpdir` | 출력 폴더 | 임시 파일 저장 위치 |
| `--test` | — | 단일 파일 테스트 모드 |
| `-v`, `--verbose` | — | 상세 로그 출력 |

## .media 파일 포맷

CN Core 홈캠 장비가 생성하는 독자 포맷입니다. 24바이트 청크 헤더 + 페이로드 구조:

| 청크 타입 | 값 | 내용 |
|----------|-------|------|
| I-frame | `0` | H.265 IDR 프레임 |
| P-frame | `1` | H.265 P-프레임 |
| Audio   | `3` | PCM s16le, 16kHz, 모노, 20ms (640바이트) |

## A/V 싱크 원리

오디오 청크 1개 = 정확히 20ms이므로:

```
audio_duration = audio_chunk_count × 0.02s
FPS = video_frame_count / audio_duration
```

이 FPS로 FFmpeg에 입력하면 비디오·오디오 길이가 정확히 일치합니다.
