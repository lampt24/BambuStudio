#!/bin/sh
# Run as root on the VPS. Makes https://3d.lampt.works serve the PrintPilot
# dashboard inside the tailnet. Does not touch public Cloudflare DNS records.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
install -m 644 "$here/printpilot.caddy" /etc/caddy-extra/printpilot.caddy
grep -q '^3d.lampt.works:53' /opt/9router/Corefile || cat "$here/Corefile.snippet" | grep -v '^#' >> /opt/9router/Corefile
cd /opt/9router && docker compose restart coredns caddy
echo "Done. Also add Tailscale split DNS: 3d.lampt.works -> 100.67.67.24"
