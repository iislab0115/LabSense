"""
SmartThings collection worker (for the data-collection bundle)

Origin: smartthings-collector/smartthings_collector.py
- Under unified execution, `SMARTTHINGS_EMBEDDED=1` reduces duplicate console logs.
- SMP labels owned by Shelly (`shelly_collector.DEVICES`) are skipped here.
- Any other `SMP*` label is collected from SmartThings as `stplug` (no cap).

=== Program Overview ===
1. Required libraries:
   - pip install aiohttp

2. Execution order:
   - First run: execute smartthings_auth.py → token file created
   - After: just run this script. Token is refreshed automatically.

3. Data collection logic:
   - All devices polled every 60 seconds in a single unified loop.
   - SmartThings plugs (labels starting with SMP, excluding shelly_collector.DEVICES):
     `/devices/{id}/status` polling (stplug; no fixed max count).
   - Shelly plugs: collected by shelly_collector on LAN (1s).
   - Motion sensors: polls device history for new events since last poll.
     CSV timestamp = exact event time recorded by the device (ms precision).
   - Door sensors : polls device history for new contact/temperature/battery events since last poll.
     CSV timestamp = exact event time recorded by the device (ms precision).
   - Devices with missing fields or errors are added to the ban list.

4. Token management:
   - access_token expires every 24 hours.
   - Automatically refreshes using refresh_token 30 minutes before expiry.
   - Immediately attempts token refresh on 401 response.
   - Safely shuts down on token refresh failure.

5. Network error handling:
   - Retries with exponential backoff on transient errors like DNS failures. (max 5 times)
"""

import os
import csv
import json
import base64
import aiohttp
import asyncio
import logging
import signal
from datetime import datetime, timedelta
from pathlib import Path
 
# === Load config (config.local.json overrides config.json) ===
def _resolve_config_path():
    base = Path(__file__).parent
    local = base / "config.local.json"
    if local.exists():
        return local
    return base / "config.json"

def _load_config():
    config_path = _resolve_config_path()
    if not config_path.exists():
        raise FileNotFoundError(
            "config.json / config.local.json not found.\n"
            "Copy config.json to config.local.json and fill in CLIENT_ID and CLIENT_SECRET."
        )
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)

_config = _load_config()

def reload_config():
    """(Optional) Reload config at runtime if you later add hot-reload needs."""
    try:
        config_path = _resolve_config_path()
        with open(config_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"Failed to reload config: {e}")
        return None

# === Logging setup ===
LOG_FILE = os.path.abspath(_config["LOG_FILE"])
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
_EMBEDDED = os.environ.get("SMARTTHINGS_EMBEDDED", "0") == "1"

_st_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_root = logging.getLogger()
_root.setLevel(logging.INFO)


def _st_has_file_handler():
    for h in _root.handlers:
        if isinstance(h, logging.FileHandler):
            try:
                if getattr(h, "baseFilename", None) == LOG_FILE:
                    return True
            except Exception:
                pass
    return False


if not _st_has_file_handler():
    _fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    _fh.setFormatter(_st_fmt)
    _root.addHandler(_fh)
if not _EMBEDDED:
    _sh = logging.StreamHandler()
    _sh.setFormatter(_st_fmt)
    _root.addHandler(_sh)

# === Path settings ===
CSV_BASE_DIR  = os.path.abspath(_config["CSV_BASE_DIR"])
METADATA_FILE = os.path.abspath(_config["METADATA_FILE"])
BAN_LIST_FILE = os.path.abspath(_config["BAN_LIST_FILE"])
TOKEN_FILE    = os.path.abspath(_config["TOKEN_FILE"])
os.makedirs(os.path.dirname(METADATA_FILE), exist_ok=True)
os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)

# === OAuth settings ===
CLIENT_ID     = _config["CLIENT_ID"]
CLIENT_SECRET = _config["CLIENT_SECRET"]
REDIRECT_URI  = _config["REDIRECT_URI"]
TOKEN_URL     = _config["TOKEN_URL"]

# === API settings ===
API_BASE_URL = _config["API_BASE_URL"]

# === Interval settings (seconds) ===
DEVICE_UPDATE_INTERVAL = _config["DEVICE_UPDATE_INTERVAL"]   # device list refresh
DEVICE_STATUS_INTERVAL = _config["DEVICE_STATUS_INTERVAL"]   # unified poll interval (plug + motion/door/camera history)

# === Retry settings ===
MAX_RETRIES     = _config["MAX_RETRIES"]
BASE_RETRY_WAIT = _config["BASE_RETRY_WAIT"]
MAX_RETRY_WAIT  = _config["MAX_RETRY_WAIT"]

# === Token expiry margin ===
TOKEN_REFRESH_MARGIN = timedelta(minutes=30)

# === Session timeout ===
SESSION_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)

# === Local state ===
device_metadata   = []
ban_list          = []
running           = True
current_date      = datetime.now().strftime("%Y%m%d")
last_update_time  = None
motion_last_seen  = {}   # device_id -> epoch ms of last processed motion event
door_last_seen    = {}   # device_id -> epoch ms of last processed door/contact event
camera_metadata   = []   # [{id, location_id, label}, ...] cameras (events only)
camera_last_seen  = {}   # device_id -> epoch ms of last processed camera event
camera_latest     = {}   # device_id -> {"motion": str|None, "sound": str|None, "ts": str|None}

# === Token state ===
token_data = {
    "access_token":  None,
    "refresh_token": None,
    "expires_at":    None,
}
token_refreshing = False

# === Shared dashboard state ===
dashboard_state = {
    "status":        "Initializing",
    "last_cycle":    None,
    "token_expires": None,
    "total":         0,
    "success":       0,
    "fail":          0,
    "devices":       [],
    "camera": {
        "last_event_at": None,
        "last_event_label": None,
        "last_motion": None,
        "last_sound": None,
        "last_error": None,
    },
}
# === Network error types ===
_retriable = [
    aiohttp.ClientConnectorError,
    aiohttp.ServerDisconnectedError,
    aiohttp.ClientOSError,
    asyncio.TimeoutError,
]
if hasattr(aiohttp, "ClientConnectorDNSError"):
    _retriable.append(aiohttp.ClientConnectorDNSError)
RETRIABLE_EXCEPTIONS = tuple(_retriable)


# ==============================
# Token management
# ==============================

