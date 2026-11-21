# PrintPilot deployment

## VPS

The dashboard listens only on the VPS Tailscale address, port 18790. Open
`http://100.67.67.24:18790` from a device connected to the same tailnet.
The manager token is in `/home/dev/.config/printpilot/server.json`; do not paste it
into URLs, source files or screenshots. The device token only permits ingest.
The router uses a dedicated PrintPilot API key. Configuration and SQLite data
are outside the Git checkout, with private directory permissions.

```sh
sh services/printpilot/run-server.sh
```

For reboot persistence, an administrator can install the supplied unit:

```sh
sudo install -m 644 services/printpilot/printpilot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now printpilot
```

Stop any foreground/test instance before enabling the system unit. The `dev`
account currently cannot use sudo; a running user process is not proof of VPS
reboot persistence. No existing server services need to be modified.

Set `webhook_url` in the private server config to a manager notification endpoint
that accepts JSON, then restart. Until configured, alerts appear in the dashboard
but external delivery is not enabled. Never infer printer health from `/health`:
it reports the server process only.

## Redmi 9C

Source: `~/printpilot/services/printpilot/bridge/`.
Config: `~/.config/printpilot/bridge.json` (mode 0600).
Service: `$PREFIX/var/service/printpilot-bridge`.

```sh
sv status "$PREFIX/var/service/printpilot-bridge"
tail -f ~/.config/printpilot/logs/current
sv restart "$PREFIX/var/service/printpilot-bridge"
```

The service reports an unknown printer state until IP/access code are configured.
Use [CLOUD.md](CLOUD.md) to obtain the code without disabling Bambu Handy.
Keep cloud mode enabled. Prefer pinning the printer TLS certificate fingerprint;
an explicit insecure local-camera setting is available but not enabled by the
deployed config. Do not disable TLS checks for the server API.

`~/.termux/boot/printpilot.sh` starts the existing Termux service supervisor and
requests a wake lock. Termux:Boot must be installed/enabled and Android must allow
Termux background operation. Device reboot and battery-management behavior still
need a real reboot test. No automatic printer pause/stop commands are sent.

## Native clients and updates

The dedicated `PrintPilot` workflow builds Windows x64 and macOS arm64 on manual
dispatch with `native=true`; artifacts expire after one day. macOS 27 runtime
compatibility is not established by a macOS 26 build. These are unsigned custom
builds; do not claim notarization or publisher signing.

See [STUDIO.md](STUDIO.md) for private client config and
[UPSTREAM.md](UPSTREAM.md) for upstream updates. A supported build passing CI
still needs manual smoke testing: load a 3MF, analyze, cancel, apply, slice again,
then review the send dialog. Never test by starting an unattended real print.
