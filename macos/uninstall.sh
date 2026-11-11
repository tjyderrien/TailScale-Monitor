#!/bin/sh
# Remove what the tailmon installer added:  sudo /usr/local/share/tailmon/uninstall.sh
# Your settings (~/.config/tailmon.json) are kept.
set -e
if [ "$(id -u)" != 0 ]; then
    exec sudo "$0" "$@"
fi
rm -f /usr/local/bin/tailmon /usr/local/share/man/man1/tailmon.1
if grep -q "Added by the tailmon installer" /usr/local/bin/tailscale 2>/dev/null; then
    rm -f /usr/local/bin/tailscale
fi
pkgutil --forget io.github.tjyderrien.tailmon >/dev/null 2>&1 || true
pkgutil --forget io.github.tjyderrien.tailmon.tailscale-cli >/dev/null 2>&1 || true
rm -rf /usr/local/share/tailmon
echo "tailmon removed."
