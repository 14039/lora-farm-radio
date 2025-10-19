#!/usr/bin/env bash
# Environment for the Raspberry Pi uploader.
# Assumes a Porter datastore tunnel is running locally.

PORTER_TUNNEL_HOST="${PORTER_TUNNEL_HOST:-127.0.0.1}"
PORTER_TUNNEL_PORT="${PORTER_TUNNEL_PORT:-8122}"

# Postgres credentials injected by Porter (password as provisioned on your datastore).
export DATABASE_URL="postgresql://postgres:hZq4hbPWwOvuZCPp0dEr@${PORTER_TUNNEL_HOST}:${PORTER_TUNNEL_PORT}/postgres?sslmode=disable"

# BLE Starlink relay control (Linux/BlueZ via Bleak)
# Set MAC from bluetoothctl scan output. 4s ON / 10s OFF test schedule by default.
# --- BLE Starlink relay (DSD TECH) ---
# Set this to the MAC you see from: bluetoothctl -> scan on
export DSD_DEVICE_MAC="C8:47:80:59:E1:27"
# 1234 is the default device password
export DSD_PASSWORD="1234"
# Schedule in minutes (decimals allowed). 10s = 0.1667, 4s = 0.0667
export STARLINK_UPTIME_MINS="45"   # 45 min OFF
export STARLINK_DOWNTIME_MINS="5" # 5 min ON. -NOTE THESE ARE BACKWARADS DUE TO HARDWARE - DO NOT CHANGE FOR NOW

# Optional: LED notifier on successful DB flush (feature removed; keep unset)
