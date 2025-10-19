#!/usr/bin/env python3
"""
Raspberry Pi serial-to-AWS Postgres uploader for LoRa RX (Feather M0).

- Listens to the Feather M0 RX over USB serial. The RX firmware emits one JSON
  object per line and appends an "rssi_dbm" field. Lines starting with "#" are
  considered debug and ignored. For tolerance with older formats, lines like
  "... | {json}" are also accepted by extracting the JSON portion after the pipe.

- Translates each packet into the farm DB schema (see ai-context/schema.sql):
  - Upsert into sensor(hardware_id, name, sensor_type, gps_latitude, gps_longitude, metadata)
  - Insert into reading(sensor_id, ts, sequence, temperature_c, humidity_pct,
    capacitance_val, battery_v, rssi_dbm) with idempotency via unique(sensor_id, ts)

- Upload model: queue locally and, every TICK_SECONDS (default 90), check if the
  database is reachable; if yes, flush up to BATCH_SIZE readings in one transaction.
  This simple cadence assumes WiFi is available for ~5 minutes each hour and avoids
  clock-alignment assumptions.

Configuration (environment variables):
  - DATABASE_URL: Postgres connection URL (required)
  - SERIAL_PORT: Explicit serial path (e.g., /dev/ttyACM0). If unset, auto-detects
  - SERIAL_BAUD: Baud rate (default 115200)

  - DEFAULT_LAT: Default sensor latitude if not present in packet (float, optional)
  - DEFAULT_LON: Default sensor longitude if not present in packet (float, optional)
  - BATCH_SIZE: Max items to flush per cycle (default 5000)
  - TICK_SECONDS: Main loop tick interval (default 90)
  - CONNECT_TIMEOUT_S: DB connect timeout seconds for reachability checks (default 5)

  # BLE relay control (Raspberry Pi / Linux):
  - STARLINK_UPTIME_MINS: minutes ON (float; 0 disables)
  - STARLINK_DOWNTIME_MINS: minutes OFF (float; 0 disables)
  - DSD_DEVICE_MAC: Bluetooth MAC of the DSD TECH relay (required to enable BLE control)
  - DSD_PASSWORD: relay password (int or 0x hex). Default 1234.
Dependencies:
  pip install pyserial psycopg[binary]>=3.1 bleak
"""

import os
import sys
import time
import json
import glob
import logging
import signal
import threading
import asyncio
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Deque, Dict, List, Optional, Tuple
import binascii

try:
    import serial  # pyserial
except Exception as e:  # pragma: no cover
    print("pyserial is required: pip install pyserial", file=sys.stderr)
    raise

try:
    import psycopg
    from psycopg import sql
    try:
        from psycopg.extras import execute_values  # type: ignore
    except Exception:  # pragma: no cover
        execute_values = None  # Fallback to executemany
except Exception as e:  # pragma: no cover
    print("psycopg v3 is required: pip install psycopg[binary]", file=sys.stderr)
    raise


# ---------------------- Configuration ----------------------
DEFAULT_BAUD = int(os.getenv("SERIAL_BAUD", "115200"))
DEFAULT_LAT_ENV = os.getenv("DEFAULT_LAT")
DEFAULT_LON_ENV = os.getenv("DEFAULT_LON")
DEFAULT_LAT = float(DEFAULT_LAT_ENV) if DEFAULT_LAT_ENV else None
DEFAULT_LON = float(DEFAULT_LON_ENV) if DEFAULT_LON_ENV else None
DEFAULT_BATCH_SIZE = int(os.getenv("BATCH_SIZE", "5000"))
DEFAULT_TICK_SECONDS = float(os.getenv("TICK_SECONDS", "90"))
CONNECT_TIMEOUT_S = int(os.getenv("CONNECT_TIMEOUT_S", "5"))


# ---------------------- Logging ----------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("lora-aws")


# ---------------------- BLE Starlink relay (bare-bones) ----------------------
# Uses Bleak on Linux/BlueZ. Device is addressed by MAC on Linux.
# Service/Characteristic and frame format match dsdtech_switch_test.py (FFE0/FFE1).
# Immediate ON (0x01) and OFF (0x02) only.

try:
    from bleak import BleakClient, BleakScanner  # bleak is async BLE GATT client
except Exception:
    BleakClient = None
    BleakScanner = None

