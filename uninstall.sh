#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo 'Run this uninstaller as root (for example: sudo ./uninstall.sh).' >&2
    exit 1
fi
if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != --purge ]; }; then
    echo 'Usage: sudo ./uninstall.sh [--purge]' >&2
    exit 1
fi
command -v systemctl >/dev/null || { echo 'systemctl is required.' >&2; exit 1; }
if [ -f /etc/systemd/system/hyperhive-agent.service ]; then
    systemctl disable --now hyperhive-agent.service
    rm -f /etc/systemd/system/hyperhive-agent.service
    systemctl daemon-reload
fi
rm -f /opt/hyperhive-agent/collector.py
rmdir /opt/hyperhive-agent 2>/dev/null || :
if [ "${1:-}" = --purge ]; then
    rm -f /etc/hyperhive-agent/.env /var/lib/hyperhive-agent/state.json /var/lib/hyperhive-agent/state.tmp
    rmdir /etc/hyperhive-agent /var/lib/hyperhive-agent 2>/dev/null || :
    echo 'HyperHive agent removed, including credentials and state.'
else
    echo 'HyperHive agent removed. Credentials and state were retained; use --purge to remove them.'
fi