def load_token():
    global token_data
    if not os.path.exists(TOKEN_FILE):
        logging.error(
            f"Token file not found: {TOKEN_FILE}\n"
            "Please run smartthings_auth.py first to obtain a token."
        )
        return False

    with open(TOKEN_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)

    token_data["access_token"]  = raw.get("access_token")
    token_data["refresh_token"] = raw.get("refresh_token")

    if "expires_at" in raw:
        token_data["expires_at"] = datetime.fromisoformat(raw["expires_at"])
    elif "expires_in" in raw:
        token_data["expires_at"] = datetime.now() + timedelta(seconds=int(raw["expires_in"]))
    else:
        token_data["expires_at"] = datetime.now() + timedelta(hours=24)

    logging.info(
        f"Token loaded. Expires at: {token_data['expires_at'].strftime('%Y-%m-%d %H:%M:%S')}"
    )
    dashboard_state["token_expires"] = token_data["expires_at"].strftime("%Y-%m-%d %H:%M:%S")
    return True


def save_token():
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "access_token":  token_data["access_token"],
            "refresh_token": token_data["refresh_token"],
            "expires_at":    token_data["expires_at"].isoformat(),
        }, f, ensure_ascii=False, indent=4)
    logging.info("Token file updated.")
    dashboard_state["token_expires"] = token_data["expires_at"].strftime("%Y-%m-%d %H:%M:%S")


def get_headers():
    return {"Authorization": f"Bearer {token_data['access_token']}"}


def make_basic_auth_header():
    credentials = f"{CLIENT_ID}:{CLIENT_SECRET}"
    encoded = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")
    return f"Basic {encoded}"


def is_token_expiring():
    if token_data["expires_at"] is None:
        return True
    return datetime.now() >= token_data["expires_at"] - TOKEN_REFRESH_MARGIN