DSD_SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
DSD_CHAR_UUID    = "0000ffe1-0000-1000-8000-00805f9b34fb"

def _parse_pwd(env_val: Optional[str]) -> int:
    if not env_val:
        return 0x04D2  # 1234
    try:
        return int(env_val, 16) & 0xFFFF if env_val.lower().startswith("0x") else int(env_val) & 0xFFFF
    except Exception:
        return 0x04D2

def _frame(password: int, opcode: int, content: bytes) -> bytes:
    # [0xA1][pwd_hi][pwd_lo][opcode][content...][xor][0xAA]
    pkt = bytearray([0xA1, (password >> 8) & 0xFF, password & 0xFF, opcode])
    pkt.extend(content)
    x = 0
    for b in pkt:
        x ^= b
    pkt.extend([x, 0xAA])
    return bytes(pkt)

def _on_now(password: int, channel: int = 0x01) -> bytes:
    return _frame(password, 0x01, bytes([channel]))

def _off_now(password: int, channel: int = 0x01) -> bytes:
    return _frame(password, 0x02, bytes([channel]))

async def _bt_switch_loop(uptime_m: float, downtime_m: float, mac: str, password: int, stop_event: threading.Event) -> None:
    if BleakClient is None or BleakScanner is None:
        logger.warning("BLE: bleak not installed; Starlink relay control disabled")
        return
    if not mac:
        logger.warning("BLE: DSD_DEVICE_MAC is not set; Starlink relay control disabled")
        return
    # Allow fractional minutes (e.g., 10s = 0.1667)
    uptime_s = max(0.1, float(uptime_m) * 60.0)
    downtime_s = max(0.1, float(downtime_m) * 60.0)

    while not stop_event.is_set():
        try:
            dev = await BleakScanner.find_device_by_address(mac, timeout=15.0)
            if not dev:
                logger.info("BLE: relay %s not found; retry in 3s", mac)
                await asyncio.sleep(3.0)
                continue

            logger.info("BLE: connecting to DSD TECH at %s", mac)
            async with BleakClient(dev) as client:
                logger.info("BLE: connected; starting ON/OFF schedule")
                while not stop_event.is_set():
                    # ON phase
                    await client.write_gatt_char(DSD_CHAR_UUID, _on_now(password), response=True)
                    logger.info("BLE: relay ON for %.1f sec", uptime_s)
                    await asyncio.sleep(uptime_s)
                    if stop_event.is_set():
                        break
                    # OFF phase
                    await client.write_gatt_char(DSD_CHAR_UUID, _off_now(password), response=True)
                    logger.info("BLE: relay OFF for %.1f sec", downtime_s)
                    await asyncio.sleep(downtime_s)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("BLE: error/disconnect: %s; retry in 3s", e)
            await asyncio.sleep(3.0)

def start_bt_switcher(starlink_uptime_mins: float, starlink_downtime_mins: float, mac: Optional[str], password: int, stop_event: threading.Event) -> Optional[threading.Thread]:
    if starlink_uptime_mins <= 0 or starlink_downtime_mins <= 0 or not mac:
        logger.info("BLE: switcher disabled (check STARLINK_* mins and DSD_DEVICE_MAC)")
        return None

    logger.info(
        "BLE: starting switcher thread mac=%s up=%.3f min down=%.3f min",
        mac,
        starlink_uptime_mins,
        starlink_downtime_mins,
    )

    def _runner():
        asyncio.run(_bt_switch_loop(starlink_uptime_mins, starlink_downtime_mins, mac, password, stop_event))

    t = threading.Thread(target=_runner, name="starlink-ble", daemon=True)
    t.start()
    return t


# ---------------------- Serial helpers ----------------------
def find_serial_port(explicit_path: Optional[str]) -> str:
    if explicit_path and os.path.exists(explicit_path):
        return explicit_path
    # Common USB serial names on Linux/RPi
    patterns = [
        "/dev/ttyACM*",
        "/dev/ttyUSB*",
        "/dev/serial/by-id/*",
    ]
    for pat in patterns:
        matches = sorted(glob.glob(pat))
        if matches:
            return matches[0]
    raise FileNotFoundError(
        "No serial device found. Set SERIAL_PORT env or connect the Feather M0."
    )


