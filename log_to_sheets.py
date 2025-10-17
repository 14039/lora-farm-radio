#!/usr/bin/env python3

# THIS FILE IS DEPRECATED. FOR REFERNCE ONLY. SEE log_to_aws.py FOR THE CURRENT VERSION.

"""
Raspberry Pi serial-to-Google Sheets uploader for LoRa RX (Feather M0).

- Opens the Feather M0 serial port and reads one line per packet.
  The RX firmware should emit a single JSON object per line.
  This script is also tolerant of the older RX log format that included
  a human-readable prefix and the JSON payload after a pipe ("|").

- Appends each parsed record to the target Google Sheet using a
  service account from the provided JSON key file.

Configuration (environment variables with sensible defaults):
  - SERIAL_PORT: Explicit serial path (e.g., /dev/ttyACM0). If unset, auto-detects.
  - SERIAL_BAUD: Baud rate (default 115200)
  - GSPREAD_SHEET_ID: Spreadsheet ID from the URL
  - GSPREAD_GID: Sheet gid (tab) from the URL (default 0)
  - GOOGLE_APPLICATION_CREDENTIALS: Path to service account key JSON

Dependencies:
  pip install pyserial google-api-python-client google-auth google-auth-httplib2
"""

import os
import sys
import time
import json
import glob
import logging
from typing import Optional, Tuple

try:
    import serial  # pyserial
except Exception as e:  # pragma: no cover
    print("pyserial is required: pip install pyserial", file=sys.stderr)
    raise

try:
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from google.oauth2.service_account import Credentials
except Exception as e:  # pragma: no cover
    print(
        "Google API client libraries are required: pip install google-api-python-client google-auth google-auth-httplib2",
        file=sys.stderr,
    )
    raise


# ---------------------- Configuration ----------------------
DEFAULT_BAUD = int(os.getenv("SERIAL_BAUD", "115200"))
DEFAULT_SHEET_ID = os.getenv(
    "GSPREAD_SHEET_ID", "1cylhStNh4lJ1DeXBaWiGCLXtVj-kF_PwUBJcI4kv6pA"
)
DEFAULT_GID = int(os.getenv("GSPREAD_GID", "0"))

# Attempt to infer credentials path from env or local repo
DEFAULT_CREDS_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
if not DEFAULT_CREDS_PATH:
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = sorted(glob.glob(os.path.join(here, "raspberrypilogger-*.json")))
    DEFAULT_CREDS_PATH = candidates[-1] if candidates else None


# ---------------------- Logging ----------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("lora-sheets")


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


# ---------------------- Sheets helpers ----------------------
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class SheetsAppender:
    def __init__(self, creds_path: str, spreadsheet_id: str, gid: int):
        if not creds_path or not os.path.exists(creds_path):
            raise FileNotFoundError(
                f"Service account key not found at {creds_path!r}. Set GOOGLE_APPLICATION_CREDENTIALS."
            )
        self.spreadsheet_id = spreadsheet_id
        self.creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
        self.service = build("sheets", "v4", credentials=self.creds, cache_discovery=False)
        self.sheet_title = self._resolve_sheet_title(spreadsheet_id, gid) or "Sheet1"
        logger.info("Resolved sheet title: %s", self.sheet_title)

    def _resolve_sheet_title(self, spreadsheet_id: str, gid: int) -> Optional[str]:
        try:
            meta = (
                self.service.spreadsheets()
                .get(spreadsheetId=spreadsheet_id, fields="sheets(properties(sheetId,title))")
                .execute()
            )
            for sheet in meta.get("sheets", []):
                props = sheet.get("properties", {})
                if props.get("sheetId") == gid:
                    return props.get("title")
        except HttpError as e:
            logger.warning("Failed to resolve sheet title by gid=%s: %s", gid, e)
        return None

    def append_row(self, row_values: list) -> None:
        body = {"values": [row_values]}
        rng = f"{self.sheet_title}!A:Z"
        self.service.spreadsheets().values().append(
            spreadsheetId=self.spreadsheet_id,
            range=rng,
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body=body,
        ).execute()


