"""
DSD TECH SH-BT01C BLE relay – macOS quick controller (Bleak)

What this does
----------------
- Discovers the single-channel DSD TECH BLE relay (advertises as "DSD TECH").
- Connects via CoreBluetooth on macOS using Bleak.
- Toggles the relay ON/OFF every 5 seconds for an audible click test.

Hardware and wiring (context)
-----------------------------
- Module terminals: 5V, 12V, GND (power from only one rail), and dry contacts NC/COM/NO.
- Typical high-side wiring: Source + → fuse → COM, NO → load +, Source – → load –. Leave NC unused.

BLE protocol essentials (from vendor docs)
------------------------------------------
- Service UUID: 0000FFE0-0000-1000-8000-00805F9B34FB
- Characteristic (write/notify): 0000FFE1-0000-1000-8000-00805F9B34FB
- Frame format to write to FFE1:
  [0xA1] [pwd_hi] [pwd_lo] [opcode] [content…] [xor] [0xAA]
  • Default password is 1234 → 0x04D2 (big-endian in-frame)
  • xor is the XOR of all bytes from 0xA1 up through the last content byte
- Opcodes used here:
  • 0x01 = ON now (content: channel 1 byte)
  • 0x02 = OFF now (content: channel 1 byte)
  • 0x03 = ON after N sec (content: channel + 3-byte seconds, big-endian)
  • 0x04 = OFF after N sec (content: channel + 3-byte seconds)

macOS specifics (CoreBluetooth via Bleak)
----------------------------------------
- macOS does not expose BLE MAC addresses; Bleak uses a CoreBluetooth UUID for the address.
- The CoreBluetooth UUID is per-machine and not a true hardware MAC. It is often stable across reboots on the same Mac, but is not guaranteed globally and may change if caches/reset occur.
- Discover by advertised name (e.g., "DSD TECH") and/or by the service UUID above.
- First use will prompt for Bluetooth permission. Approve under System Settings → Privacy & Security → Bluetooth if blocked.
- Only one central at a time: close the phone app while the Mac controls the relay.

How this script works (high-level)
----------------------------------
1) Scans using BleakScanner for a device whose name contains DEFAULT_DEVICE_NAME or advertises SERVICE_UUID.
2) Connects with BleakClient to that device.
3) Optionally enables notifications on FFE1 (helpful for observing ACKs, not strictly required).
4) Alternates sending ON-now and OFF-now frames every TOGGLE_PERIOD_SEC seconds.

Changing behavior (e.g., 5 min ON / 55 min OFF)
-----------------------------------------------
- This test uses immediate ON (0x01) and immediate OFF (0x02).
- To schedule inside the device per its one-shot timers, use 0x03/0x04 with a 3-byte seconds value, and re-arm as needed (hourly windows).

Running on macOS
----------------
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install bleak
python dsdtech_switch_test.py

Environment overrides:
- DSD_DEVICE_NAME: partial name to match (default: "DSD TECH").
- DSD_PASSWORD: integer (e.g., 1234) or hex (e.g., 0x04D2). Default: 1234.
- DSD_DEVICE_UUID: CoreBluetooth UUID string for this device on THIS Mac. When set, the script will prefer matching by this UUID.

Running on Raspberry Pi (Linux/BlueZ) – notes
---------------------------------------------
- Bleak uses the BlueZ backend on Linux; device addresses will be MACs (e.g., "AA:BB:CC:DD:EE:FF").
- Ensure Bluetooth is enabled and BlueZ is running; install system packages as needed (e.g., bluez, bluetooth).
- Script logic is identical; discovery will find by name/service. You can pass the name substring via argv or DSD_DEVICE_NAME.

Bleak documentation references
------------------------------
- Bleak home and API: https://bleak.readthedocs.io/
- Scanning: BleakScanner.discover(...) and BleakScanner.find_device_by_filter(...)
- Connecting/Writing: BleakClient(...), client.write_gatt_char(...), client.start_notify(...)
"""

import asyncio
import os
import signal
import sys
from typing import Optional

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError


SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

# Advertised name often contains "DSD TECH"; override via env or argv if needed
DEFAULT_DEVICE_NAME = os.environ.get("DSD_DEVICE_NAME", "DSD TECH")
# CoreBluetooth UUID observed on this Mac for the DSD TECH device (not a MAC). Override via env DSD_DEVICE_UUID.
DEFAULT_DEVICE_UUID = os.environ.get(
    "DSD_DEVICE_UUID",
    "BE940471-8C8F-325C-CDDC-87FF6EEDFD22",  # discovered in local scan
)

# Safe code/password (default 1234 => 0x04D2). Override with env DSD_PASSWORD (decimal or 0x hex)
def _parse_pwd(env_val: Optional[str]) -> int:
    if not env_val:
        return 0x04D2
    try:
        if env_val.lower().startswith("0x"):
            return int(env_val, 16) & 0xFFFF
        return int(env_val) & 0xFFFF
    except Exception:
        return 0x04D2


