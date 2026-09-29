#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unified desktop dashboard (tkinter)

- Shelly: live SMP state (shelly_collector.dashboard_state)
- SmartThings: MS#/DS#/Vacuum/Camera files under CSV_BASE_DIR, refreshed periodically
- Double-click: a graph per device type
"""

import os
import re
import csv
import math
from datetime import datetime
from pathlib import Path

import numpy as np
import tkinter as tk
from tkinter import ttk
import logging

import matplotlib

# Live SMP state always comes from this module's dashboard_state, which is the
# same object main.py hands over as the collector
def _shelly_dashboard_state():
    try:
        import shelly_collector as sc

        return getattr(sc, "dashboard_state", None) or {}
    except Exception:
        return {}


def _smartthings_dashboard_state():
    """The SmartThings worker's dashboard_state, used to total the plug (stplug) rows."""
    try:
        import smartthings_worker as st

        return getattr(st, "dashboard_state", None) or {}
    except Exception:
        return {}


def _smartthings_device_metadata():
    """Fallback that gives the registered stplug count even before the first cycle."""
    try:
        import smartthings_worker as st

        return getattr(st, "device_metadata", None) or []
    except Exception:
        return []

matplotlib.use("TkAgg")
import matplotlib.dates as mdates
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from matplotlib import rcParams

rcParams["font.family"] = "Segoe UI"
rcParams["axes.unicode_minus"] = False


BG = "#0f1117"
CARD_BG = "#1a1d27"
BORDER = "#2d3148"
FG = "#e2e8f0"
MUTED = "#64748b"
GREEN = "#22c55e"
RED = "#ef4444"
BLUE = "#60a5fa"
YELLOW = "#fbbf24"
PURPLE = "#a78bfa"
ORANGE = "#fb923c"
CYAN = "#38bdf8"

# SmartThings CSV refresh period (ms)
ST_CSV_REFRESH_MS = 5000

# Plug graph: Shelly at 1 Hz over several days is hundreds of thousands of points,
# which can make Tk/matplotlib look frozen, so cap the rendering only.
_PLUG_GRAPH_MAX_POINTS = 22000

# Time bucket (s) for plug plots only. Shelly at 1 Hz needs bucketing; SmartThings
# polls rarely, so the same policy simply yields wider spacing.
def _plug_plot_bucket_sec(days: int) -> float:
    if days <= 1:
        return 10.0
    if days <= 3:
        return 30.0
    return 60.0


def _safe_float(x, default=0.0):
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def _parse_csv_float_or_nan(val):
    """CSV cell to float. An empty string or a parse failure becomes nan, which
    breaks the line in the plot."""
    if val is None:
        return math.nan
    s = str(val).strip()
    if not s:
        return math.nan
    try:
        return float(s)
    except (TypeError, ValueError):
        return math.nan