async def refresh_access_token():
    global token_data, token_refreshing

    if token_refreshing:
        logging.info("Another request is refreshing the token. Waiting...")
        while token_refreshing:
            await asyncio.sleep(0.5)
        return token_data["access_token"] is not None

    token_refreshing = True
    logging.info("Starting access_token refresh...")

    if not token_data["refresh_token"]:
        logging.error("No refresh_token. Please re-run smartthings_auth.py.")
        token_refreshing = False
        return False

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                TOKEN_URL,
                data={
                    "grant_type":    "refresh_token",
                    "refresh_token": token_data["refresh_token"],
                    "client_id":     CLIENT_ID,
                },
                headers={
                    "Content-Type":  "application/x-www-form-urlencoded",
                    "Authorization": make_basic_auth_header(),
                },
                timeout=SESSION_TIMEOUT
            ) as resp:
                if resp.status == 200:
                    raw = await resp.json()
                    token_data["access_token"] = raw["access_token"]
                    if "refresh_token" in raw:
                        token_data["refresh_token"] = raw["refresh_token"]
                    expires_in = int(raw.get("expires_in", 86400))
                    token_data["expires_at"] = datetime.now() + timedelta(seconds=expires_in)
                    save_token()
                    logging.info(
                        f"access_token refreshed successfully. "
                        f"New expiry: {token_data['expires_at'].strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                    token_refreshing = False
                    return True
                elif resp.status == 401:
                    logging.error(
                        "refresh_token has expired. (Expires after 29 days of inactivity) "
                        "Please re-run smartthings_auth.py to re-authenticate."
                    )
                    token_refreshing = False
                    return False
                else:
                    text = await resp.text()
                    logging.error(f"Token refresh failed ({resp.status}): {text}")
                    token_refreshing = False
                    return False
    except Exception as e:
        logging.error(f"Error during token refresh: {type(e).__name__}: {e}")
        token_refreshing = False
        return False


# ==============================
# Metadata / Ban list
# ==============================

def load_metadata():
    global device_metadata
    if os.path.exists(METADATA_FILE):
        with open(METADATA_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, list):
            device_metadata = raw
            logging.info(f"Metadata loaded. ({len(device_metadata)} device(s), legacy format)")
        else:
            device_metadata = raw.get("devices", [])
            logging.info(f"Metadata loaded. ({len(device_metadata)} device(s))")
    else:
        logging.warning("Metadata file not found. Initial update required.")

def save_metadata():
    data = {"devices": device_metadata}
    with open(METADATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    logging.info(f"Metadata saved: {METADATA_FILE}\n")

def load_ban_list():
    global ban_list
    if os.path.exists(BAN_LIST_FILE):
        with open(BAN_LIST_FILE, "r", encoding="utf-8") as f:
            ban_list = json.load(f)
        logging.info(f"Ban list loaded. ({len(ban_list)} device(s))")
    else:
        logging.info("Ban list file not found. Starting fresh.")


def _unban(device_id: str, reason: str):
    """Ensure a target device is not in the ban list."""
    if device_id in ban_list:
        ban_list.remove(device_id)
        logging.info(f"Removed from ban list: {device_id} ({reason})")

def save_ban_list():
    with open(BAN_LIST_FILE, "w", encoding="utf-8") as f:
        json.dump(ban_list, f, ensure_ascii=False, indent=4)
    logging.info(f"Ban list saved. ({len(ban_list)} device(s))")


# ==============================
# CSV saving
# ==============================

def save_motion_to_csv(device_status, device_id):
    """Save motion sensor data: motion, temperature."""
    global current_date
    today_date  = datetime.now().strftime("%Y%m%d")
    folder_path = os.path.join(CSV_BASE_DIR, today_date)
    if today_date != current_date or not os.path.exists(folder_path):
        current_date = today_date
        os.makedirs(folder_path, exist_ok=True)

    filename    = f"{device_status['label']}_{device_id}_{today_date}.csv"
    filepath    = os.path.join(folder_path, filename)
    file_exists = os.path.isfile(filepath)

    try:
        with open(filepath, mode='a', newline='', encoding='utf-8') as file:
            writer = csv.writer(file)
            if not file_exists:
                writer.writerow([
                    "Timestamp", "Label", "Location", "Room",
                    "Motion", "Temperature (°C)"
                ])
            writer.writerow([
                device_status["timestamp"],
                device_status["label"],
                device_status["location_name"],
                device_status["room_name"],
                device_status["motion"],
                device_status["temperature"],
            ])
        logging.info(
            f"CSV saved [Motion]: {device_status['label']} | "
            f"Motion={device_status['motion']}, "
            f"Temp={device_status['temperature']}°C"
        )
    except Exception as e:
        logging.error(f"CSV save error ({device_status['label']}): {e}")


# ==============================
# Camera / Vacuum / Door CSV saving
# ==============================

def save_camera_to_csv(timestamp_str, camera_label, motion=None, sound=None):
    """Save camera motion/sound events to CSV (no clip download)."""
    global current_date
    today_date  = datetime.now().strftime("%Y%m%d")
    folder_path = os.path.join(CSV_BASE_DIR, today_date)
    if today_date != current_date or not os.path.exists(folder_path):
        current_date = today_date
        os.makedirs(folder_path, exist_ok=True)

    filename    = f"Camera_{today_date}.csv"
    filepath    = os.path.join(folder_path, filename)
    file_exists = os.path.isfile(filepath)

    try:
        with open(filepath, mode="a", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            if not file_exists:
                writer.writerow(["Timestamp", "Camera", "Motion", "Sound"])
            writer.writerow([timestamp_str, camera_label, motion or "", sound or ""])
    except Exception as e:
        logging.error(f"CSV save error (Camera): {e}")


VACUUM_CSV_HEADER = [
    "Timestamp", "Label", "Location", "Room",
    "Power (W)", "Energy (Wh)",
    "Cleaner State", "Dustbin State", "Dustbin Last Emptied (UTC)",
]


def _st_capability_attr_string(main: dict, capability_id: str, attribute: str) -> str:
    """Extract the capability.attribute.value string from components.main."""
    cap = main.get(capability_id)
    if not isinstance(cap, dict):
        return ""
    attr_obj = cap.get(attribute)
    if isinstance(attr_obj, dict):
        v = attr_obj.get("value")
        if v is None:
            return ""
        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False)
        return str(v).strip().strip('"')
    if isinstance(attr_obj, str):
        return attr_obj.strip()
    return ""


def _migrate_vacuum_csv_to_latest(filepath: str) -> None:
    """Pad an older 5-8 column file out to the current VACUUM_CSV_HEADER width
    and rewrite it."""
    if not os.path.isfile(filepath) or os.path.getsize(filepath) == 0:
        return
    try:
        with open(filepath, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
    except OSError:
        return
    if not rows:
        return
    hdr = rows[0]
    if hdr == VACUUM_CSV_HEADER:
        return
    n = len(VACUUM_CSV_HEADER)
    new_rows = [VACUUM_CSV_HEADER]
    for r in rows[1:]:
        padded = (list(r) + [""] * n)[:n]
        new_rows.append(padded)
    try:
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(new_rows)
        logging.info(f"Vacuum CSV migrated to {n} columns: {filepath}")
    except OSError as e:
        logging.error(f"Vacuum CSV migrate failed: {e}")


def save_vacuum_to_csv(device_status, device_id):
    """Stick vacuum: power and energy, cleaner and dustbin state, and
    lastEmptiedTime, on every cycle."""
    global current_date
    today_date  = datetime.now().strftime("%Y%m%d")
    folder_path = os.path.join(CSV_BASE_DIR, today_date)
    if today_date != current_date or not os.path.exists(folder_path):
        current_date = today_date
        os.makedirs(folder_path, exist_ok=True)

    filename    = f"{device_status['label']}_{device_id}_{today_date}.csv"
    filepath    = os.path.join(folder_path, filename)
    file_exists = os.path.isfile(filepath)

    p = device_status.get("power", "")
    e = device_status.get("energy", "")
    if p is None:
        p = ""
    if e is None:
        e = ""
    cs = device_status.get("cleaner_state", "") or ""
    ds = device_status.get("dustbin_state", "") or ""
    de = device_status.get("dustbin_last_emptied", "") or ""

    row = [
        device_status["timestamp"],
        device_status["label"],
        device_status["location_name"],
        device_status["room_name"],
        p,
        e,
        cs,
        ds,
        de,
    ]

    try:
        if not file_exists:
            with open(filepath, mode="w", newline="", encoding="utf-8") as file:
                w = csv.writer(file)
                w.writerow(VACUUM_CSV_HEADER)
                w.writerow(row)
        else:
            _migrate_vacuum_csv_to_latest(filepath)
            with open(filepath, mode="a", newline="", encoding="utf-8") as file:
                csv.writer(file).writerow(row)
        logging.info(
            f"CSV saved [Vacuum]: {device_status['label']} | "
            f"P={device_status.get('power')} W, E={device_status.get('energy')} Wh | "
            f"Cleaner={cs!r}, Dustbin={ds!r}, LastEmptied={de!r}"
        )
    except Exception as e:
        logging.error(f"CSV save error ({device_status['label']}): {e}")


# ==============================
# API requests (retry + auto 401 refresh)
# ==============================

def save_plug_to_csv(device_status, device_id):
    """Save SmartThings smart plug data: switch on/off + power + energy.

    The header carries 'Timestamp / Power (W) / Energy (Wh)', which
    dashboard._load_plug_csv recognises, so the Shelly plug graph code is reused as is.
    """
    global current_date
    today_date  = datetime.now().strftime("%Y%m%d")
    folder_path = os.path.join(CSV_BASE_DIR, today_date)
    if today_date != current_date or not os.path.exists(folder_path):
        current_date = today_date
        os.makedirs(folder_path, exist_ok=True)

    filename    = f"{device_status['label']}_{device_id}_{today_date}.csv"
    filepath    = os.path.join(folder_path, filename)
    file_exists = os.path.isfile(filepath)

    p = device_status.get("power")
    e = device_status.get("energy")
    if p is None:
        p = ""
    if e is None:
        e = ""

    try:
        with open(filepath, mode='a', newline='', encoding='utf-8') as file:
            writer = csv.writer(file)
            if not file_exists:
                writer.writerow([
                    "Timestamp", "Label", "Location", "Room",
                    "Switch", "Power (W)", "Energy (Wh)"
                ])
            writer.writerow([
                device_status["timestamp"],
                device_status["label"],
                device_status["location_name"],
                device_status["room_name"],
                device_status.get("switch", ""),
                p,
                e,
            ])
        logging.info(
            f"CSV saved [Plug]: {device_status['label']} | "
            f"Switch={device_status.get('switch')}, "
            f"P={device_status.get('power')} W, "
            f"E={device_status.get('energy')} Wh"
        )
    except Exception as e:
        logging.error(f"CSV save error ({device_status['label']}): {e}")


def save_door_to_csv(device_status, device_id):
    """Save door sensor data: contact(open/closed), temperature, battery."""
    global current_date
    today_date  = datetime.now().strftime("%Y%m%d")
    folder_path = os.path.join(CSV_BASE_DIR, today_date)
    if today_date != current_date or not os.path.exists(folder_path):
        current_date = today_date
        os.makedirs(folder_path, exist_ok=True)

    filename    = f"{device_status['label']}_{device_id}_{today_date}.csv"
    filepath    = os.path.join(folder_path, filename)
    file_exists = os.path.isfile(filepath)

    try:
        with open(filepath, mode="a", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            if not file_exists:
                writer.writerow([
                    "Timestamp", "Label", "Location", "Room",
                    "Contact", "Temperature (°C)", "Battery (%)"
                ])
            writer.writerow([
                device_status["timestamp"],
                device_status["label"],
                device_status["location_name"],
                device_status["room_name"],
                device_status.get("contact", ""),
                device_status.get("temperature", ""),
                device_status.get("battery", ""),
            ])
        logging.info(
            f"CSV saved [Door]: {device_status['label']} | "
            f"Contact={device_status.get('contact')}, "
            f"Temp={device_status.get('temperature')}, "
            f"Battery={device_status.get('battery')}"
        )
    except Exception as e:
        logging.error(f"CSV save error ({device_status['label']}): {e}")


async def request_with_retry(session, url, context="request"):
    global running
    wait = BASE_RETRY_WAIT
    token_refreshed = False

    if is_token_expiring():
        success = await refresh_access_token()
        if not success:
            logging.error("Halting request due to token refresh failure.")
            return None
        token_refreshed = True

    for attempt in range(1, MAX_RETRIES + 1):
        if not running:
            return None
        try:
            async with session.get(url, headers=get_headers()) as resp:
                if resp.status == 200:
                    return await resp.json()

                elif resp.status == 401:
                    if token_refreshed:
                        logging.error(
                            f"[{context}] 401 persists after token refresh. Shutting down."
                        )
                        running = False
                        return None
                    logging.warning(f"[{context}] 401 Unauthorized → attempting token refresh...")
                    success = await refresh_access_token()
                    if not success:
                        logging.error("Token refresh failed. Shutting down.")
                        running = False
                        return None
                    token_refreshed = True
                    continue

                elif resp.status == 403:
                    if attempt <= 2:
                        logging.warning(f"[{context}] HTTP 403 ({attempt}/2 attempt(s). {wait}s, retrying...")
                        await asyncio.sleep(wait)
                        wait = min(wait * 2, MAX_RETRY_WAIT)
                        continue
                    else:
                        logging.warning(f"[{context}] HTTP 403. Retry limit reached.")
                        return None

                elif 400 <= resp.status < 500:
                    logging.warning(f"[{context}] HTTP {resp.status}. Not retrying.")
                    return None

                else:
                    logging.warning(f"[{context}] HTTP {resp.status}. (attempt {attempt}/{MAX_RETRIES})")

        except RETRIABLE_EXCEPTIONS as e:
            logging.warning(
                f"[{context}] Network error (attempt {attempt}/{MAX_RETRIES}): "
                f"{type(e).__name__}: {e}"
            )
        except Exception as e:
            logging.error(f"[{context}] Unexpected error: {type(e).__name__}: {e}")
            return None

        if attempt < MAX_RETRIES:
            logging.info(f"[{context}] {wait}s, retrying...")
            await asyncio.sleep(wait)
            wait = min(wait * 2, MAX_RETRY_WAIT)

    logging.error(f"[{context}] Max retries exceeded.")
    return None


# ==============================
# API fetch functions
# ==============================

async def fetch_location_name(session, location_id):
    data = await request_with_retry(
        session,
        f"{API_BASE_URL}/locations/{location_id}",
        context=f"Location lookup ({location_id})"
    )
    return data.get("name", "Unknown") if data else "Unknown"


async def fetch_room_name(session, location_id, room_id):
    """Fetch room name. Returns empty string if roomId is missing."""
    if not room_id:
        return ""
    data = await request_with_retry(
        session,
        f"{API_BASE_URL}/locations/{location_id}/rooms/{room_id}",
        context=f"Room lookup ({room_id})"
    )
    return data.get("name", "") if data else ""


def _is_camera_device(device: dict) -> bool:
    """Detect camera by profile, name, label, or type."""
    label = str(device.get("label", "")).lower()
    name  = str(device.get("name", "")).lower()
    profile = device.get("profile", {}) or {}
    profile_id = str(profile.get("id", "")).lower()
    dev_type = str(device.get("type", "")).upper()
    return (
        dev_type == "VIDEO"
        or "camera" in label
        or "camera" in name
        or "camera" in profile_id
        or "imi.camera" in name
    )


def _shelly_plug_labels() -> set:
    """The set of SMP labels Shelly owns directly (upper case).

    shelly_collector.DEVICES is authoritative; any SMP* label absent from it is
    collected through the SmartThings API. Returns an empty set if the import fails.
    """
    try:
        import shelly_collector as _sc
        return {str(d.get("label", "")).strip().upper() for d in getattr(_sc, "DEVICES", [])}
    except Exception:
        return set()


async def fetch_device_list(session):
    """Fetch device list and update metadata.
    - Smart plugs (SMP*):
        - label present in shelly_collector.DEVICES: skip (Shelly polls it at 1 s)
        - any other SMP* label: register as type="stplug" (SmartThings status,
          polled at 60 s only)
    - Motion sensors: starting with MS (or name contains "motion")
    - Door sensors  : starting with DS (contact sensor)
    - Cameras       : detected by profile/name/label/type, stored in camera_metadata (events only)
    - Stick vacuum  : label contains "vacuum" (OCF), collects powerConsumptionReport
    """
    global device_metadata, camera_metadata, ban_list

    data = await request_with_retry(session, f"{API_BASE_URL}/devices", context="Device list fetch")
    if data is None:
        logging.error("Device list fetch failed. Keeping existing metadata.")
        return

    metadata = []
    cams = []
    location_cache = {}
    ban_changed = False
    shelly_labels = _shelly_plug_labels()

    for device in data.get("items", []):
        label       = device.get("label", "")
        device_id   = device["deviceId"]
        location_id = device.get("locationId", "")
        room_id     = device.get("roomId", "")  # None or empty string if missing

        label_u  = str(label).strip()
        label_up = label_u.upper()
        raw_type = str(device.get("type", "")).upper()
        is_plug  = label_up.startswith("SMP")
        is_door  = label_up.startswith("DS")
        dev_name = device.get("name", "")
        is_motion = label_up.startswith("MS") or ("motion" in str(dev_name).lower())
        is_camera = _is_camera_device(device)
        is_vacuum = ("vacuum" in label_u.lower()) or ("[vacuum]" in str(dev_name).lower())

        if is_camera:
            # Exclude virtual "camera" devices (e.g. Camera205/Camera207 placeholders)
            if raw_type == "VIRTUAL":
                if device_id not in ban_list:
                    ban_list.append(device_id)
                    logging.info(f"Virtual camera excluded → added to ban list: {label_u} (ID={device_id})")
                    ban_changed = True
                continue
            if device_id in ban_list:
                _unban(device_id, f"camera target ({label_u})")
                ban_changed = True
            # Cache location/room names for camera display in dashboard
            if location_id not in location_cache:
                location_cache[location_id] = await fetch_location_name(session, location_id)
            location_name = location_cache[location_id]
            room_name = await fetch_room_name(session, location_id, room_id)
            cams.append({
                "id": device_id,
                "location_id": location_id,
                "location_name": location_name,
                "room_name": room_name,
                "label": label_u or "Camera",
            })
            logging.info(f"Camera detected: {label_u} (ID={device_id})")
            continue

        # SMP labels:
        #  - present in shelly_collector.DEVICES: Shelly collects at 1 s (skip)
        #  - otherwise: polled through the SmartThings API (stplug)
        if is_plug:
            if label_up in shelly_labels:
                continue
            if device_id in ban_list:
                _unban(device_id, f"smartthings plug ({label_u})")
                ban_changed = True
            if location_id not in location_cache:
                location_cache[location_id] = await fetch_location_name(session, location_id)
            location_name = location_cache[location_id]
            room_name = await fetch_room_name(session, location_id, room_id)
            metadata.append({
                "id":            device_id,
                "label":         label,
                "location_id":   location_id,
                "location_name": location_name,
                "room_name":     room_name,
                "type":          "stplug",
            })
            logging.info(
                f"Device registered: [stplug] {label} | "
                f"Location={location_name}, Room={room_name or '(none)'}"
            )
            continue

        if not (is_motion or is_door or is_vacuum):
            if device_id not in ban_list:
                ban_list.append(device_id)
                logging.info(f"Not a target device, added to ban list: {label} (ID={device_id})")
                ban_changed = True
            continue
        else:
            if device_id in ban_list:
                _unban(device_id, f"target device ({label_u})")
                ban_changed = True

        # Cache location name
        if location_id not in location_cache:
            location_cache[location_id] = await fetch_location_name(session, location_id)
        location_name = location_cache[location_id]

        # Fetch room name
        room_name = await fetch_room_name(session, location_id, room_id)

        if is_door:
            device_type = "door"
        elif is_vacuum:
            device_type = "vacuum"
        else:
            device_type = "motion"
        metadata.append({
            "id":            device_id,
            "label":         label,
            "location_id":   location_id,
            "location_name": location_name,
            "room_name":     room_name,
            "type":          device_type,
        })
        logging.info(
            f"Device registered: [{device_type}] {label} | "
            f"Location={location_name}, Room={room_name or '(none)'}"
        )

    device_metadata = metadata
    camera_metadata = cams
    if ban_changed:
        save_ban_list()
    logging.info(
        f"Device list updated. "
        f"Motion: {sum(1 for d in metadata if d['type']=='motion')}, "
        f"Door: {sum(1 for d in metadata if d['type']=='door')}, "
        f"Vacuum: {sum(1 for d in metadata if d['type']=='vacuum')}, "
        f"Plug (ST): {sum(1 for d in metadata if d['type']=='stplug')}, "
        f"Cameras: {len(cams)}"
    )
    save_metadata()


def _deep_find_first_number(obj):
    """Best-effort: find the first numeric value in nested dict/list."""
    if isinstance(obj, dict):
        # Direct numeric-like fields
        for k in ("power", "powerConsumption", "consumption", "watts", "watt", "value"):
            if k in obj:
                v = obj.get(k)
                if isinstance(v, (int, float)):
                    return v
                if isinstance(v, str):
                    try:
                        if v.strip() != "":
                            return float(v.strip())
                    except ValueError:
                        pass
                # If nested, keep searching
                found = _deep_find_first_number(v)
                if found is not None:
                    return found
        for v in obj.values():
            found = _deep_find_first_number(v)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _deep_find_first_number(v)
            if found is not None:
                return found
    return None


def _num_from_pc_attr(v):
    """Parse numeric from SmartThings attribute value (int/float/str)."""
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str) and v.strip():
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


async def fetch_vacuum_power(session, device):
    """Fetch stick vacuum: main.powerConsumptionReport.powerConsumption.value { power, energy, ... }."""
    device_id = device["id"]
    if device_id in ban_list:
        return None

    data = await request_with_retry(
        session,
        f"{API_BASE_URL}/devices/{device_id}/status",
        context=f"Vacuum status ({device['label']})"
    )
    if data is None:
        return None

    main = data.get("components", {}).get("main", {})

    # Samsung stick vacuum: cleaner and dustbin state, written every cycle
    cleaner_state = _st_capability_attr_string(main, "samsungce.stickCleanerStatus", "operatingState")
    dustbin_state = _st_capability_attr_string(main, "samsungce.stickCleanerDustbinStatus", "operatingState")
    dustbin_last_emptied = _st_capability_attr_string(
        main, "samsungce.stickCleanerDustbinStatus", "lastEmptiedTime"
    )

    power_val = None
    energy_val = None

    # Primary: powerConsumptionReport.powerConsumption.value = { "power", "energy", ... }
    try:
        pcr = main.get("powerConsumptionReport") or {}
        pc = pcr.get("powerConsumption") or {}
        v = pc.get("value")
        if isinstance(v, dict):
            power_val = _num_from_pc_attr(v.get("power"))
            energy_val = _num_from_pc_attr(v.get("energy"))
    except Exception:
        pass

    # Try common paths for power only; energy comes from the object above
    candidates = [
        ("powerConsumptionReport", "powerConsumption"),
        ("powerConsumptionReport", "power"),
        ("powerMeter", "power"),
    ]
    if power_val is None:
        for cap, attr in candidates:
            cap_obj = main.get(cap) or {}
            attr_obj = cap_obj.get(attr) or {}
            v = attr_obj.get("value")
            if isinstance(v, dict):
                power_val = _num_from_pc_attr(v.get("power"))
                if energy_val is None:
                    energy_val = _num_from_pc_attr(v.get("energy"))
                if power_val is not None:
                    break
            if isinstance(v, (int, float)):
                power_val = v
                break
            if isinstance(v, str):
                try:
                    if v.strip():
                        power_val = float(v.strip())
                        break
                except ValueError:
                    pass

    # Fallback: power only, leaving energy untouched
    if power_val is None and "powerConsumptionReport" in main:
        pcr = main.get("powerConsumptionReport") or {}
        pc = pcr.get("powerConsumption") or {}
        v = pc.get("value")
        if isinstance(v, dict) and v.get("power") is not None:
            power_val = _num_from_pc_attr(v.get("power"))

    if power_val is None and "powerConsumptionReport" in main:
        # Last resort: look for a 'power' key anywhere in the nested dict
        def _find_power_key(obj):
            if isinstance(obj, dict):
                if "power" in obj and obj.get("power") is not None:
                    return _num_from_pc_attr(obj.get("power"))
                for x in obj.values():
                    r = _find_power_key(x)
                    if r is not None:
                        return r
            return None

        power_val = _find_power_key(main.get("powerConsumptionReport"))

    if power_val is None and energy_val is None:
        logging.warning(
            f"[Vacuum] no power/energy in powerConsumptionReport: {device['label']} "
            f"(main caps={list(main.keys())})"
        )
        return None

    status = {
        "timestamp":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "label":         device["label"],
        "location_name": device["location_name"],
        "room_name":     device["room_name"],
        "power":         power_val,
        "energy":        energy_val,
        "cleaner_state": cleaner_state,
        "dustbin_state": dustbin_state,
        "dustbin_last_emptied": dustbin_last_emptied,
        "type":          "vacuum",
    }
    save_vacuum_to_csv(status, device_id)

    return status


async def fetch_plug_status(session, device):
    """Poll a single SmartThings smart plug.

    - main.switch.switch.value          : 'on' / 'off'
    - main.powerMeter.power.value       : instantaneous power (W)
    - main.energyMeter.energy.value     : cumulative energy (kWh or Wh, per unit)
    - main.powerConsumptionReport.powerConsumption.value.{power,energy} : fallback

    Normalised to 'Power (W)' / 'Energy (Wh)' so the CSV header stays compatible
    with the Shelly plug files.
    """
    device_id = device["id"]
    if device_id in ban_list:
        return None

    data = await request_with_retry(
        session,
        f"{API_BASE_URL}/devices/{device_id}/status",
        context=f"Plug status ({device['label']})"
    )
    if data is None:
        return None

    main = data.get("components", {}).get("main", {})

    switch_val = _st_capability_attr_string(main, "switch", "switch")

    power_val = None
    energy_val = None

    # powerMeter.power
    pm = main.get("powerMeter") or {}
    pm_attr = pm.get("power") or {}
    if isinstance(pm_attr, dict):
        power_val = _num_from_pc_attr(pm_attr.get("value"))

    # energyMeter.energy, converted to Wh when the unit is kWh
    em = main.get("energyMeter") or {}
    em_attr = em.get("energy") or {}
    if isinstance(em_attr, dict):
        e_raw = _num_from_pc_attr(em_attr.get("value"))
        if e_raw is not None:
            unit = str(em_attr.get("unit", "")).strip().lower()
            if unit == "kwh":
                energy_val = e_raw * 1000.0
            else:
                # 'wh', or no unit at all: use the value as is
                energy_val = e_raw

    # Fallback: powerConsumptionReport.powerConsumption.value = { power, energy(Wh), ... }
    if power_val is None or energy_val is None:
        pcr = main.get("powerConsumptionReport") or {}
        pc_val = (pcr.get("powerConsumption") or {}).get("value")
        if isinstance(pc_val, dict):
            if power_val is None:
                power_val = _num_from_pc_attr(pc_val.get("power"))
            if energy_val is None:
                energy_val = _num_from_pc_attr(pc_val.get("energy"))

    if switch_val == "" and power_val is None and energy_val is None:
        logging.warning(
            f"[Plug] could not read switch, power or energy: {device['label']} "
            f"(main caps={list(main.keys())})"
        )
        return None

    status = {
        "timestamp":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "label":         device["label"],
        "location_name": device["location_name"],
        "room_name":     device["room_name"],
        "switch":        switch_val,
        "power":         power_val,
        "energy":        energy_val,
        "type":          "stplug",
    }
    save_plug_to_csv(status, device_id)
    return status


async def fetch_motion_history(session, device):
    """
    Fetch new motion events from device history since last poll (60s).
    Uses the device's own event timestamp for CSV.
    Returns latest motion state for dashboard display.
    """
    global motion_last_seen

    device_id = device["id"]
    if device_id in ban_list:
        return None

    after_ms    = motion_last_seen.get(device_id, 0)
    location_id = device.get("location_id", "")
    url = (f"{API_BASE_URL}/history/devices"
           f"?deviceId={device_id}&locationId={location_id}&limit=100")
    if after_ms:
        url += f"&oldestFirst=true&pagingAfterEpoch={after_ms}"

    data = await request_with_retry(session, url,
        context=f"Motion history ({device['label']})")
    if data is None:
        return None

    items = data.get("items", [])
    if not items:
        prev = motion_last_seen.get(device_id)
        return {"motion": "inactive", "temperature": None,
                "label": device["label"],
                "location_name": device["location_name"],
                "room_name": device["room_name"]}

    items.sort(key=lambda x: x.get("epoch", 0))

    latest_motion = None
    latest_temp   = None
    new_last_seen = after_ms

    for item in items:
        epoch = item.get("epoch", 0)
        cap   = item.get("capability", "")
        attr  = item.get("attribute", "")
        value = item.get("value", "")

        if item.get("component") != "main":
            continue
        if epoch > new_last_seen:
            new_last_seen = epoch

        event_dt  = datetime.fromtimestamp(epoch / 1000)
        event_str = event_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

        if cap == "motionSensor" and attr == "motion":
            latest_motion = value
            status = {
                "timestamp":     event_str,
                "label":         device["label"],
                "location_name": device["location_name"],
                "room_name":     device["room_name"],
                "motion":        value,
                "temperature":   latest_temp,
                "type":          "motion",
            }
            save_motion_to_csv(status, device_id)
            logging.info(f"[Motion] {device['label']} → {value} at {event_str}")

        elif cap == "temperatureMeasurement" and attr == "temperature":
            try:
                latest_temp = float(value)
                # Save temperature-only event for smoother dashboard graph
                status = {
                    "timestamp":     event_str,
                    "label":         device["label"],
                    "location_name": device["location_name"],
                    "room_name":     device["room_name"],
                    "motion":        latest_motion or "inactive",
                    "temperature":   latest_temp,
                    "type":          "motion",
                }
                save_motion_to_csv(status, device_id)
                logging.debug(f"[Motion] {device['label']} temp={latest_temp}°C at {event_str}")
            except (ValueError, TypeError):
                pass

    if new_last_seen > after_ms:
        motion_last_seen[device_id] = new_last_seen + 1

    return {
        "motion":        latest_motion or "inactive",
        "temperature":   latest_temp,
        "label":         device["label"],
        "location_name": device["location_name"],
        "room_name":     device["room_name"],
    }

async def fetch_door_history(session, device):
    """
    Fetch new door sensor events (contact/temperature/battery) from device history since last poll.
    Uses the device's own event timestamp for CSV.
    Returns latest state for dashboard display.
    """
    global door_last_seen

    device_id = device["id"]
    if device_id in ban_list:
        return None

    after_ms    = door_last_seen.get(device_id, 0)
    location_id = device.get("location_id", "")
    url = (f"{API_BASE_URL}/history/devices"
           f"?deviceId={device_id}&locationId={location_id}&limit=100")
    if after_ms:
        url += f"&oldestFirst=true&pagingAfterEpoch={after_ms}"

    data = await request_with_retry(session, url, context=f"Door history ({device['label']})")
    if data is None:
        return None

    items = data.get("items", [])
    if not items:
        return {
            "contact":     "closed",
            "temperature": None,
            "battery":     None,
            "label":       device["label"],
            "location_name": device["location_name"],
            "room_name":     device["room_name"],
        }

    items.sort(key=lambda x: x.get("epoch", 0))
    latest_contact = None
    latest_temp    = None
    latest_batt    = None
    new_last_seen  = after_ms

    for item in items:
        if item.get("component") != "main":
            continue

        epoch = item.get("epoch", 0)
        cap   = item.get("capability", "")
        attr  = item.get("attribute", "")
        value = item.get("value", "")

        if epoch > new_last_seen:
            new_last_seen = epoch

        event_dt  = datetime.fromtimestamp(epoch / 1000)
        event_str = event_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

        if cap == "contactSensor" and attr == "contact":
            latest_contact = value
            status = {
                "timestamp":     event_str,
                "label":         device["label"],
                "location_name": device["location_name"],
                "room_name":     device["room_name"],
                "contact":       value,
                "temperature":   latest_temp,
                "battery":       latest_batt,
                "type":          "door",
            }
            save_door_to_csv(status, device_id)
            logging.info(f"[Door] {device['label']} → {value} at {event_str}")

        elif cap == "temperatureMeasurement" and attr == "temperature":
            try:
                latest_temp = float(value)
                status = {
                    "timestamp":     event_str,
                    "label":         device["label"],
                    "location_name": device["location_name"],
                    "room_name":     device["room_name"],
                    "contact":       latest_contact or "closed",
                    "temperature":   latest_temp,
                    "battery":       latest_batt,
                    "type":          "door",
                }
                save_door_to_csv(status, device_id)
            except (ValueError, TypeError):
                pass

        elif cap == "battery" and attr == "battery":
            try:
                latest_batt = int(float(value))
                status = {
                    "timestamp":     event_str,
                    "label":         device["label"],
                    "location_name": device["location_name"],
                    "room_name":     device["room_name"],
                    "contact":       latest_contact or "closed",
                    "temperature":   latest_temp,
                    "battery":       latest_batt,
                    "type":          "door",
                }
                save_door_to_csv(status, device_id)
            except (ValueError, TypeError):
                pass

    if new_last_seen > after_ms:
        door_last_seen[device_id] = new_last_seen + 1

    return {
        "contact":       latest_contact or "closed",
        "temperature":   latest_temp,
        "battery":       latest_batt,
        "label":         device["label"],
        "location_name": device["location_name"],
        "room_name":     device["room_name"],
    }


async def fetch_camera_events(session):
    """Poll camera device history for motion/sound events only (no clip download)."""
    global camera_last_seen

    if not camera_metadata:
        return

    for cam in camera_metadata:
        cam_id = cam.get("id")
        cam_location_id = cam.get("location_id", "")
        cam_label = cam.get("label", "Camera")

        if not cam_location_id:
            cam_location_id = next((d.get("location_id", "") for d in device_metadata), "")

        after_ms = camera_last_seen.get(cam_id, 0)
        url = (f"{API_BASE_URL}/history/devices"
               f"?deviceId={cam_id}&locationId={cam_location_id}&limit=100")
        if after_ms:
            url += f"&oldestFirst=true&pagingAfterEpoch={after_ms}"

        data = await request_with_retry(session, url, context=f"Camera history ({cam_label})")
        if data is None:
            dashboard_state["camera"]["last_error"] = f"Failed to fetch camera history: {cam_label}"
            continue

        items = data.get("items", [])
        if not items:
            continue

        items.sort(key=lambda x: x.get("epoch", 0))
        new_last_seen = after_ms

        for item in items:
            if item.get("component") != "main":
                continue

            epoch = item.get("epoch", 0)
            cap   = item.get("capability", "")
            attr  = item.get("attribute", "")
            value = item.get("value", "")

            if epoch > new_last_seen:
                new_last_seen = epoch

            if not ((cap == "motionSensor" and attr == "motion") or (cap == "soundSensor" and attr == "sound")):
                continue

            event_dt  = datetime.fromtimestamp(epoch / 1000)
            event_str = event_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

            latest = camera_latest.get(cam_id) or {"motion": None, "sound": None, "ts": None}
            if cap == "motionSensor" and attr == "motion":
                save_camera_to_csv(event_str, cam_label, motion=value)
                latest["motion"] = value
            elif cap == "soundSensor" and attr == "sound":
                save_camera_to_csv(event_str, cam_label, sound=value)
                latest["sound"] = value
            latest["ts"] = event_str
            camera_latest[cam_id] = latest

            # Keep legacy summary (not required by dashboard after UI change)
            dashboard_state["camera"]["last_motion"] = latest.get("motion")
            dashboard_state["camera"]["last_sound"] = latest.get("sound")
            dashboard_state["camera"]["last_event_at"] = event_str
            dashboard_state["camera"]["last_event_label"] = cam_label
            dashboard_state["camera"]["last_error"] = None

        if new_last_seen > after_ms:
            camera_last_seen[cam_id] = new_last_seen + 1


async def collection_loop(session):
    """
    Single 60-second loop:
    - Motion / Door / Vacuum / Camera / SmartThings plugs (stplug; Shelly SMP* excluded)
    """
    global last_update_time

    while running:
        start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logging.info(f"================= Time: {start_time} =================")

        # Refresh device list every 10 minutes
        now = datetime.now()
        if now.minute % 10 == 0 and (
            last_update_time is None or last_update_time.minute != now.minute
        ):
            last_update_time = now
            await fetch_device_list(session)

        motion_devices  = [d for d in device_metadata if d["type"] == "motion"]
        door_devices    = [d for d in device_metadata if d["type"] == "door"]
        vacuum_devices  = [d for d in device_metadata if d["type"] == "vacuum"]
        plug_devices    = [d for d in device_metadata if d["type"] == "stplug"]

        motion_tasks    = [fetch_motion_history(session, d)  for d in motion_devices]
        door_tasks      = [fetch_door_history(session, d)    for d in door_devices]
        vacuum_tasks    = [fetch_vacuum_power(session, d)    for d in vacuum_devices]
        plug_tasks = [fetch_plug_status(session, d) for d in plug_devices]

        motion_results, door_results, vacuum_results, plug_results = await asyncio.gather(
            asyncio.gather(*motion_tasks, return_exceptions=True),
            asyncio.gather(*door_tasks, return_exceptions=True),
            asyncio.gather(*vacuum_tasks, return_exceptions=True),
            asyncio.gather(*plug_tasks, return_exceptions=True),
        )

        # ── Camera events (motion/sound only) ────────────────────
        await fetch_camera_events(session)

        # ── Update dashboard ─────────────────────────────────────
        device_rows   = []
        success_count = 0
        fail_count    = 0

        for result, device in zip(motion_results, motion_devices):
            if isinstance(result, Exception) or result is None:
                fail_count += 1
                device_rows.append({
                    "label":   device["label"],  "location": device["location_name"],
                    "room":    device["room_name"], "type": "motion",
                    "power":   "-", "energy": "-", "motion": "-",
                    "temp":    "-", "sound": "-",
                    "status":  "Fail", "updated": start_time,
                })
            else:
                success_count += 1
                device_rows.append({
                    "label":   device["label"],  "location": device["location_name"],
                    "room":    device["room_name"], "type": "motion",
                    "power":   "-", "energy": "-",
                    "motion":  result.get("motion", "-"),
                    "temp":    result["temperature"] if result.get("temperature") is not None else "-",
                    "sound": "-",
                    "status":  "OK", "updated": start_time,
                })

        for result, device in zip(door_results, door_devices):
            if isinstance(result, Exception) or result is None:
                fail_count += 1
                device_rows.append({
                    "label":   device["label"],  "location": device["location_name"],
                    "room":    device["room_name"], "type": "door",
                    "power":   "-", "energy": "-", "motion": "-",
                    "temp":    "-", "sound": "-",
                    "status":  "Fail", "updated": start_time,
                })
            else:
                success_count += 1
                device_rows.append({
                    "label":   device["label"],  "location": device["location_name"],
                    "room":    device["room_name"], "type": "door",
                    "power":   "-", "energy": "-",
                    "motion":  result.get("contact", "-"),  # reuse column for contact(open/closed)
                    "temp":    result["temperature"] if result.get("temperature") is not None else "-",
                    "sound": "-",
                    "status":  "OK", "updated": start_time,
                })

        for result, device in zip(vacuum_results, vacuum_devices):
            if isinstance(result, Exception) or result is None:
                fail_count += 1
                device_rows.append({
                    "label":   device["label"],  "location": device["location_name"],
                    "room":    device["room_name"], "type": "vacuum",
                    "power":   "-", "energy": "-", "motion": "-",
                    "temp":    "-", "sound": "-",
                    "status":  "Fail", "updated": start_time,
                })
            else:
                success_count += 1
                device_rows.append({
                    "label":   device["label"],  "location": device["location_name"],
                    "room":    device["room_name"], "type": "vacuum",
                    "power":   result.get("power", "-"),
                    "energy":  result.get("energy", "-"),
                    "motion":  result.get("cleaner_state", "-"),
                    "temp":    result.get("dustbin_state", "-"),
                    "sound":   result.get("dustbin_last_emptied", "-"),
                    "status":  "OK", "updated": start_time,
                })

        for result, device in zip(plug_results, plug_devices):
            if isinstance(result, Exception) or result is None:
                fail_count += 1
                device_rows.append({
                    "label":   device["label"],  "location": device["location_name"],
                    "room":    device["room_name"], "type": "stplug",
                    "power":   "-", "energy": "-", "motion": "-",
                    "temp":    "-", "sound": "-",
                    "status":  "Fail", "updated": start_time,
                })
            else:
                success_count += 1
                pw = result.get("power")
                en = result.get("energy")
                device_rows.append({
                    "label":   device["label"],  "location": device["location_name"],
                    "room":    device["room_name"], "type": "stplug",
                    "power":   "-" if pw is None else pw,
                    "energy":  "-" if en is None else en,
                    "motion":  result.get("switch", "-") or "-",
                    "temp":    "-", "sound": "-",
                    "status":  "OK", "updated": start_time,
                })

        # ── Cameras: show in table like other sensors ────────────
        for cam in camera_metadata:
            cam_id = cam.get("id")
            cam_label = cam.get("label", "Camera")
            latest = camera_latest.get(cam_id) or {}
            # Use motion column for motion state, sound column for sound state
            device_rows.append({
                "label":   cam_label,
                "location": cam.get("location_name", "-"),
                "room":    cam.get("room_name", "-") or "-",
                "type":    "camera",
                "power":   "-",
                "energy":  "-",
                "motion":  latest.get("motion", "-") or "-",
                "temp":    "-",
                "sound":   latest.get("sound", "-") or "-",
                "status":  "OK",
                "updated": latest.get("ts") or start_time,
            })

        dashboard_state["status"]     = "Collecting"
        dashboard_state["last_cycle"] = start_time
        dashboard_state["total"]      = len(device_metadata) + len(camera_metadata)
        dashboard_state["success"]    = success_count
        dashboard_state["fail"]       = fail_count
        dashboard_state["devices"]    = device_rows

        logging.info(f"================= Done: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} =================\n")
        await asyncio.sleep(DEVICE_STATUS_INTERVAL)


# ==============================
# Scheduler
# ==============================

async def scheduler():
    global running
    os.makedirs(CSV_BASE_DIR, exist_ok=True)

    while running:
        try:
            connector = aiohttp.TCPConnector(
                limit=20,
                ttl_dns_cache=300,
                enable_cleanup_closed=True
            )
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=SESSION_TIMEOUT
            ) as session:
                logging.info("Fetching latest device list...")
                await fetch_device_list(session)

                # Single unified 60s loop: plug status + motion/door/vacuum + camera clips
                await collection_loop(session)

        except RETRIABLE_EXCEPTIONS as e:
            logging.error(
                f"Session-level network error: {type(e).__name__}: {e}\n"
                f"{BASE_RETRY_WAIT}s, recreating session..."
            )
            await asyncio.sleep(BASE_RETRY_WAIT)

        except Exception as e:
            if not running:
                break
            logging.error(
                f"Scheduler error: {type(e).__name__}: {e}\n"
                f"{BASE_RETRY_WAIT}s, restarting..."
            )
            await asyncio.sleep(BASE_RETRY_WAIT)

    logging.info("Scheduler shut down safely.")


# ==============================
# Utilities
# ==============================

def print_device_list():
    if not device_metadata:
        logging.warning("Device list is empty.")
    else:
        logging.info("=== Current Device List ===")
        for idx, device in enumerate(device_metadata, start=1):
            logging.info(
                f"{idx}. [{device['type']}] {device['label']} | "
                f"Location={device['location_name']}, Room={device['room_name'] or '(none)'}"
            )
        logging.info("======================\n")


def shutdown_handler(signum, frame):
    global running
    logging.info("Shutdown signal received. Cleaning up...")
    running = False


def request_stop():
    """Called by the unified launcher to stop the scheduler loop cleanly."""
    global running
    running = False


# ==============================
# Main
# ==============================


if __name__ == "__main__":
    signal.signal(signal.SIGINT,  shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    logging.info("Starting SmartThings data collection.")

    if not load_token():
        logging.error("Please run smartthings_auth.py first.")
        exit(1)

    load_metadata()
    load_ban_list()
    print_device_list()

    try:
        logging.info("Starting data collection. Press Ctrl+C to stop.")
        asyncio.run(scheduler())
    finally:
        try:
            asyncio.run(asyncio.sleep(0))
        except RuntimeError:
            pass
        logging.info("Program terminated normally.")

