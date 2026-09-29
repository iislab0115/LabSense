# Integrated Smart-Home Data Collection System (Shelly + SmartThings)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/Python-3.9%2B-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg)](#system-requirements)
[![Release](https://img.shields.io/github/v/release/lime9903/data-collection?include_prereleases&label=Release)](https://github.com/lime9903/data-collection/releases)
[![Last Commit](https://img.shields.io/github/last-commit/lime9903/data-collection)](https://github.com/lime9903/data-collection/commits/main)
[![Cite](https://img.shields.io/badge/Cite-CITATION.cff-informational.svg)](CITATION.cff)
<!-- Uncomment after minting a Zenodo DOI:
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.XXXXXXX.svg)](https://doi.org/10.5281/zenodo.XXXXXXX)
-->

Reference implementation and data-collection framework for the study
*"Ambient sensor time-series data for occupancy and activity recognition in a
laboratory workspace"* (Kim, Lee, Lee, Roh, & Hwang, 2026).

The system simultaneously acquires high-frequency power measurements from
Shelly smart plugs and event-driven / polled telemetry from SmartThings
devices (plugs, motion sensors, contact sensors, stick vacuums, and cameras)
within a single Python process. Data are written to per-device CSV files
organized by date, and an optional desktop dashboard (Tkinter) provides
real-time monitoring.

The code is released under the MIT License to support reproducibility of the
associated publication.

## Supported Devices and Sampling Strategy

| Device | Acquisition Path | Sampling Interval |
|---|---|---|
| Shelly smart plug (`SMP01`–`SMP12`) | Local HTTP (Gen2 RPC / Gen1 `/status`) | 1 s |
| SmartThings smart plug (`SMP13`–`SMP19`) | SmartThings REST `/devices/{id}/status` + `/history/devices` | 60 s (polling) + event history |
| Motion sensor (`MS*`) | SmartThings `/history/devices` | 60 s (event-driven) |
| Contact / door sensor (`DS*`) | SmartThings `/history/devices` | 60 s (event-driven) |
| Stick vacuum | SmartThings `/devices/{id}/status` (`powerConsumptionReport`) | 60 s |
| Camera | SmartThings `/history/devices` (motion / sound events) | 60 s |

---

## Table of Contents

1. [System Requirements](#system-requirements)
2. [Installation](#installation)
3. [Configuration](#configuration)
4. [Running the Collector](#running-the-collector)
5. [Data Output Structure](#data-output-structure)
6. [Monitoring](#monitoring)
7. [Analysis Examples](#analysis-examples)
8. [Troubleshooting](#troubleshooting)
9. [Operational Notes](#operational-notes)
10. [Citation](#citation)
11. [License](#license)
12. [Acknowledgments](#acknowledgments)

---

## System Requirements

- Python 3.9 or later
- Windows 10/11, macOS, or Linux (tested primarily on Windows 10/11)
- Local network connectivity to Shelly devices (HTTP)
- Internet connectivity for the SmartThings REST API
- A registered SmartThings application (Client ID / Client Secret) and a
  Personal Access Token; see the
  [SmartThings Developer Workspace](https://smartthings.developer.samsung.com/)

---

## Installation

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
```

Or install the primary packages directly:

```bash
pip install requests pandas matplotlib aiohttp
```

### 2. Project layout

```
data-collection/
├── main.py                # Unified entry point (Shelly + SmartThings + dashboard)
├── shelly_collector.py    # Shelly acquisition (SMP01–SMP12)
├── smartthings_worker.py  # SmartThings acquisition (SMP13–SMP19 / MS / DS / vacuum / camera)
├── dashboard.py           # Tkinter dashboard
├── smartthings_auth.py    # One-time SmartThings OAuth bootstrap
├── run_persistent.py      # Persistent supervisor (auto-restart wrapper)
├── run_collector.sh       # Shell-based auto-restart launcher (optional)
├── config.json            # Configuration template (placeholder values; committed to VCS)
├── config.local.json      # User-specific values (git-ignored; created by the user)
├── requirements.txt
└── README.md
```

---

## Configuration

The repository ships `config.json` as a **template with placeholder values**.
User-specific values (credentials, absolute paths) should be placed in a
separate file named `config.local.json`, which is excluded from version control
via `.gitignore`.

At runtime, all modules load `config.local.json` if it exists and fall back to
`config.json` otherwise.

### 1. Create your local configuration

```powershell
# Windows PowerShell
Copy-Item config.json config.local.json
```

```bash
# Linux / macOS / Git Bash
cp config.json config.local.json
```

### 2. Fill in the required fields in `config.local.json`

| Key | Description |
|---|---|
| `SHELLY_DEVICES` | **Required for Shelly collection.** One entry per plug: `label` (becomes the CSV file prefix, e.g. `SMP01`), `id` (the device identifier the plug reports) and `ip` (its address on your local network). The collector refuses to start while these are still placeholders. |
| `SHELLY_INTERVAL` | Shelly polling interval in seconds, default `1.0` |
| `SHELLY_TIMEOUT` | Shelly HTTP request timeout in seconds, default `4.0` |
| `CLIENT_ID` | SmartThings OAuth Client ID |
| `CLIENT_SECRET` | SmartThings OAuth Client Secret |
| `PAT_TOKEN` | SmartThings Personal Access Token (fallback for REST calls) |
| `REDIRECT_URI` | OAuth redirect URI (`https://httpbin.org/get` works for CLI use) |
| `LOG_FILE` | Absolute path for the SmartThings worker log file |
| `TOKEN_FILE` | Absolute path where the OAuth token is persisted |
| `CSV_BASE_DIR` | Absolute base directory for CSV output |
| `METADATA_FILE` | Absolute path for the device-metadata JSON |
| `BAN_LIST_FILE` | Absolute path for the ban-list JSON |
| `CLIP_DIR` | Absolute directory for camera clips |
| `DEVICE_UPDATE_INTERVAL` | Device-list refresh interval (seconds), default `600` |
| `DEVICE_STATUS_INTERVAL` | Device-status polling interval (seconds), default `60` |
| `MAX_RETRIES` | Maximum retry count for transient failures, default `5` |
| `BASE_RETRY_WAIT` | Initial exponential-backoff wait (seconds), default `5` |
| `MAX_RETRY_WAIT` | Maximum exponential-backoff wait (seconds), default `60` |

### 3. Obtain the initial OAuth token (once)

After filling in `CLIENT_ID` and `CLIENT_SECRET`:

```bash
python smartthings_auth.py
```

This command performs the OAuth flow and writes the token to the path specified
by `TOKEN_FILE`. The `refresh_token` is auto-renewed for as long as the
collector is running; it expires only after 29 days of inactivity.

---

## Running the Collector

### Option 1 — Unified execution (recommended)

```bash
# With desktop dashboard (Tkinter)
python main.py

# Headless (background collection only)
python main.py --headless
```

The Shelly and SmartThings workers run concurrently as daemon threads within
a single process. `Ctrl+C` triggers a graceful shutdown of both workers.

### Option 2 — Shelly-only mode

```bash
python shelly_collector.py
```

### Option 3 — Persistent auto-restart

```bash
# Linux / macOS
chmod +x run_collector.sh
./run_collector.sh

# Windows (Git Bash)
bash run_collector.sh
```

The script repeatedly re-invokes `python main.py --headless`, so the process
is restarted immediately after any unexpected termination.
Alternatively, `python run_persistent.py` provides the same behavior in pure
Python.

### Option 4 — systemd service (Linux)

`/etc/systemd/system/plug-collector.service`:

```ini
[Unit]
Description=Plug collection (Shelly + SmartThings)
After=network.target

[Service]
Type=simple
User=your_username
WorkingDirectory=/path/to/project
ExecStart=/usr/bin/python3 /path/to/project/main.py --headless
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now plug-collector
sudo journalctl -u plug-collector -f
```

---

## Data Output Structure

CSV files are written under the directory specified by `CSV_BASE_DIR`,
partitioned by date (`YYYYMMDD`) and by device. No combined/rollup files are
produced by the collector; downstream aggregation is left to analysis code.

```
<CSV_BASE_DIR>/
├── 20260511/
│   ├── SMP01_9070694ae97c_20260511.csv                       # Shelly plug (1 s)
│   ├── SMP02_907069496a08_20260511.csv
│   ├── ...
│   ├── SMP13_<deviceId>_20260511.csv                         # SmartThings plug (60 s + history)
│   ├── SMP14_<deviceId>_20260511.csv
│   ├── ...
│   ├── MS1_<deviceId>_20260511.csv                           # Motion sensor (event-driven)
│   ├── DS1_<deviceId>_20260511.csv                           # Door sensor (event-driven)
│   ├── Stick vacuum_<deviceId>_20260511.csv                  # Vacuum (60 s)
│   ├── Stick vacuum_<deviceId>_20260511_dustbin_events.csv   # Dustbin-empty events only
│   └── Camera_20260511.csv                                   # All cameras: motion / sound events
├── logs/
│   ├── collector_YYYYMMDD.log                                # Shelly worker log
│   └── smartthings.log                                       # SmartThings worker log (config LOG_FILE)
└── 20260512/
    └── ...
```

### CSV Schema (representative)

| Category | Columns |
|---|---|
| SMP01–SMP12 (Shelly) | `Timestamp, Power (W), Energy (Wh), Energy Inc (Wh), Voltage (V), Current (A), Temp (C)` |
| SMP13–SMP19 (SmartThings) | `Timestamp, Label, Location, Room, Switch, Power (W), Energy (Wh)` |
| MS\* (motion) | `Timestamp, Label, Location, Room, Motion, Temperature (°C)` |
| DS\* (contact) | `Timestamp, Label, Location, Room, Contact, Temperature (°C), Battery (%)` |
| Vacuum | `Timestamp, Label, Location, Room, Power (W), Energy (Wh), Cleaner State, Dustbin State, Dustbin Last Emptied (UTC)` |
| Camera | `Timestamp, Camera, Motion, Sound` |

> **Note.** A SmartThings plug CSV (`SMP13`–`SMP19`) interleaves two record
> types: (i) 60-second `status` polls with second-resolution timestamps and
> (ii) event records from the history API with millisecond-resolution
> timestamps. Downstream analysis code can simply sort by `Timestamp`.

---

## Monitoring

### 1. Desktop dashboard

```bash
python main.py
```

- Top summary cards: **SMP (live)**, **SMP OK**, **SMP Fail**
  (aggregated across Shelly and SmartThings plugs)
- Table columns: Label / Source (Shelly, SmartThings) / Type (Plug, Motion,
  Door, Vacuum, Camera) / Detail (current reading) / Status / Last Updated
- Double-clicking a row opens a per-device time-series plot
  (Today / 3 days / 7 days)

### 2. Log files

```bash
# Shelly worker (1 s collection)
tail -f <CSV_BASE_DIR>/logs/collector_YYYYMMDD.log

# SmartThings worker (60 s polling + history)
tail -f <LOG_FILE_from_config>

# Filter for errors
grep ERROR <CSV_BASE_DIR>/logs/collector_*.log
grep ERROR <LOG_FILE_from_config>
```

> On Windows PowerShell, `Get-Content <path> -Tail 20 -Wait` is the closest
> analogue to `tail -f`.

### 3. Process check

```bash
# Linux / macOS
ps aux | grep main.py

# Windows PowerShell
Get-Process python | Where-Object { $_.MainWindowTitle -match "main.py" }
```

---

## Analysis Examples

The following pandas snippets illustrate common access patterns over the
collected CSV files.

### 1. Load an entire day for a single plug

```python
import pandas as pd
from pathlib import Path

BASE = Path(r"D:\smartthings_data\csv_data")
days = ["20260509", "20260510", "20260511"]
label = "SMP05"

frames = []
for d in days:
    for p in BASE.joinpath(d).glob(f"{label}_*.csv"):
        df = pd.read_csv(p)
        df["Timestamp"] = pd.to_datetime(df["Timestamp"])
        frames.append(df)

smp05 = pd.concat(frames).sort_values("Timestamp").reset_index(drop=True)
hourly = smp05.set_index("Timestamp")["Power (W)"].resample("1H").mean()
print(hourly.tail())
```

### 2. Aggregate all plugs into a single DataFrame

```python
import pandas as pd
from pathlib import Path

BASE = Path(r"D:\smartthings_data\csv_data")
day = "20260511"
plug_files = sorted(BASE.joinpath(day).glob("SMP*_*.csv"))

per_plug = {}
for p in plug_files:
    label = p.stem.split("_")[0]      # SMP01 / SMP13 / ...
    df = pd.read_csv(p)
    df["Timestamp"] = pd.to_datetime(df["Timestamp"])
    per_plug[label] = df.set_index("Timestamp")["Power (W)"]

power_all = pd.concat(per_plug, axis=1).sort_index()   # columns: SMP01..SMP19
power_all["total"] = power_all.sum(axis=1)
print(power_all.resample("1min").mean().tail())
```

### 3. Count motion / contact events

```python
import pandas as pd
from pathlib import Path

BASE = Path(r"D:\smartthings_data\csv_data\20260511")

ms = pd.read_csv(next(BASE.glob("MS1_*.csv")))
ms["Timestamp"] = pd.to_datetime(ms["Timestamp"])
active_count = (ms["Motion"] == "active").sum()
print(f"MS1 active events today: {active_count}")
```

---

## Troubleshooting

### 1. Motion / door / vacuum / camera rows disappear from the dashboard

This is typically caused by inserting the same `iid` into the Treeview twice,
which raises an exception inside `_refresh`. Recent versions include per-label
de-duplication and a `_safe_insert` guard, so the issue should no longer be
observed. If it recurs, inspect the SmartThings worker log for
`[ERROR] Dashboard refresh error: ...`.

### 2. Shelly plug reports "Connection timeout"

```bash
# 1. Ping the device
ping 192.168.0.172

# 2. Query the HTTP endpoint directly
curl http://192.168.0.172/rpc/Switch.GetStatus?id=0

# 3. If unstable, verify Wi-Fi signal strength or add a repeater
```

### 3. SmartThings `refresh_token` expired

If the log contains `refresh_token has expired ...`:

```bash
python smartthings_auth.py
```

The `refresh_token` expires after 29 days without use.

### 4. Repeated SmartThings 401 / 403 responses

Verify that `CLIENT_ID` and `CLIENT_SECRET` in `config.local.json` are correct
and that the SmartThings application has the OAuth scopes `r:devices:*` and
`r:locations:*` enabled in the Developer Workspace.

### 5. Disk-space pressure

```powershell
# Compress old date-partitioned folders
Compress-Archive D:\smartthings_data\csv_data\20260301 D:\backup\20260301.zip

# Or remove them
Remove-Item -Recurse D:\smartthings_data\csv_data\20260301
```

Approximate storage cost per device per day:
Shelly plug (1 s) ≈ 0.5 MB; SmartThings plugs and sensors are considerably
smaller.

### 6. The collector hangs or stops producing data

```bash
grep ERROR <CSV_BASE_DIR>/logs/collector_*.log
grep ERROR <LOG_FILE_from_config>
```

If an auto-restart supervisor is required, use `run_collector.sh`,
`run_persistent.py`, or the systemd unit described above.

---

## Operational Notes

### Daily check (example)

```powershell
# 1. Open the dashboard and inspect SMP OK / Fail counters
python main.py

# 2. Check disk usage (Windows)
Get-PSDrive D | Select-Object Used,Free

# 3. Inspect the latest SmartThings log entries
Get-Content D:\smartthings_data\logs\smartthings.log -Tail 30
```

### Shutdown and archiving

```powershell
# 1. Stop the collector
#    - Close the dashboard window, or send Ctrl+C in the console

# 2. Archive collected data (PowerShell, whole-day folders)
Compress-Archive D:\smartthings_data\csv_data\* D:\backup\csv_data_$(Get-Date -Format yyyyMMdd).zip
```

---

## Citation

If you use this software or the associated dataset in a research context,
please cite the following work:

Kim, J., Lee, H., Lee, J., Roh, J., & Hwang, E. (2026).
*Ambient sensor time-series data for occupancy and activity recognition in a
laboratory workspace.*

BibTeX:

```bibtex
@misc{kim2026ambient,
  title        = {Ambient sensor time-series data for occupancy and activity
                  recognition in a laboratory workspace},
  author       = {Kim, Jeein and Lee, Haewon and Lee, JuYoung and
                  Roh, Janghyun and Hwang, Euiseok},
  year         = {2026},
  howpublished = {\url{https://github.com/lime9903/data-collection}},
  note         = {Version v1.0.0}
}
```

A machine-readable `CITATION.cff` is also provided in the repository root,
which GitHub renders as a "Cite this repository" widget.

### Authors and Affiliations

- **Jeein Kim**$^{1}$, **Haewon Lee**$^{2}$, **JuYoung Lee**$^{1}$,
  **Janghyun Roh**$^{1}$, **Euiseok Hwang**$^{1,2,*}$
- $^{1}$ Department of AI, Gwangju Institute of Science and
  Technology, Gwangju, 61005, South Korea
- $^{2}$ Department of Electrical Engineering and Computer Science,
  Gwangju Institute of Science and Technology, Gwangju, 61005, South Korea
- $^{*}$ Corresponding author: <euiseokh@gist.ac.kr>

---

## License

This project is released under the MIT License. See the [`LICENSE`](LICENSE)
file in the repository root for the full text.

---

## Acknowledgments

This work was supported by the National Research Foundation of Korea (NRF)
grant funded by the Korea government (MSIT) (**RS-2024-00349582**).
