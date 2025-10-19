#!/usr/bin/env bash
# Environment for the Raspberry Pi uploader.
# Assumes a Porter datastore tunnel is running locally.

PORTER_TUNNEL_HOST="${PORTER_TUNNEL_HOST:-127.0.0.1}"
PORTER_TUNNEL_PORT="${PORTER_TUNNEL_PORT:-8122}"

# Postgres credentials injected by Porter (password as provisioned on your datastore).
export DATABASE_URL="postgresql://postgres:hZq4hbPWwOvuZCPp0dEr@${PORTER_TUNNEL_HOST}:${PORTER_TUNNEL_PORT}/postgres?sslmode=disable"

# Optional: LED notifier on successful DB flush
# Use BCM numbering (e.g., 18). Wire: GPIO -> resistor (330Ω) -> LED -> GND for active-high.
# LED notifier removed; keep variables unset