PWD = _parse_pwd(os.environ.get("DSD_PASSWORD"))
CHANNEL = 0x01  # Single-channel relay
TOGGLE_PERIOD_SEC = 5


def build_frame(opcode: int, content: bytes = b"") -> bytes:
    header = bytearray([0xA1, (PWD >> 8) & 0xFF, PWD & 0xFF, opcode])
    payload = header + bytearray(content)
    xor_acc = 0
    for b in payload:
        xor_acc ^= b
    payload += bytes([xor_acc, 0xAA])
    return bytes(payload)


def on_now_frame() -> bytes:
    return build_frame(0x01, bytes([CHANNEL]))


def off_now_frame() -> bytes:
    return build_frame(0x02, bytes([CHANNEL]))


async def find_device(target_name: str, timeout: float = 12.0, target_uuid: str | None = None):
    if target_uuid:
        print(f"[scan] Trying CoreBluetooth UUID match: {target_uuid}", flush=True)
        dev = await BleakScanner.find_device_by_address(target_uuid, timeout=timeout)
        if dev:
            return dev
        print("[scan] UUID match not found; falling back to name/service scan…", flush=True)
    print(f"[scan] Looking for device name contains '{target_name}' or service {SERVICE_UUID} (timeout={timeout}s)", flush=True)
    def _match(d, ad):
        if d.name and target_name in d.name:
            return True
        uuids = (ad.service_uuids or [])
        return any(u.lower() == SERVICE_UUID for u in uuids)

    return await BleakScanner.find_device_by_filter(_match, timeout=timeout)


def _hex(b: bytes) -> str:
    return b.hex()


async def toggle_loop(device_name: str, device_uuid: str | None = None):
    while True:
        dev = await find_device(device_name, target_uuid=device_uuid)
        if not dev:
            print("[scan] Relay not found. Ensure Bluetooth is ON and the phone app is disconnected. Retrying in 3s…", flush=True)
            await asyncio.sleep(3)
            continue
        try:
            async with BleakClient(dev) as client:
                print(f"[conn] Connected to device: name='{getattr(dev, 'name', None)}' address='{getattr(dev, 'address', None)}'", flush=True)
                try:
                    await client.start_notify(
                        CHAR_UUID, lambda _, d: print(f"[notify] {d.hex()}", flush=True)
                    )
                    print("[notify] Notifications enabled on FFE1", flush=True)
                except Exception as ne:
                    print(f"[notify] Could not enable notifications on FFE1: {ne}", flush=True)

                while True:
                    on_frame = on_now_frame()
                    await client.write_gatt_char(CHAR_UUID, on_frame, response=True)
                    print(f"[tx] ON frame: {_hex(on_frame)}  → Relay: ON", flush=True)
                    await asyncio.sleep(TOGGLE_PERIOD_SEC)
                    off_frame = off_now_frame()
                    await client.write_gatt_char(CHAR_UUID, off_frame, response=True)
                    print(f"[tx] OFF frame: {_hex(off_frame)} → Relay: OFF", flush=True)
                    await asyncio.sleep(TOGGLE_PERIOD_SEC)
        except asyncio.CancelledError:
            raise
        except BleakError as e:
            msg = str(e)
            if "turned off" in msg.lower():
                print("[error] macOS reports Bluetooth is OFF. Enable Bluetooth in System Settings and retry.", flush=True)
            else:
                print(f"[error] BLE error: {msg}", flush=True)
            print("[conn] Will retry in 3s…", flush=True)
            await asyncio.sleep(3)
        except Exception as e:
            print(f"[error] Disconnected or unexpected error: {e}", flush=True)
            print("[conn] Will retry in 3s…", flush=True)
            await asyncio.sleep(3)


def main():
    target_name = DEFAULT_DEVICE_NAME
    if len(sys.argv) > 1 and sys.argv[1]:
        target_name = sys.argv[1]

    print("=== DSD TECH BLE Relay Toggle Test ===", flush=True)
    print(f"device_name_match: '{target_name}'", flush=True)
    print(f"device_uuid_match: '{DEFAULT_DEVICE_UUID}'", flush=True)
    print(f"service_uuid:      {SERVICE_UUID}", flush=True)
    print(f"char_uuid:         {CHAR_UUID}", flush=True)
    print(f"channel:           {CHANNEL}", flush=True)
    print(f"password (hex):    0x{PWD:04X}", flush=True)
    print(f"toggle_period:     {TOGGLE_PERIOD_SEC}s", flush=True)
    print("Press Ctrl+C to stop. Ensure Bluetooth is ON and the phone app is disconnected.", flush=True)

    async def _runner():
        await toggle_loop(target_name, device_uuid=DEFAULT_DEVICE_UUID)

    try:
        asyncio.run(_runner())
    except KeyboardInterrupt:
        print("\n[exit] Interrupted by user.", flush=True)


if __name__ == "__main__":
    main()
