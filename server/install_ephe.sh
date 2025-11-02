#!/usr/bin/env bash
set -e

EPHE_DIR="src/jhora/data/ephe"
mkdir -p "$EPHE_DIR"

echo "Downloading ephe files..."
curl -L -o /tmp/pyjhora.zip https://github.com/naturalstupid/PyJHora/archive/refs/heads/main.zip
unzip -q /tmp/pyjhora.zip -d /tmp/pyjhora_src

cp -r /tmp/pyjhora_src/PyJHora-main/src/jhora/data/ephe/* "$EPHE_DIR" || true

echo "Ephemeris copied to $EPHE_DIR"
ls -la "$EPHE_DIR" | head -n 20 || true
