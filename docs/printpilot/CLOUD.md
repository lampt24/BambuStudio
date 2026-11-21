# Bambu Cloud setup

The A1's cloud device listing can provide its printer access code when the
printer's LAN-only setting hides that code in the app. This helper uses Bambu's
email-code login or a token file you already have, reads the account's bound
device list, and updates an existing PrintPilot bridge config.

```sh
python3 services/printpilot/setup_cloud.py \
  --email you@example.com \
  --printer-ip 192.168.1.50 \
  --config ~/.config/printpilot/bridge.json
```

The helper asks Bambu Cloud to send an email code, then prompts for that code
without echoing it. You still need to complete this user interaction. To reuse
an existing token, put only the token on one line in a local file and pass
`--token-file /path/to/token`; do not pass tokens as command-line arguments.
For an account served in China, add `--region china`.

The config file must already exist. The helper writes only `printer_host`,
`printer_id`, and `printer_access_code`, preserves other settings and tokens,
and atomically replaces the file with mode `0600`. The cloud token is used only
for the device-list request and is never saved or printed. Requests have a
timeout and response-size limit; redirects are rejected so a bearer token is
not forwarded to another host.

This uses Bambu's documented email-code flow and does not bypass 2FA, CAPTCHA,
or other verification. If Bambu requests additional verification or rejects
the request, complete the account verification through Bambu's own service and
retry with a supported login method. No printer commands or cloud mutations
are sent; only the bound device list is read.

LAN-only is not required to retrieve the cloud-listed access code. Cloud
availability does not guarantee local camera or bridge compatibility with a
particular A1 firmware. Verify bridge operation on the printer before relying
on it; firmware support has not been confirmed by this setup helper.
