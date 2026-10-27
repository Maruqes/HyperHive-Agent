# HyperHive Agent

A Linux telemetry agent for [HyperHive](https://github.com/Maruqes/HyperHive). It sends machine metrics, logs and events to a HyperHive Support App.

## Install

**Requirements:** Linux with systemd, Python 3, Git and `ip` (iproute2). Install `smartctl` (smartmontools) for disk health when available. Create a Support App in HyperHive and obtain its App ID and secret.

1. Clone the agent:

   ```sh
   git clone https://github.com/Maruqes/HyperHive-Agent.git
   cd HyperHive-Agent
   ```

2. Create the configuration file:

   ```sh
   sudo ./install.sh
   sudo nano /etc/hyperhive-agent/.env
   ```

   Set the exact Support ingest URL, App ID and secret. List important services and network interfaces separated by commas; leave interfaces empty to use default-route uplinks.

   ```dotenv
   SUPPORT_INGEST_URL=https://YOUR-HYPERHIVE-HOST/api/support/ingest
   SUPPORT_APP_ID=your-app-id
   SUPPORT_APP_SECRET=your-app-secret
   SUPPORT_SERVICES=sshd.service
   SUPPORT_NETWORK_INTERFACES=
   ```

3. Install and start the service:

   ```sh
   sudo ./install.sh
   sudo systemctl status hyperhive-agent.service
   ```

   View logs with `sudo journalctl -u hyperhive-agent.service -f`.

The installer keeps the configuration private and starts the service as root for journal and disk-health access. It preserves an existing configuration when rerun. The App represents one machine; no instance ID is sent.

## Update

```sh
git pull
sudo ./install.sh
```

## Uninstall

```sh
sudo ./uninstall.sh
```

This retains credentials and checkpoint state. To remove them too, run `sudo ./uninstall.sh --purge`.

The agent sends heartbeats and metrics every 15 seconds and snapshots approximately every 60 seconds. Journal checkpoints advance after successful delivery; retries may duplicate samples. Logs can contain sensitive information. Hardware capabilities may be unavailable, and large snapshots may be truncated. Live delivery requires a working Support endpoint and valid credentials.
