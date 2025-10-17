#!/usr/bin/env bash
# Environment for the Raspberry Pi uploader.
# Assumes a Porter datastore tunnel is running locally.

PORTER_TUNNEL_HOST="${PORTER_TUNNEL_HOST:-127.0.0.1}"
PORTER_TUNNEL_PORT="${PORTER_TUNNEL_PORT:-8122}"

# Postgres credentials injected by Porter (password as provisioned on your datastore).
export DATABASE_URL="postgresql://postgres:hZq4hbPWwOvuZCPp0dEr@${PORTER_TUNNEL_HOST}:${PORTER_TUNNEL_PORT}/postgres?sslmode=disable"
