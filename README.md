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

   Set the exact Support ingest URL, App ID and secret. Leave services empty to monitor all loaded systemd services, or list important services separated by commas. Leave interfaces empty to monitor all interfaces, or list interfaces separated by commas. Aggregate traffic uses default-route uplinks when the list is empty, avoiding duplicate bridge and virtual-interface traffic.

   ```dotenv
   SUPPORT_INGEST_URL=https://YOUR-HYPERHIVE-HOST/api/support/ingest
   SUPPORT_APP_ID=your-app-id
   SUPPORT_APP_SECRET=your-app-secret
   SUPPORT_TLS_VERIFY=true
   SUPPORT_SERVICES=
   SUPPORT_NETWORK_INTERFACES=
   ```

   Both HTTP and HTTPS URLs are supported. For HTTPS with an untrusted or self-signed certificate, set `SUPPORT_TLS_VERIFY=false`. This disables certificate and hostname verification, so use it only on a trusted network. HTTP also sends the App secret without encryption.

3. Install and start the service:

   ```sh
   sudo ./install.sh
   sudo systemctl status hyperhive-agent.service
   ```

   View logs with `sudo journalctl -u hyperhive-agent.service -f`.

## Health reporting

The agent sends an explicit health status and diagnostic message with each batch:

- **Unhealthy:** a service has failed or has an invalid unit definition; an explicitly configured service is missing, masked, stopped or stopping; a disk explicitly fails its SMART health check; or a writable filesystem has at least 98% of its space or inodes unavailable.
- **Degraded:** a service is waiting for an automatic restart, a service state is unknown, a writable filesystem reaches 90% space or inode use, a collection check fails, or initial health checks are still pending.
- **Healthy:** no issue is currently detected by the available checks.

Successful inactive `Type=oneshot` jobs are allowed. Unconfigured inactive, masked or missing units do not trigger failures. Service names accept both `nginx` and `nginx.service`, and aliases remain explicitly monitored. Service failure and recovery events use the same rules as health. Messages include the affected service, state, result and exit code, or the affected disk/filesystem. The most severe issues come first.

Filesystem space checks use the capacity unavailable to an unprivileged writer, including reserved blocks; read-only mounts are excluded. CPU and memory utilization are telemetry only and have no health thresholds. Missing SMART support or unsupported disks do not themselves mean hardware failure. If a collection check fails, its last observed health issues remain until a successful check replaces them, and the collection error is also reported.

Health follows the approximately 60-second snapshot cadence, with earlier service checks triggered by systemd journal events. Delivery runs every 15 seconds or when data is queued. Service snapshots can be truncated on machines with many services, but health is evaluated before truncation. Lost heartbeats make the Support App offline independently of its last reported health.

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