def _aggregate_plug_plot_series(timestamps, powers, energies, bucket_sec=1.0):
    """Time bucket for plotting: power is the max finite value in the bucket and
    energy the last finite one. The CSV itself is neither interpolated nor despiked."""
    if not timestamps:
        return [], [], []
    rows = sorted(zip(timestamps, powers, energies), key=lambda x: x[0])
    t0 = rows[0][0]
    out_ts, out_pw, out_en = [], [], []
    cur_key = None
    pw_vals = []
    last_e = math.nan
    last_t = None

    def flush():
        if cur_key is None:
            return
        out_ts.append(last_t)
        out_pw.append(max(pw_vals) if pw_vals else math.nan)
        out_en.append(last_e if math.isfinite(last_e) else math.nan)

    for t, p, e in rows:
        key = int((t - t0).total_seconds() // bucket_sec)
        if cur_key is None:
            cur_key = key
        elif key != cur_key:
            flush()
            cur_key = key
            pw_vals = []
            last_e = math.nan
        last_t = t
        if math.isfinite(p):
            pw_vals.append(float(p))
        if math.isfinite(e):
            last_e = float(e)
    flush()
    return out_ts, out_pw, out_en


def _cap_plug_plot_points(timestamps, powers, energies, max_points=_PLUG_GRAPH_MAX_POINTS):
    """Cap the point count for rendering only; this is for UI responsiveness and
    does not preserve the meaning of the original CSV or of any statistic."""
    if max_points <= 0 or len(timestamps) <= max_points:
        return timestamps, powers, energies
    step = int(math.ceil(len(timestamps) / max_points))
    return timestamps[::step], powers[::step], energies[::step]


def _parse_ts(ts_raw):
    if not ts_raw:
        return None
    ts_raw = str(ts_raw).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(ts_raw, fmt)
        except ValueError:
            continue
    return None


def _csv_base_dir(collector_module):
    """Must match the path SmartThings actually writes to, or the MS/DS CSVs
    will not all appear."""
    try:
        import smartthings_worker as st

        return Path(st.CSV_BASE_DIR)
    except Exception:
        pass
    p = getattr(collector_module, "CSV_BASE_DIR", None)
    if p is None:
        return Path(r"D:/smartthings_data/csv_data")
    return Path(p)


def _parse_st_csv_stem(stem: str):
    """Parse a {Label}_{device_id}_{YYYYMMDD} or Camera_{YYYYMMDD} stem."""
    if stem.startswith("Camera_"):
        rest = stem[len("Camera_") :]
        if rest.isdigit() and len(rest) == 8:
            return "Camera", "", rest
    parts = stem.rsplit("_", 2)
    if len(parts) == 3 and parts[2].isdigit() and len(parts[2]) == 8:
        return parts[0], parts[1], parts[2]
    return stem, "", ""


def _st_tree_iid(kind: str, fpath: str, display_label: str) -> str:
    """A Treeview iid must be unique per item; duplicate labels are fine."""
    stem = Path(fpath).stem
    lbl, dev_id, _ = _parse_st_csv_stem(stem)
    label_for_graph = display_label or lbl
    if kind == "camera":
        return "camera::all"
    suffix = dev_id or f"{abs(hash(os.path.normpath(fpath))) & 0xFFFFFFFFFFFF:x}"
    return f"{kind}::{label_for_graph}::{suffix}"


def _label_from_motion_door_vacuum_iid(iid_str: str, kind: str) -> str:
    """motion::/door::/vacuum:: iid to a graph label (the first field)."""
    s = str(iid_str)
    prefix = f"{kind}::"
    if not s.startswith(prefix):
        return ""
    rest = s[len(prefix) :]
    return rest.split("::", 1)[0] if rest else ""


def _csv_paths_under(base: Path) -> list[Path]:
    """rglob is more reliable than glob.glob('**') on Windows."""
    try:
        base = base.expanduser().resolve()
    except OSError:
        return []
    if not base.is_dir():
        return []
    try:
        return list(base.rglob("*.csv"))
    except OSError:
        return []


def _dedupe_latest_per_stem_device(paths: list[Path]) -> list[Path]:
    """For the same Label_deviceId_YYYYMMDD pattern across dates, keep only the
    most recent by mtime."""
    groups: dict[tuple[str, str], tuple[float, Path]] = {}
    for p in paths:
        stem = p.stem
        lbl, dev_id, _ = _parse_st_csv_stem(stem)
        key = (lbl or stem, dev_id) if dev_id else (str(p), "")
        try:
            mt = p.stat().st_mtime
        except OSError:
            continue
        prev = groups.get(key)
        if prev is None or mt > prev[0]:
            groups[key] = (mt, p)
    return sorted((t[1] for t in groups.values()), key=lambda x: str(x).lower())


def _tree_label_from_st_path(fpath: str) -> str:
    """Label column of the table: prefer the file name over the Label inside the
    CSV, which can be duplicated."""
    stem = Path(fpath).stem
    lbl, dev_id, _ = _parse_st_csv_stem(stem)
    if lbl and dev_id:
        return lbl
    if "_" in stem:
        return stem.split("_", 1)[0]
    return stem


class Dashboard(tk.Tk):
    def __init__(self, collector_module):
        super().__init__()
        self.collector = collector_module
        self._csv_base = _csv_base_dir(collector_module)
        # _read_last_csv_row: (mtime, row_dict). Avoids loading a large CSV in full
        # and re-reading the same file repeatedly.
        self._last_row_cache: dict[str, tuple[float, dict | None]] = {}
        self._last_row_cache_max = 4096
        self.title("Integrated Data Collector (Shelly + SmartThings)")
        self.geometry("1280x720")
        self.configure(bg=BG)
        self.resizable(True, True)

        self._build_ui()

        self.collector.on_data_updated = self._on_collected
        self._refresh()

        self._st_refresh_job = None
        self._schedule_st_refresh()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _schedule_st_refresh(self):
        if self._st_refresh_job:
            try:
                self.after_cancel(self._st_refresh_job)
            except Exception:
                pass
        self._st_refresh_job = self.after(ST_CSV_REFRESH_MS, self._st_timer_tick)

    def _st_timer_tick(self):
        try:
            self._refresh()
        except Exception as e:
            logging.error(f"ST CSV refresh error: {e}")
        self._schedule_st_refresh()

    def _build_ui(self):
        header = tk.Frame(self, bg=CARD_BG, pady=12)
        header.pack(fill="x")

        dot = tk.Label(header, text="●", fg=YELLOW, bg=CARD_BG, font=("Segoe UI", 12))
        dot.pack(side="left", padx=(16, 6))
        self.dot = dot

        tk.Label(
            header,
            text="Integrated: Shelly (SMP) + SmartThings (MS/DS/Vacuum/Camera)",
            fg=FG,
            bg=CARD_BG,
            font=("Segoe UI", 13, "bold"),
        ).pack(side="left")

        self.lbl_status = tk.Label(
            header,
            text="Initializing...",
            fg=MUTED,
            bg=CARD_BG,
            font=("Segoe UI", 10),
        )
        self.lbl_status.pack(side="right", padx=16)

        card_frame = tk.Frame(self, bg=BG, pady=16)
        card_frame.pack(fill="x", padx=16)

        self.lbl_total = self._make_card(card_frame, "SMP (live)", "-", BLUE)
        self.lbl_success = self._make_card(card_frame, "SMP OK", "-", GREEN)
        self.lbl_fail = self._make_card(card_frame, "SMP Fail", "-", RED)

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x", padx=16, pady=(10, 0))

        tk.Label(
            self,
            text="Device status (SMP live + SmartThings from CSV)",
            fg=MUTED,
            bg=BG,
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w", padx=16, pady=(12, 4))

        table_frame = tk.Frame(self, bg=BG)
        table_frame.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            "Custom.Treeview",
            background=CARD_BG,
            foreground=FG,
            fieldbackground=CARD_BG,
            rowheight=28,
            font=("Segoe UI", 10),
            borderwidth=0,
        )
        style.configure(
            "Custom.Treeview.Heading",
            background="#13151f",
            foreground=MUTED,
            font=("Segoe UI", 9, "bold"),
            relief="flat",
        )

        cols = ("label", "source", "type", "detail", "status", "updated")
        self.tree = ttk.Treeview(
            table_frame,
            columns=cols,
            show="headings",
            style="Custom.Treeview",
            selectmode="browse",
        )

        headers = {
            "label": ("Label", 140),
            "source": ("Source", 90),
            "type": ("Type", 90),
            "detail": ("Detail", 320),
            "status": ("Status", 90),
            "updated": ("Last Updated", 180),
        }
        for col, (head, width) in headers.items():
            self.tree.heading(col, text=head)
            self.tree.column(col, width=width, anchor="w" if col in ("label", "detail") else "center")

        self.tree.tag_configure("plug_ok", foreground=GREEN)
        self.tree.tag_configure("plug_fail", foreground=RED)
        self.tree.tag_configure("st_motion", foreground=ORANGE)
        self.tree.tag_configure("st_door", foreground=CYAN)
        self.tree.tag_configure("st_vacuum", foreground=PURPLE)
        self.tree.tag_configure("st_camera", foreground=YELLOW)

        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)

        self.tree.bind("<Double-1>", self._on_device_double_click)

        statusbar = tk.Frame(self, bg="#13151f", pady=5)
        statusbar.pack(fill="x", side="bottom")
        tk.Label(
            statusbar,
            text="Double-click: graph (SMP power/energy | MS motion/temp | DS contact/temp | Vacuum power | Camera motion/sound)",
            fg=MUTED,
            bg="#13151f",
            font=("Segoe UI", 9),
        ).pack(side="left", padx=12)

        self.lbl_refreshed = tk.Label(
            statusbar,
            text="-",
            fg=MUTED,
            bg="#13151f",
            font=("Segoe UI", 9),
        )
        self.lbl_refreshed.pack(side="right", padx=12)

    def _make_card(self, parent, label_text, value_text, color):
        frame = tk.Frame(
            parent,
            bg=CARD_BG,
            bd=1,
            relief="flat",
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        frame.pack(side="left", expand=True, fill="both", padx=6)
        tk.Label(frame, text=label_text, fg=MUTED, bg=CARD_BG, font=("Segoe UI", 9), pady=8).pack()
        lbl = tk.Label(frame, text=value_text, fg=color, bg=CARD_BG, font=("Segoe UI", 24, "bold"), pady=4)
        lbl.pack(pady=(0, 10))
        return lbl

    def _on_close(self):
        if self._st_refresh_job:
            try:
                self.after_cancel(self._st_refresh_job)
            except Exception:
                pass
        stop_fn = getattr(self.collector, "_stop_callback", None)
        try:
            if callable(stop_fn):
                stop_fn()
        except Exception:
            pass
        self.destroy()

    def _on_collected(self):
        self.after(0, self._refresh)

    def _refresh(self):
        try:
            # The SMP card and table always come from shelly_collector.dashboard_state,
            # which the collection thread updates
            state = _shelly_dashboard_state()
            st_state = _smartthings_dashboard_state()

            status = state.get("status", "Initializing")
            self.lbl_status.config(text=status)
            self.dot.config(fg=GREEN if status == "Collecting" else YELLOW)

            # ---- SMP card total: Shelly (1 s) + SmartThings stplug (60 s polling) ----
            sh_total = int(state.get("total", 0) or 0)
            sh_ok    = int(state.get("success", 0) or 0)
            sh_fail  = int(state.get("fail", 0) or 0)

            st_plug_rows = [
                d for d in (st_state.get("devices", []) or [])
                if d.get("type") == "stplug"
            ]
            st_plug_total = len(st_plug_rows)
            st_plug_ok    = sum(1 for d in st_plug_rows if d.get("status") == "OK")
            st_plug_fail  = sum(1 for d in st_plug_rows if d.get("status") == "Fail")

            # If the SmartThings worker has not run its first cycle yet and devices
            # is empty, fall back to the stplug count registered in device_metadata.
            if st_plug_total == 0:
                st_plug_total = sum(
                    1 for d in _smartthings_device_metadata() if d.get("type") == "stplug"
                )

            self.lbl_total.config(text=str(sh_total + st_plug_total))
            self.lbl_success.config(text=str(sh_ok + st_plug_ok))
            self.lbl_fail.config(text=str(sh_fail + st_plug_fail))

            self.tree.delete(*self.tree.get_children())

            # The same iid appearing twice in one cycle raises tk.TclError, which the
            # outer try/except swallows, so the motion/door rows after it disappear.
            # Wrap each row in a helper so one failure does not affect the rest.
            def _safe_insert(iid, values, tags):
                try:
                    self.tree.insert("", "end", iid=iid, values=values, tags=tags)
                except tk.TclError as ins_err:
                    logging.warning(f"Tree insert skipped (iid={iid}): {ins_err}")

            # --- Shelly plugs (live) ---
            devices = state.get("devices", []) or []
            for d in sorted(devices, key=lambda x: x.get("label", "")):
                power = d.get("power")
                energy = d.get("energy")
                status_text = d.get("status", "-")
                updated = d.get("updated", "-")
                power_str = "-" if power is None else f"{_safe_float(power):.1f} W"
                energy_str = "-" if energy is None else f"{_safe_float(energy):.1f} Wh"
                detail = f"{power_str}  |  {energy_str}"
                tag = "plug_ok" if status_text == "OK" else "plug_fail"
                _safe_insert(
                    iid=f"plug::{d.get('label', '')}",
                    values=(
                        d.get("label", ""),
                        "Shelly",
                        "Plug",
                        detail,
                        status_text,
                        updated,
                    ),
                    tags=(tag, "plug"),
                )

            # --- SmartThings: summarise from the CSVs ---
            for row in self._discover_st_rows():
                iid = row.get("iid") or f"{row['kind']}::{row['label']}"
                tag = row.get("tag", "st_motion")
                _safe_insert(
                    iid=iid,
                    values=(
                        row["label"],
                        "SmartThings",
                        row["type"],
                        row["detail"],
                        row["status"],
                        row["updated"],
                    ),
                    tags=(tag, row["kind"]),
                )

            self.lbl_refreshed.config(
                text=(
                    f"Last refresh: {datetime.now().strftime('%H:%M:%S')}  |  "
                    f"CSV root: {self._csv_base}"
                )
            )
        except Exception as e:
            logging.error(f"Dashboard refresh error: {e}")

    def _discover_st_rows(self):
        """Build the SMP (SmartThings) / MS / DS / Vacuum / Camera summary rows from
        CSV_BASE_DIR.

        SmartThings plugs: only labels whose file name is SMP* and which are NOT
        registered in shelly_collector.DEVICES, to avoid duplicates.
        """
        rows = []
        base = Path(self._csv_base)
        if not base.is_dir():
            return rows

        all_csv = _csv_paths_under(base)

        # SMP labels Shelly handles directly (upper case). Their plug:: rows are
        # already inserted by _refresh() from shelly_collector.dashboard_state, so
        # they are excluded here.
        try:
            import shelly_collector as _sc

            shelly_plug_labels = {
                str(d.get("label", "")).strip().upper() for d in getattr(_sc, "DEVICES", [])
            }
        except Exception:
            shelly_plug_labels = set()

        # Smart plugs (SmartThings): only SMP* labels Shelly does not own
        plug_paths = [
            p
            for p in all_csv
            if p.name.upper().startswith("SMP")
            and _tree_label_from_st_path(str(p)).upper() not in shelly_plug_labels
        ]
        plug_paths = _dedupe_latest_per_stem_device(plug_paths)

        # Extra dedupe: the same label can have an older CSV under a different
        # device_id (e.g. SMP16=ABC in March, re-registered today as SMP16=XYZ),
        # so keep only the most recent by mtime.
        # Otherwise the plug::SMP16 iid is inserted twice, the Tree raises, _refresh
        # aborts, and every motion/vacuum/camera row after that point goes missing.
        latest_per_label: dict[str, tuple[float, "Path"]] = {}
        for p in plug_paths:
            label_key = _tree_label_from_st_path(str(p)).upper()
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            prev = latest_per_label.get(label_key)
            if prev is None or mt > prev[0]:
                latest_per_label[label_key] = (mt, p)
        plug_paths = sorted((t[1] for t in latest_per_label.values()), key=lambda x: str(x).lower())

        for p in plug_paths:
            fpath = str(p)
            label, detail, updated, ok = self._last_row_plug(fpath)
            if not label:
                continue
            rows.append({
                "kind": "plug",
                "label": label,
                "iid": f"plug::{label}",
                "type": "Plug",
                "detail": detail,
                "status": "OK" if ok else "-",
                "updated": updated,
                "tag": "plug_ok" if ok else "plug_fail",
                "source": "SmartThings",
            })

        # Motion: file name starts with MS (SMP belongs to Shelly and never does)
        motion_paths = [p for p in all_csv if p.name.upper().startswith("MS")]
        motion_paths = _dedupe_latest_per_stem_device(motion_paths)
        for p in motion_paths:
            fpath = str(p)
            label, detail, updated, ok = self._last_row_motion(fpath)
            if not label:
                continue
            rows.append({
                "kind": "motion",
                "label": label,
                "iid": _st_tree_iid("motion", fpath, label),
                "type": "Motion",
                "detail": detail,
                "status": "OK" if ok else "-",
                "updated": updated,
                "tag": "st_motion",
            })

        # Door: DS*
        door_paths = [p for p in all_csv if p.name.upper().startswith("DS")]
        door_paths = _dedupe_latest_per_stem_device(door_paths)
        for p in door_paths:
            fpath = str(p)
            label, detail, updated, ok = self._last_row_door(fpath)
            if not label:
                continue
            rows.append({
                "kind": "door",
                "label": label,
                "iid": _st_tree_iid("door", fpath, label),
                "type": "Door",
                "detail": detail,
                "status": "OK" if ok else "-",
                "updated": updated,
                "tag": "st_door",
            })

        # Vacuum: file name contains vacuum (excluding Camera and the old
        # dustbin_events helper CSVs)
        vac_paths = [
            p
            for p in all_csv
            if "vacuum" in p.name.casefold()
            and not p.name.lower().startswith("camera_")
            and "_dustbin_events" not in p.name.lower()
        ]
        vac_paths = _dedupe_latest_per_stem_device(vac_paths)

        # Per-label dedupe, the same guard as for plugs: ignore old device_id CSVs
        latest_per_label_vac: dict[str, tuple[float, "Path"]] = {}
        for p in vac_paths:
            label_key = _tree_label_from_st_path(str(p)).upper()
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            prev = latest_per_label_vac.get(label_key)
            if prev is None or mt > prev[0]:
                latest_per_label_vac[label_key] = (mt, p)
        vac_paths = sorted(
            (t[1] for t in latest_per_label_vac.values()), key=lambda x: str(x).lower()
        )
        for p in vac_paths:
            fpath = str(p)
            label, detail, updated, ok = self._last_row_vacuum(fpath)
            if not label:
                continue
            rows.append({
                "kind": "vacuum",
                "label": label,
                "iid": _st_tree_iid("vacuum", fpath, label),
                "type": "Vacuum",
                "detail": detail,
                "status": "OK" if ok else "-",
                "updated": updated,
                "tag": "st_vacuum",
            })

        # Camera: Camera_YYYYMMDD.csv, with several camera rows per file
        cam_files = sorted([p for p in all_csv if p.name.upper().startswith("CAMERA_")])
        if cam_files:
            fpath = str(cam_files[-1])
            summary = self._camera_file_summary(fpath)
            rows.append({
                "kind": "camera",
                "label": "Camera",
                "iid": _st_tree_iid("camera", fpath, "Camera"),
                "type": "Camera",
                "detail": summary["detail"],
                "status": "OK" if summary["ok"] else "-",
                "updated": summary["updated"],
                "tag": "st_camera",
            })

        def sort_key(r):
            order = {"plug": 0, "motion": 1, "door": 2, "vacuum": 3, "camera": 4}
            return (order.get(r["kind"], 9), r["label"])

        rows.sort(key=sort_key)
        return rows

    def _read_last_csv_row(self, fpath):
        """Use only the last data row. Materialising the whole file with
        list(DictReader) exhausts memory on high-rate, large CSVs."""
        try:
            mtime = os.path.getmtime(fpath)
        except OSError:
            return None

        hit = self._last_row_cache.get(fpath)
        if hit is not None and hit[0] == mtime:
            return hit[1]

        row_dict: dict | None = None
        try:
            with open(fpath, newline="", encoding="utf-8-sig", errors="replace") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if not header:
                    row_dict = None
                else:
                    keys = [str(k).strip().lstrip("\ufeff") for k in header]
                    last_raw: list[str] | None = None
                    for row in reader:
                        if row and any((c or "").strip() for c in row):
                            last_raw = row
                    if last_raw is None:
                        row_dict = None
                    else:
                        n = len(keys)
                        padded = (list(last_raw) + [""] * n)[:n]
                        row_dict = dict(zip(keys, padded))
        except Exception:
            row_dict = None

        if len(self._last_row_cache) >= self._last_row_cache_max:
            self._last_row_cache.clear()
        self._last_row_cache[fpath] = (mtime, row_dict)
        return row_dict

    def _label_from_filename(self, fpath):
        stem = Path(fpath).stem
        m = re.match(r"^(.+)_[0-9a-fA-F-]{8,}_[0-9]{8}$", stem)
        if m:
            return m.group(1)
        parts = stem.rsplit("_", 2)
        if len(parts) >= 3 and parts[-1].isdigit() and len(parts[-1]) == 8:
            return "_".join(parts[:-2]) if len(parts) > 2 else parts[0]
        return stem.split("_")[0]

    def _last_row_motion(self, fpath):
        label = _tree_label_from_st_path(fpath)
        row = self._read_last_csv_row(fpath)
        if not row:
            return label, "(no data rows)", "-", False
        motion = (row.get("Motion") or "-").strip()
        temp = row.get("Temperature (°C)", row.get("Temperature", ""))
        try:
            temp_s = f"{float(temp):.1f}°C" if str(temp).strip() else "-"
        except (TypeError, ValueError):
            temp_s = str(temp) if temp else "-"
        detail = f"motion={motion}  |  temp={temp_s}"
        ts = _parse_ts(row.get("Timestamp", ""))
        updated = ts.strftime("%Y-%m-%d %H:%M:%S") if ts else "-"
        return label, detail, updated, True

    def _last_row_door(self, fpath):
        label = _tree_label_from_st_path(fpath)
        row = self._read_last_csv_row(fpath)
        if not row:
            return label, "(no data rows)", "-", False
        contact = (row.get("Contact") or row.get("contact") or "-").strip()
        temp = row.get("Temperature (°C)", row.get("Temperature", ""))
        try:
            temp_s = f"{float(temp):.1f}°C" if str(temp).strip() else "-"
        except (TypeError, ValueError):
            temp_s = str(temp) if temp else "-"
        batt = row.get("Battery (%)", row.get("Battery", ""))
        batt_s = f"{batt}%" if str(batt).strip() else "-"
        detail = f"contact={contact}  |  temp={temp_s}  |  bat={batt_s}"
        ts = _parse_ts(row.get("Timestamp", ""))
        updated = ts.strftime("%Y-%m-%d %H:%M:%S") if ts else "-"
        return label, detail, updated, True

    def _last_row_plug(self, fpath):
        """Last row of a SmartThings plug CSV, as the dashboard detail string.

        Header: Timestamp, Label, Location, Room, Switch, Power (W), Energy (Wh)
        Shows 'power / energy' so the card matches the Shelly plug cards, with the
        ON/OFF state prefixed.
        """
        label = _tree_label_from_st_path(fpath)
        row = self._read_last_csv_row(fpath)
        if not row:
            return label, "(no data rows)", "-", False

        sw = (row.get("Switch") or row.get("switch") or "").strip()
        p = row.get("Power (W)", row.get("Power", ""))
        e = row.get("Energy (Wh)", row.get("Energy", ""))
        try:
            pw = f"{float(p):.1f} W" if str(p).strip() else "-"
        except (TypeError, ValueError):
            pw = str(p) if p else "-"
        try:
            ew = f"{float(e):.1f} Wh" if str(e).strip() else "-"
        except (TypeError, ValueError):
            ew = str(e) if e else "-"

        detail = f"{pw}  |  {ew}"
        if sw:
            detail = f"switch={sw}  |  {detail}"

        ts = _parse_ts(row.get("Timestamp", ""))
        updated = ts.strftime("%Y-%m-%d %H:%M:%S") if ts else "-"
        return label, detail, updated, True

    def _last_row_vacuum(self, fpath):
        label = _tree_label_from_st_path(fpath)
        row = self._read_last_csv_row(fpath)
        if not row:
            return label, "(no data rows)", "-", False
        p = row.get("Power (W)", row.get("Power", ""))
        e = row.get("Energy (Wh)", row.get("Energy", ""))
        try:
            pw = f"{float(p):.1f} W" if str(p).strip() else "-"
        except (TypeError, ValueError):
            pw = str(p) if p else "-"
        try:
            ew = f"{float(e):.1f} Wh" if str(e).strip() else "-"
        except (TypeError, ValueError):
            ew = str(e) if e else "-"
        cs = (row.get("Cleaner State") or "").strip()
        ds = (row.get("Dustbin State") or "").strip()
        de = (row.get("Dustbin Last Emptied (UTC)") or "").strip()
        detail = f"power={pw}  |  energy={ew}"
        if cs or ds or de:
            detail += (
                f"  |  cleaner={cs or '-'}  |  dustbin={ds or '-'}  |  last emptied={de or '-'}"
            )
        ts = _parse_ts(row.get("Timestamp", ""))
        updated = ts.strftime("%Y-%m-%d %H:%M:%S") if ts else "-"
        return label, detail, updated, True

    def _camera_file_summary(self, fpath):
        row = self._read_last_csv_row(fpath)
        if not row:
            return {"detail": "no rows", "updated": "-", "ok": False}
        ts = _parse_ts(row.get("Timestamp", ""))
        updated = ts.strftime("%Y-%m-%d %H:%M:%S") if ts else "-"
        cam = (row.get("Camera") or "").strip() or "?"
        mot = (row.get("Motion") or "").strip()
        snd = (row.get("Sound") or "").strip()
        detail = f"last: {cam}  |  motion={mot or '-'}  |  sound={snd or '-'}"
        return {"detail": detail, "updated": updated, "ok": True}

    # ---- CSV loading (for the graphs) ----

    def _find_csv_files(self, label):
        """rglob instead of glob **, which misses files on Windows."""
        prefix = f"{label}_"
        return sorted(
            str(p) for p in _csv_paths_under(Path(self._csv_base)) if p.name.startswith(prefix)
        )

    def _load_plug_csv(self, label, days=1):
        files = self._find_csv_files(label)[-days:]
        timestamps, powers, energies = [], [], []
        for fpath in files:
            try:
                with open(fpath, newline="", encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        try:
                            ts_raw = row.get("Timestamp", "")
                            if not ts_raw:
                                continue
                            ts = _parse_ts(ts_raw)
                            if not ts:
                                continue
                            p = _parse_csv_float_or_nan(row.get("Power (W)", ""))
                            e = _parse_csv_float_or_nan(row.get("Energy (Wh)", ""))
                            timestamps.append(ts)
                            powers.append(p)
                            energies.append(e)
                        except Exception:
                            continue
            except Exception:
                continue
        combined = sorted(zip(timestamps, powers, energies), key=lambda x: x[0])
        if combined:
            timestamps, powers, energies = zip(*combined)
            timestamps = list(timestamps)
            powers = list(powers)
            energies = list(energies)
            # Plot only: the same time bucket (10/30/60 s by span) plus a point cap.
            # The CSV itself is untouched.
            bsec = _plug_plot_bucket_sec(days)
            timestamps, powers, energies = _aggregate_plug_plot_series(
                timestamps, powers, energies, bucket_sec=bsec
            )
            return _cap_plug_plot_points(timestamps, powers, energies)
        return [], [], []

    def _load_motion_csv(self, label, days=1):
        files = self._find_csv_files(label)[-days:]
        timestamps, motions, temps = [], [], []
        for fpath in files:
            try:
                with open(fpath, newline="", encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        try:
                            ts_raw = row.get("Timestamp", "")
                            ts = _parse_ts(ts_raw)
                            if not ts:
                                continue
                            timestamps.append(ts)
                            motions.append(1 if str(row.get("Motion", "")).lower() == "active" else 0)
                            temp_val = row.get("Temperature (°C)", row.get("Temperature", ""))
                            temp_val = str(temp_val).strip() if temp_val is not None else ""
                            try:
                                temps.append(float(temp_val) if temp_val else None)
                            except ValueError:
                                temps.append(None)
                        except Exception:
                            continue
            except Exception:
                continue
        combined = sorted(zip(timestamps, motions, temps), key=lambda x: x[0])
        if combined:
            timestamps, motions, temps = zip(*combined)
            return list(timestamps), list(motions), list(temps)
        return [], [], []

    def _load_door_csv(self, label, days=1):
        files = self._find_csv_files(label)[-days:]
        timestamps, contacts, temps = [], [], []
        for fpath in files:
            try:
                with open(fpath, newline="", encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        try:
                            ts_raw = row.get("Timestamp", "")
                            ts = _parse_ts(ts_raw)
                            if not ts:
                                continue
                            contact_val = str(
                                row.get("Contact")
                                or row.get("contact")
                                or row.get("Motion")
                                or row.get("motion")
                                or ""
                            ).lower()
                            temp_val = str(
                                row.get("Temperature (°C)")
                                or row.get("Temperature")
                                or row.get("temperature")
                                or ""
                            ).strip()
                            timestamps.append(ts)
                            contacts.append(1 if contact_val == "open" else 0)
                            try:
                                temps.append(float(temp_val) if temp_val else None)
                            except ValueError:
                                temps.append(None)
                        except Exception:
                            continue
            except Exception:
                continue
        combined = sorted(zip(timestamps, contacts, temps), key=lambda x: x[0])
        if combined:
            timestamps, contacts, temps = zip(*combined)
            return list(timestamps), list(contacts), list(temps)
        return [], [], []

    def _load_vacuum_csv(self, label, days=1):
        files = self._find_csv_files(label)[-days:]
        timestamps, powers, energies = [], [], []
        for fpath in files:
            try:
                with open(fpath, newline="", encoding="utf-8-sig", errors="replace") as f:
                    for row in csv.DictReader(f):
                        try:
                            row = {str(k).strip().lstrip("\ufeff"): v for k, v in row.items()}
                            ts_raw = row.get("Timestamp", "")
                            ts = _parse_ts(ts_raw)
                            if not ts:
                                continue
                            p = row.get("Power (W)") or row.get("Power") or row.get("power") or ""
                            e = row.get("Energy (Wh)") or row.get("Energy") or ""
                            if str(p).strip() == "" and str(e).strip() == "":
                                continue
                            pv = float(p) if str(p).strip() != "" else None
                            ev = float(e) if str(e).strip() != "" else None
                            timestamps.append(ts)
                            powers.append(pv if pv is not None else 0.0)
                            energies.append(ev if ev is not None else 0.0)
                        except Exception:
                            continue
            except Exception:
                continue
        combined = sorted(zip(timestamps, powers, energies), key=lambda x: x[0])
        if combined:
            timestamps, powers, energies = zip(*combined)
            return list(timestamps), list(powers), list(energies)
        return [], [], []

    def _load_camera_csv(self, days=1):
        files = sorted(
            str(p) for p in _csv_paths_under(Path(self._csv_base)) if p.name.upper().startswith("CAMERA_")
        )[-days:]
        timestamps, motions, sounds = [], [], []
        for fpath in files:
            try:
                with open(fpath, newline="", encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        try:
                            ts_raw = row.get("Timestamp", "")
                            ts = _parse_ts(ts_raw)
                            if not ts:
                                continue
                            motion_val = str(row.get("Motion", "")).lower()
                            sound_val = str(row.get("Sound", "")).lower()
                            if not motion_val and not sound_val:
                                continue
                            timestamps.append(ts)
                            motions.append(1 if motion_val == "active" else 0)
                            sounds.append(1 if sound_val in ("detected", "active") else 0)
                        except Exception:
                            continue
            except Exception:
                continue
        combined = sorted(zip(timestamps, motions, sounds), key=lambda x: x[0])
        if combined:
            timestamps, motions, sounds = zip(*combined)
            return list(timestamps), list(motions), list(sounds)
        return [], [], []

    def _on_device_double_click(self, event):
        selected = self.tree.selection()
        if not selected:
            return
        iid = selected[0]
        if str(iid).startswith("plug::"):
            label = str(iid).split("::", 1)[1]
            self._open_graph_window(label, "Plug")
            return
        if str(iid).startswith("motion::"):
            label = _label_from_motion_door_vacuum_iid(iid, "motion")
            self._open_graph_window(label, "Motion")
            return
        if str(iid).startswith("door::"):
            label = _label_from_motion_door_vacuum_iid(iid, "door")
            self._open_graph_window(label, "Door")
            return
        if str(iid).startswith("vacuum::"):
            label = _label_from_motion_door_vacuum_iid(iid, "vacuum")
            self._open_graph_window(label, "Vacuum")
            return
        if str(iid).startswith("camera::"):
            self._open_graph_window("Camera", "Camera")
            return

    def _open_graph_window(self, label, dev_type):
        win = tk.Toplevel(self)
        if dev_type == "Plug":
            subtitle = "Power / Energy"
        elif dev_type == "Vacuum":
            subtitle = "Power / Energy"
        elif dev_type == "Door":
            subtitle = "Door / Temp"
        elif dev_type == "Camera":
            subtitle = "Motion / Sound"
        else:
            subtitle = "Motion / Temp"
        win.title(f"{label} — {subtitle}")
        win.geometry("900x600")
        win.configure(bg=BG)

        top = tk.Frame(win, bg=CARD_BG, pady=10)
        top.pack(fill="x")
        tk.Label(top, text=f"  {label}", fg=FG, bg=CARD_BG, font=("Segoe UI", 12, "bold")).pack(side="left", padx=8)

        days_var = tk.IntVar(value=1)
        btn_frame = tk.Frame(top, bg=CARD_BG)
        btn_frame.pack(side="right", padx=12)

        def make_btn(text, d):
            tk.Button(
                btn_frame,
                text=text,
                bg="#2d3148",
                fg=FG,
                activebackground="#3d4168",
                activeforeground=FG,
                relief="flat",
                padx=10,
                pady=4,
                font=("Segoe UI", 9),
                cursor="hand2",
                command=lambda: [days_var.set(d), draw()],
            ).pack(side="left", padx=3)

        make_btn("Today", 1)
        make_btn("3 Days", 3)
        make_btn("7 Days", 7)

        fig = Figure(figsize=(9, 5), facecolor=BG)
        canvas = FigureCanvasTkAgg(fig, master=win)
        canvas.get_tk_widget().pack(fill="both", expand=True, padx=8, pady=8)

        no_data_lbl = tk.Label(win, text="", fg=RED, bg=BG, font=("Segoe UI", 11))
        no_data_lbl.pack()

        def style_ax(ax):
            ax.set_facecolor(CARD_BG)
            ax.tick_params(colors=MUTED, labelsize=8)
            ax.xaxis.label.set_color(MUTED)
            ax.yaxis.label.set_color(MUTED)
            for spine in ax.spines.values():
                spine.set_edgecolor(BORDER)
            ax.grid(True, color=BORDER, linewidth=0.5, linestyle="--")

        def fmt_xaxis(ax, days):
            fmt = "%H:%M" if days == 1 else "%m/%d %H:%M"
            ax.xaxis.set_major_formatter(mdates.DateFormatter(fmt))
            fig.autofmt_xdate(rotation=30)

        def draw():
            days = days_var.get()
            fig.clear()
            no_data_lbl.config(text="")

            if dev_type == "Plug":
                timestamps, powers, energies = self._load_plug_csv(label, days)
                if not timestamps:
                    no_data_lbl.config(text=f"No CSV data for '{label}'.")
                    canvas.draw()
                    return
                pw = np.asarray(powers, dtype=float)
                en = np.asarray(energies, dtype=float)
                pw_ma = np.ma.masked_invalid(pw)
                en_ma = np.ma.masked_invalid(en)

                ax = fig.add_subplot(111)
                style_ax(ax)
                lw_p, lw_e = 1.2, 1.0
                ax.plot(
                    timestamps,
                    pw_ma,
                    color=BLUE,
                    linewidth=lw_p,
                    label="Power (W)",
                    zorder=3,
                    alpha=0.95,
                )
                if len(timestamps) > 1:
                    try:
                        pw_f = np.asarray(powers, dtype=float)
                        valid = np.isfinite(pw_f)
                        if valid.any():
                            ax.fill_between(
                                timestamps,
                                0,
                                pw_f,
                                where=valid,
                                alpha=0.14,
                                color=BLUE,
                                zorder=2,
                                interpolate=True,
                            )
                    except (TypeError, ValueError):
                        pass
                ax.set_ylabel("Power (W)", color=MUTED, fontsize=9)
                ax2 = ax.twinx()
                ax2.plot(
                    timestamps,
                    en_ma,
                    color=PURPLE,
                    linewidth=lw_e,
                    linestyle="--",
                    label="Energy (Wh)",
                    alpha=0.95,
                )
                ax2.set_ylabel("Energy (Wh)", color=MUTED, fontsize=9)
                pw_fin = pw[np.isfinite(pw)]
                avg_str = f"{float(np.mean(pw_fin)):.2f}" if pw_fin.size else "-"
                en_fin = en[np.isfinite(en)]
                last_e_str = f"{float(en_fin[-1]):.1f}" if en_fin.size else "-"
                ax.set_title(
                    f"{label}  |  Avg {avg_str} W  |  Last E {last_e_str} Wh",
                    color=FG,
                    fontsize=10,
                    pad=10,
                )
                h1, l1 = ax.get_legend_handles_labels()
                h2, l2 = ax2.get_legend_handles_labels()
                ax.legend(h1 + h2, l1 + l2, facecolor=CARD_BG, edgecolor=BORDER, labelcolor=FG, fontsize=9, loc="upper left")
                fmt_xaxis(ax, days)
                if len(timestamps) > 2:
                    ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=10))

            elif dev_type == "Vacuum":
                timestamps, powers, energies = self._load_vacuum_csv(label, days)
                if not timestamps:
                    no_data_lbl.config(text=f"No CSV data for '{label}'.")
                    canvas.draw()
                    return
                ax = fig.add_subplot(111)
                style_ax(ax)
                ax.plot(timestamps, powers, color=PURPLE, linewidth=1.5, label="Power (W)", zorder=3)
                ax.fill_between(timestamps, powers, alpha=0.12, color=PURPLE)
                ax.set_ylabel("Power (W)", color=MUTED, fontsize=9)
                ax2 = ax.twinx()
                ax2.plot(timestamps, energies, color=BLUE, linewidth=1.2, linestyle="--", label="Energy (Wh)")
                ax2.set_ylabel("Energy (Wh)", color=MUTED, fontsize=9)
                last_e = energies[-1] if energies else 0
                ax.set_title(
                    f"{label}  |  Avg {sum(powers)/len(powers):.2f} W  |  Last E {last_e:.1f} Wh",
                    color=FG,
                    fontsize=10,
                    pad=10,
                )
                h1, l1 = ax.get_legend_handles_labels()
                h2, l2 = ax2.get_legend_handles_labels()
                ax.legend(h1 + h2, l1 + l2, facecolor=CARD_BG, edgecolor=BORDER, labelcolor=FG, fontsize=9, loc="upper left")
                fmt_xaxis(ax, days)

            elif dev_type == "Door":
                timestamps, contacts, temps = self._load_door_csv(label, days)
                if not timestamps:
                    no_data_lbl.config(text=f"No CSV data for '{label}'.")
                    canvas.draw()
                    return
                ax1 = fig.add_subplot(2, 1, 1)
                ax2 = fig.add_subplot(2, 1, 2, sharex=ax1)
                style_ax(ax1)
                style_ax(ax2)
                ax1.step(timestamps, contacts, color=CYAN, linewidth=1.2, where="post", label="Contact")
                ax1.fill_between(timestamps, contacts, alpha=0.2, color=CYAN, step="post")
                ax1.set_yticks([0, 1])
                ax1.set_yticklabels(["Closed", "Open"], color=MUTED, fontsize=8)
                ax1.set_ylabel("Door", color=MUTED, fontsize=9)
                ax1.set_title(f"{label}  |  Last {days} day(s)", color=FG, fontsize=10, pad=8)
                ax1.legend(facecolor=CARD_BG, edgecolor=BORDER, labelcolor=FG, fontsize=9)
                last_temp = None
                filled = []
                for v in temps:
                    if v is not None:
                        last_temp = v
                    filled.append(last_temp)
                pairs = [(t, v) for t, v in zip(timestamps, filled) if v is not None]
                if pairs:
                    t_ts, t_vals = zip(*pairs)
                    ax2.plot(t_ts, t_vals, color=PURPLE, linewidth=1.5, label="Temp (°C)")
                    ax2.fill_between(t_ts, t_vals, alpha=0.15, color=PURPLE)
                    ax2.legend(facecolor=CARD_BG, edgecolor=BORDER, labelcolor=FG, fontsize=9)
                else:
                    ax2.set_title("No temperature data", color=MUTED, fontsize=9, pad=4)
                ax2.set_ylabel("Temp (°C)", color=MUTED, fontsize=9)
                fmt_xaxis(ax2, days)
                fig.tight_layout(pad=1.5)
                canvas.draw()
                return

            elif dev_type == "Camera":
                timestamps, motions, sounds = self._load_camera_csv(days)
                if not timestamps:
                    no_data_lbl.config(text="No camera CSV data.")
                    canvas.draw()
                    return
                ax1 = fig.add_subplot(2, 1, 1)
                ax2 = fig.add_subplot(2, 1, 2, sharex=ax1)
                style_ax(ax1)
                style_ax(ax2)
                ax1.step(timestamps, motions, color=ORANGE, linewidth=1.2, where="post", label="Motion")
                ax1.fill_between(timestamps, motions, alpha=0.2, color=ORANGE, step="post")
                ax1.set_yticks([0, 1])
                ax1.set_yticklabels(["inactive", "active"])
                ax1.set_title(f"Camera events  |  Last {days} day(s)", color=FG, fontsize=10, pad=10)
                ax1.legend(facecolor=CARD_BG, edgecolor=BORDER, labelcolor=FG, fontsize=9)
                ax2.step(timestamps, sounds, color=CYAN, linewidth=1.2, where="post", label="Sound")
                ax2.fill_between(timestamps, sounds, alpha=0.2, color=CYAN, step="post")
                ax2.set_yticks([0, 1])
                ax2.set_yticklabels(["not detected", "detected"])
                ax2.legend(facecolor=CARD_BG, edgecolor=BORDER, labelcolor=FG, fontsize=9)
                fmt_xaxis(ax2, days)
                fig.tight_layout(pad=1.5)
                canvas.draw()
                return

            else:  # Motion
                timestamps, motions, temps = self._load_motion_csv(label, days)
                if not timestamps:
                    no_data_lbl.config(text=f"No CSV data for '{label}'.")
                    canvas.draw()
                    return
                ax1 = fig.add_subplot(2, 1, 1)
                ax2 = fig.add_subplot(2, 1, 2, sharex=ax1)
                style_ax(ax1)
                style_ax(ax2)
                ax1.step(timestamps, motions, color=ORANGE, linewidth=1.2, where="post", label="Motion")
                ax1.fill_between(timestamps, motions, alpha=0.2, color=ORANGE, step="post")
                ax1.set_yticks([0, 1])
                ax1.set_yticklabels(["Inactive", "Active"], color=MUTED, fontsize=8)
                ax1.set_ylabel("Motion", color=MUTED, fontsize=9)
                ax1.set_title(f"{label}  |  Last {days} day(s)", color=FG, fontsize=10, pad=8)
                ax1.legend(facecolor=CARD_BG, edgecolor=BORDER, labelcolor=FG, fontsize=9)
                last_temp = None
                filled = []
                for v in temps:
                    if v is not None:
                        last_temp = v
                    filled.append(last_temp)
                pairs = [(t, v) for t, v in zip(timestamps, filled) if v is not None]
                if pairs:
                    t_ts, t_vals = zip(*pairs)
                    ax2.plot(t_ts, t_vals, color=PURPLE, linewidth=1.5, label="Temp (°C)")
                    ax2.fill_between(t_ts, t_vals, alpha=0.15, color=PURPLE)
                    ax2.legend(facecolor=CARD_BG, edgecolor=BORDER, labelcolor=FG, fontsize=9)
                else:
                    ax2.set_title("No temperature data", color=MUTED, fontsize=9, pad=4)
                ax2.set_ylabel("Temp (°C)", color=MUTED, fontsize=9)
                fmt_xaxis(ax2, days)

            fig.tight_layout()
            canvas.draw()

        draw()


def run_dashboard(collector_module):
    app = Dashboard(collector_module)
    app.mainloop()
