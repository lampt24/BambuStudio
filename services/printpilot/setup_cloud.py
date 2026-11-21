#!/usr/bin/env python3
"""Configure an existing PrintPilot bridge from a Bambu Cloud device listing."""

import argparse
import getpass
import ipaddress
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

TIMEOUT = 15
MAX_RESPONSE = 1024 * 1024
ENDPOINTS = {
    "global": "https://api.bambulab.com",
    "china": "https://api.bambulab.cn",
}


class SetupError(Exception):
    """A safe-to-display setup error; never includes response bodies or tokens."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CloudClient:
    def __init__(self, region="global", opener=None):
        if region not in ENDPOINTS:
            raise SetupError("Unsupported Bambu Cloud region")
        self.base = ENDPOINTS[region]
        self.opener = opener or build_opener(NoRedirect())

    def request(self, path, method="GET", payload=None, token=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Bearer " + token
        req = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=TIMEOUT) as response:
                raw = response.read(MAX_RESPONSE + 1)
        except HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                raise SetupError("Bambu Cloud redirected the request; refusing to forward credentials") from None
            if exc.code in (400, 401, 403, 404, 429):
                raise SetupError("Bambu Cloud rejected the request; check the account, code, and region") from None
            raise SetupError("Bambu Cloud request failed") from None
        except (URLError, TimeoutError, OSError):
            raise SetupError("Could not connect to Bambu Cloud") from None
        except Exception:
            raise SetupError("Bambu Cloud request failed") from None
        if len(raw) > MAX_RESPONSE:
            raise SetupError("Bambu Cloud response exceeded the size limit")
        try:
            result = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SetupError("Bambu Cloud returned an unsupported response") from None
        if not isinstance(result, dict):
            raise SetupError("Bambu Cloud returned an unsupported response")
        return result

    def email_login(self, email, code_prompt=getpass.getpass):
        self.request("/v1/user-service/user/sendemail/code", "POST",
                     {"email": email, "type": "codeLogin"})
        try:
            code = code_prompt("Bambu email verification code: ").strip()
        except EOFError:
            raise SetupError("No verification code was entered") from None
        if not code:
            raise SetupError("Verification code cannot be empty")
        result = self.request("/v1/user-service/user/login", "POST",
                              {"account": email, "code": code})
        token = result.get("accessToken")
        if not isinstance(token, str) or not token.strip():
            raise SetupError("Email-code login was unsupported or requires additional verification")
        return token.strip()

    def devices(self, token):
        result = self.request("/v1/iot-service/api/user/bind", token=token)
        devices = result.get("devices")
        if not isinstance(devices, list):
            raise SetupError("Bambu Cloud device listing is unsupported")
        valid = []
        for item in devices:
            if (isinstance(item, dict) and isinstance(item.get("dev_id"), str)
                    and item["dev_id"].strip() and isinstance(item.get("name"), str)
                    and isinstance(item.get("dev_access_code"), str)
                    and item["dev_access_code"].strip()):
                valid.append(item)
        if not valid:
            raise SetupError("No supported printers with cloud access codes were found")
        return valid


def read_token(path):
    try:
        token = Path(path).expanduser().read_text(encoding="utf-8").strip()
    except OSError:
        raise SetupError("Could not read token file") from None
    if not token or "\n" in token or "\r" in token:
        raise SetupError("Token file must contain one non-empty token line")
    return token


def write_config(path, host, device):
    target = Path(path).expanduser()
    try:
        current = json.loads(target.read_text(encoding="utf-8"))
    except OSError:
        raise SetupError("Bridge config must already exist and be readable") from None
    except json.JSONDecodeError:
        raise SetupError("Bridge config is not valid JSON") from None
    if not isinstance(current, dict):
        raise SetupError("Bridge config must be a JSON object")
    current.update(printer_host=host, printer_id=device["dev_id"],
                   printer_access_code=device["dev_access_code"])
    encoded = (json.dumps(current, indent=2) + "\n").encode("utf-8")
    temp_path = None
    try:
        fd, name = tempfile.mkstemp(prefix="." + target.name + ".", dir=str(target.parent))
        temp_path = Path(name)
        with os.fdopen(fd, "wb") as out:
            os.fchmod(out.fileno(), 0o600)
            out.write(encoded)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp_path, target)
        temp_path = None
        os.chmod(target, 0o600)
    except OSError:
        raise SetupError("Could not securely update bridge config") from None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


def choose_device(devices, input_fn=input, output=sys.stdout):
    for index, device in enumerate(devices, 1):
        name = device["name"].replace(device["dev_access_code"], "[redacted]")
        print("{}. {} ({})".format(index, name, device["dev_id"]), file=output)
    try:
        selected = int(input_fn("Select printer number: "))
    except (ValueError, EOFError):
        raise SetupError("Invalid printer selection") from None
    if selected < 1 or selected > len(devices):
        raise SetupError("Invalid printer selection")
    return devices[selected - 1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(Path.home() / ".config/printpilot/bridge.json"))
    parser.add_argument("--printer-ip", required=True, help="Printer IP address on your LAN")
    auth = parser.add_mutually_exclusive_group(required=True)
    auth.add_argument("--email", help="Bambu account email; prompts for the emailed code")
    auth.add_argument("--token-file", help="Path to a file containing an existing Bambu Cloud token")
    parser.add_argument("--region", choices=ENDPOINTS, default="global")
    args = parser.parse_args(argv)
    try:
        host = str(ipaddress.ip_address(args.printer_ip))
        client = CloudClient(args.region)
        token = read_token(args.token_file) if args.token_file else client.email_login(args.email)
        device = choose_device(client.devices(token))
        write_config(args.config, host, device)
    except SetupError as exc:
        print("Cloud setup failed: " + str(exc), file=sys.stderr)
        return 1
    except ValueError:
        print("Cloud setup failed: printer IP must be a valid IPv4 or IPv6 address", file=sys.stderr)
        return 1
    print("Bridge config updated for selected printer; cloud token was not saved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