def open_serial(port: str, baud: int) -> serial.Serial:
    logger.info("Opening serial port %s @ %d", port, baud)
    ser = serial.Serial(port=port, baudrate=baud, timeout=1)
    # Give the M0 a moment after open to reset (common on 32u4/SAMD)
    time.sleep(2.0)
    ser.reset_input_buffer()
    return ser


# ---------------------- Parser ----------------------
def _extract_json_text(line: str) -> Optional[str]:
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    # New format: pure JSON
    if text.startswith("{") and text.endswith("}"):
        return text
    # Tolerate older "... | {json}" format
    if "|" in text:
        try:
            part = text.split("|", 1)[1].strip()
            if part.startswith("{") and part.endswith("}"):
                return part
        except Exception:
            return None
    return None


def parse_line_to_packet(line: str) -> Optional[Dict]:
    json_text = _extract_json_text(line)
    if not json_text:
        return None
    try:
        obj = json.loads(json_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    return obj


# ---------------------- Identity & translation ----------------------
def stable_hardware_id(name_or_txid: str) -> int:
    # Deterministic: 1000 + (CRC32(name) & 0x7FFFFFFF)
    crc32 = binascii.crc32(name_or_txid.encode("utf-8")) & 0xFFFFFFFF
    return 1000 + (crc32 & 0x7FFFFFFF)


def translate_packet(pkt: Dict) -> Tuple[Dict, Dict]:
    """
    Produce DB-ready dicts (sensor, reading).
    - ts is set to current UTC at receipt time
    - Prefer GPS from packet if present; else use defaults
    - sensor.metadata includes source and net
    """
    name = pkt.get("name")
    sensor_id_fallback = pkt.get("sensor_id")
    if not isinstance(name, str) or not name:
        name = f"tx-{sensor_id_fallback if sensor_id_fallback is not None else 'unknown'}"

    hardware_id = stable_hardware_id(name)
    gps_lat = pkt.get("gps_lat")
    gps_long = pkt.get("gps_long")
    lat = gps_lat if isinstance(gps_lat, (int, float)) else DEFAULT_LAT
    lon = gps_long if isinstance(gps_long, (int, float)) else DEFAULT_LON

    # Use sensor_type from packet if provided; otherwise leave it empty (None)
    sensor_type_val = pkt.get("sensor_type") if isinstance(pkt.get("sensor_type"), str) else None

    sensor = {
        "hardware_id": hardware_id,
        "name": name,
        "sensor_type": sensor_type_val,
        "gps_latitude": lat,
        "gps_longitude": lon,
        "metadata": {
            "source": "radio",
            "net": pkt.get("net"),
        },
    }

    now_utc = datetime.now(timezone.utc)
    reading = {
        "sensor_id": hardware_id,
        "ts": now_utc,
        "sequence": pkt.get("sequence"),
        "temperature_c": pkt.get("temperature_c"),
        "humidity_pct": pkt.get("humidity_pct"),
        "capacitance_val": pkt.get("capacitance_val"),
        "battery_v": pkt.get("battery_v"),
        "rssi_dbm": pkt.get("rssi_dbm"),
    }

    return sensor, reading


# ---------------------- DB helpers ----------------------
def _dsn_with_timeout(db_url: str, timeout_s: int) -> str:
    if "?" in db_url:
        return f"{db_url}&connect_timeout={timeout_s}"
    return f"{db_url}?connect_timeout={timeout_s}"


def db_is_reachable(db_url: str, timeout_s: int = CONNECT_TIMEOUT_S) -> bool:
    try:
        dsn = _dsn_with_timeout(db_url, timeout_s)
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("select 1")
                cur.fetchone()
        return True
    except Exception as e:
        logger.debug("DB not reachable: %s", e)
        return False


def ensure_sensors(conn: psycopg.Connection, sensors: List[Dict]) -> None:
    """
    Bulk upsert sensor rows by hardware_id.

    - Deduplicates incoming sensor dicts by 'hardware_id' to avoid redundant work.
    - Uses a single bulk INSERT with ON CONFLICT (hardware_id) DO UPDATE to keep
      name, sensor_type, gps_latitude, gps_longitude, and metadata current.
    - Idempotent: safe to call for every batch prior to inserting readings.
    """
    if not sensors:
        return
    # Deduplicate by hardware_id (last one wins for name/type/coords/metadata)
    dedup: Dict[int, Dict] = {}
    for s in sensors:
        dedup[s["hardware_id"]] = s
    rows = [
        (
            s["hardware_id"],
            s["name"],
            s["sensor_type"],
            s.get("gps_latitude"),
            s.get("gps_longitude"),
            json.dumps(s.get("metadata") or {}),
        )
        for s in dedup.values()
    ]

    query = (
        "insert into sensor (hardware_id, name, sensor_type, gps_latitude, gps_longitude, metadata) "
        "values %s "
        "on conflict (hardware_id) do update set "
        "  name=excluded.name, sensor_type=excluded.sensor_type, "
        "  gps_latitude=excluded.gps_latitude, gps_longitude=excluded.gps_longitude, "
        "  metadata=excluded.metadata"
    )
    if execute_values:
        with conn.cursor() as cur:
            execute_values(cur, query, rows)
    else:  # Fallback: executemany
        values_sql = "(%s,%s,%s,%s,%s,%s)"
        with conn.cursor() as cur:
            cur.executemany(
                "insert into sensor (hardware_id, name, sensor_type, gps_latitude, gps_longitude, metadata) "
                f"values {values_sql} "
                "on conflict (hardware_id) do update set "
                "  name=excluded.name, sensor_type=excluded.sensor_type, "
                "  gps_latitude=excluded.gps_latitude, gps_longitude=excluded.gps_longitude, "
                "  metadata=excluded.metadata",
                rows,
            )


def insert_readings(conn: psycopg.Connection, readings: List[Dict]) -> None:
    if not readings:
        return
    rows = [
        (
            r["sensor_id"],
            r["ts"],
            r.get("sequence"),
            r.get("temperature_c"),
            r.get("humidity_pct"),
            r.get("capacitance_val"),
            r.get("battery_v"),
            r.get("rssi_dbm"),
        )
        for r in readings
    ]
    query = (
        "insert into reading (sensor_id, ts, sequence, temperature_c, humidity_pct, capacitance_val, battery_v, rssi_dbm) "
        "values %s on conflict (sensor_id, ts) do nothing"
    )
    if execute_values:
        with conn.cursor() as cur:
            execute_values(cur, query, rows)
    else:
        values_sql = "(%s,%s,%s,%s,%s,%s,%s,%s)"
        with conn.cursor() as cur:
            cur.executemany(
                "insert into reading (sensor_id, ts, sequence, temperature_c, humidity_pct, capacitance_val, battery_v, rssi_dbm) "
                f"values {values_sql} on conflict (sensor_id, ts) do nothing",
                rows,
            )


def flush_queue(db_url: str, items: List[Tuple[Dict, Dict]]) -> int:
    if not items:
        return 0
    sensors = [s for s, _ in items]
    readings = [r for _, r in items]
    dsn = _dsn_with_timeout(db_url, CONNECT_TIMEOUT_S)
    with psycopg.connect(dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute("set timezone to 'UTC'")
        ensure_sensors(conn, sensors)
        insert_readings(conn, readings)
        conn.commit()
    return len(items)


# ---------------------- Queue ----------------------
class InMemoryQueue:
    def __init__(self) -> None:
        self._q: Deque[Tuple[Dict, Dict]] = deque()
        self._lock = threading.Lock()

    def enqueue(self, item: Tuple[Dict, Dict]) -> None:
        with self._lock:
            self._q.append(item)

    def dequeue_batch(self, max_items: int) -> List[Tuple[Dict, Dict]]:
        out: List[Tuple[Dict, Dict]] = []
        with self._lock:
            while self._q and len(out) < max_items:
                out.append(self._q.popleft())
        return out

    def __len__(self) -> int:
        with self._lock:
            return len(self._q)


# ---------------------- Serial ingest thread ----------------------
def start_serial_reader(
    serial_path: str,
    baud: int,
    on_packet: Callable[[Dict], None],
    stop_event: threading.Event,
) -> threading.Thread:
    def _run() -> None:
        backoff_s = 1.0
        while not stop_event.is_set():
            try:
                with open_serial(serial_path, baud) as ser:
                    logger.info("Serial connected. Waiting for JSON lines…")
                    backoff_s = 1.0
                    while not stop_event.is_set():
                        try:
                            raw = ser.readline().decode(errors="replace")
                        except Exception as e:
                            logger.warning("Serial read error: %s", e)
                            break
                        if not raw:
                            continue
                        pkt = parse_line_to_packet(raw)
                        if not pkt:
                            continue
                        try:
                            on_packet(pkt)
                        except Exception as e:
                            logger.exception("on_packet failed: %s", e)
            except (serial.SerialException, OSError) as e:
                logger.warning("Serial error: %s; retrying in %.1fs", e, backoff_s)
                time.sleep(backoff_s)
                backoff_s = min(backoff_s * 2.0, 30.0)
            except Exception as e:
                logger.exception("Unexpected serial thread error: %s", e)
                time.sleep(2.0)

    t = threading.Thread(target=_run, name="serial-reader", daemon=True)
    t.start()
    return t


# ---------------------- Main ----------------------
def load_config() -> Dict:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL is required")
    cfg = {
        "db_url": db_url,
        "serial_port": os.getenv("SERIAL_PORT"),
        "baud": DEFAULT_BAUD,
        "batch_size": DEFAULT_BATCH_SIZE,
        "tick_seconds": DEFAULT_TICK_SECONDS,

        # BLE relay control
        "starlink_uptime_mins": float(os.getenv("STARLINK_UPTIME_MINS", "0")),
        "starlink_downtime_mins": float(os.getenv("STARLINK_DOWNTIME_MINS", "0")),
        "dsd_device_mac": os.getenv("DSD_DEVICE_MAC"),  # REQUIRED on Linux to enable
        "dsd_password": _parse_pwd(os.getenv("DSD_PASSWORD")),
    }
    return cfg


def main() -> int:
    try:
        cfg = load_config()
    except Exception as e:
        logger.error("Config error: %s", e)
        return 2

    try:
        serial_path = find_serial_port(cfg["serial_port"])
    except Exception as e:
        logger.error(str(e))
        return 3

    q = InMemoryQueue()
    stop_event = threading.Event()

    def on_packet(pkt: Dict) -> None:
        sensor, reading = translate_packet(pkt)
        q.enqueue((sensor, reading))
        try:
            logger.info(
                "Received datapoint from %s; queue_len=%d",
                sensor.get("name", "unknown"),
                len(q),
            )
        except Exception:
            pass

    # Signal handling for graceful exit
    def _handle_signal(signum, frame):  # type: ignore[no-untyped-def]
        logger.info("Signal %s received; shutting down…", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    start_serial_reader(serial_path, cfg["baud"], on_packet, stop_event)

    # Start BLE Starlink switcher (if enabled by env)
    try:
        logger.info(
            "BLE: config mac=%s up=%.3f min down=%.3f min",
            cfg["dsd_device_mac"],
            cfg["starlink_uptime_mins"],
            cfg["starlink_downtime_mins"],
        )
        start_bt_switcher(
            cfg["starlink_uptime_mins"],
            cfg["starlink_downtime_mins"],
            cfg["dsd_device_mac"],
            cfg["dsd_password"],
            stop_event,
        )
    except Exception as e:
        logger.warning("BLE: failed to start switcher: %s", e)

    logger.info(
        "Uploader started: batch_size=%s tick=%.1fs",
        cfg["batch_size"], cfg["tick_seconds"],
    )

    while not stop_event.is_set():
        try:
            logger.info("Connectivity check: testing reachability to AWS database…")
            connected = db_is_reachable(cfg["db_url"], CONNECT_TIMEOUT_S)
            logger.info(
                "Connectivity check result: %s",
                "connected" if connected else "disconnected",
            )
            if connected:
                items = q.dequeue_batch(cfg["batch_size"])
                if items:
                    try:
                        n = flush_queue(cfg["db_url"], items)
                        logger.info(
                            "Uploaded %d measurement datapoint(s) to the server; queue_len=%d",
                            n,
                            len(q),
                        )
                    except Exception as e:
                        logger.warning("Flush failed (%s); requeueing %d item(s)", e, len(items))
                        for it in reversed(items):
                            q.enqueue(it)
                        time.sleep(2.0)
                else:
                    logger.debug("Nothing to flush; queue_len=%d", len(q))
            time.sleep(cfg["tick_seconds"])
        except KeyboardInterrupt:
            stop_event.set()
        except Exception as e:
            logger.exception("Main loop error: %s", e)
            time.sleep(1.0)

    logger.info("Exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