# ---------------------- Parsing ----------------------
def parse_line_to_record(line: str) -> Optional[dict]:
    """
    Accepts either:
      1) Pure JSON: { ... }
      2) Old RX log: "ok seq=.. rssi=.. id=.. | {json}"
    Returns a dict with normalized fields or None if unparseable.
    """
    text = line.strip()
    if not text:
        return None

    payload_obj = None
    meta = {}

    if text.startswith("{"):
        try:
            payload_obj = json.loads(text)
        except json.JSONDecodeError:
            return None
    else:
        # Try to split on pipe and load the JSON part
        if "|" in text:
            try:
                json_part = text.split("|", 1)[1].strip()
                payload_obj = json.loads(json_part)
            except Exception:
                return None
        else:
            return None

        # Best-effort scrape of rssi and id from the prefix if present
        # Example prefix: "ok seq=69 from=0x1 rssi=-115 ... id=69"
        try:
            prefix = text.split("|", 1)[0]
            # crude parses; ignore if not present
            if "rssi=" in prefix:
                rssi_str = prefix.split("rssi=", 1)[1].split()[0]
                meta["rssi"] = int(rssi_str)
            if "id=" in prefix:
                id_str = prefix.split("id=", 1)[1].split()[0]
                meta["id"] = int(id_str)
        except Exception:
            pass

    if not isinstance(payload_obj, dict):
        return None

    # Normalize fields
    rec = {
        "net": payload_obj.get("net"),
        "node": payload_obj.get("node"),
        "seq": payload_obj.get("seq"),
        "ts": payload_obj.get("ts"),
        "vbat": payload_obj.get("vbat"),
        "t": payload_obj.get("t"),
        "rh": payload_obj.get("rh"),
        # Prefer rssi/id from payload if present (new RX), else use meta
        "rssi": payload_obj.get("rssi", meta.get("rssi")),
        "id": payload_obj.get("id", meta.get("id")),
        "from": payload_obj.get("from"),
        "to": payload_obj.get("to"),
    }
    return rec


def record_to_row(now_utc_iso: str, rec: dict, raw_json: str) -> list:
    return [
        now_utc_iso,
        rec.get("net"),
        rec.get("node"),
        rec.get("seq"),
        rec.get("ts"),
        rec.get("vbat"),
        rec.get("t"),
        rec.get("rh"),
        rec.get("rssi"),
        rec.get("id"),
        rec.get("from"),
        rec.get("to"),
        raw_json,
    ]


# ---------------------- Main loop ----------------------
def main() -> int:
    serial_port = os.getenv("SERIAL_PORT")
    try:
        serial_path = find_serial_port(serial_port)
    except Exception as e:
        logger.error(str(e))
        return 2

    try:
        sheets = SheetsAppender(
            creds_path=DEFAULT_CREDS_PATH,
            spreadsheet_id=DEFAULT_SHEET_ID,
            gid=DEFAULT_GID,
        )
    except Exception as e:
        logger.error("Failed to initialize Google Sheets client: %s", e)
        return 3

    backoff_s = 1.0
    while True:
        try:
            with open_serial(serial_path, DEFAULT_BAUD) as ser:
                logger.info("Serial connected. Waiting for JSON lines…")
                while True:
                    line = ser.readline().decode(errors="replace")
                    if not line:
                        continue
                    rec = parse_line_to_record(line)
                    if not rec:
                        continue
                    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    try:
                        # Ensure we keep exactly what we parsed for auditability
                        raw_json = json.dumps(rec, separators=(",", ":"))
                        row = record_to_row(now_iso, rec, raw_json)
                        sheets.append_row(row)
                        logger.info(
                            "Appended seq=%s node=%s rssi=%s",
                            rec.get("seq"),
                            rec.get("node"),
                            rec.get("rssi"),
                        )
                        backoff_s = 1.0  # reset on success
                    except HttpError as e:
                        logger.warning("Sheets append failed: %s", e)
                        time.sleep(backoff_s)
                        backoff_s = min(backoff_s * 2.0, 60.0)
        except (serial.SerialException, OSError) as e:
            logger.warning("Serial error: %s; retrying in %.1fs", e, backoff_s)
            time.sleep(backoff_s)
            backoff_s = min(backoff_s * 2.0, 30.0)
        except KeyboardInterrupt:
            logger.info("Interrupted. Exiting.")
            return 0
        except Exception as e:
            logger.exception("Unexpected error in main loop: %s", e)
            time.sleep(2.0)


if __name__ == "__main__":
    sys.exit(main())


