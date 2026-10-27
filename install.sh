#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo 'Run this installer as root (for example: sudo ./install.sh).' >&2
    exit 1
fi

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
command -v python3 >/dev/null || { echo 'Python 3 is required.' >&2; exit 1; }
command -v systemctl >/dev/null || { echo 'systemd is required.' >&2; exit 1; }
[ -d /run/systemd/system ] || { echo 'This machine is not running systemd.' >&2; exit 1; }

install -d -m 755 /opt/hyperhive-agent
install -d -m 700 /etc/hyperhive-agent
if [ ! -f /etc/hyperhive-agent/.env ]; then
    if [ -f "$source_dir/.env" ]; then
        install -m 600 "$source_dir/.env" /etc/hyperhive-agent/.env
    else
        install -m 600 "$source_dir/.env.example" /etc/hyperhive-agent/.env
        echo 'Edit /etc/hyperhive-agent/.env with your Support credentials, then run this installer again.'
        exit 0
    fi
fi
chmod 600 /etc/hyperhive-agent/.env
chown root:root /etc/hyperhive-agent/.env
python3 - "$source_dir" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from collector import load_env
try:
    config = load_env('/etc/hyperhive-agent/.env')
    if config['SUPPORT_APP_ID'] == 'your-app-id' or config['SUPPORT_APP_SECRET'] == 'your-app-secret':
        raise ValueError('replace the example credentials')
except (OSError, ValueError) as error:
    sys.exit('Invalid configuration: ' + str(error))
PY
if [ "$source_dir" != /opt/hyperhive-agent ]; then
    install -m 755 "$source_dir/collector.py" /opt/hyperhive-agent/collector.py
fi
install -m 644 "$source_dir/hyperhive-agent.service" /etc/systemd/system/hyperhive-agent.service
systemctl daemon-reload
systemctl enable hyperhive-agent.service
systemctl restart hyperhive-agent.service
echo 'HyperHive agent installed. Check: systemctl status hyperhive-agent.service'
