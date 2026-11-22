#!/usr/bin/env python3
"""Import TINMORRY's official filament presets into the BBL system profiles.

Sources (clone them first, pass the folders as arguments):
  https://github.com/TINMORRY/TINMORRY-filament-profile-for-Bambu-printers  (Studio user presets, *.json / *.bbsflmt)
  https://github.com/TINMORRY/Tinmorry-Bambu_BambuStudio                     (filament bundles, *.bbsflmt)

Usage:
  python3 scripts/printpilot/import_tinmorry.py <bambu-printers-repo> <bambustudio-repo>

What it does, so the result loads as ordinary system presets:
  * names become "TINMORRY <material> @BBL <printer>" (TINMORRY's file names are inconsistent);
  * user-preset diffs keep TINMORRY's values on top of the Bambu base they were made from;
  * full bundle presets are attached to the matching Generic base for that printer, so keys
    TINMORRY did not set fall back to Bambu's values;
  * user-only ids ("P1234567") are dropped: filament_id is inherited from the Bambu base, so the
    printer/AMS still recognise the material; each preset gets a stable unique setting_id;
  * keys this Studio version does not know are dropped and listed in the manifest;
  * files go to resources/profiles/BBL/filament/TINMORRY/, BBL.json gets list entries appended
    (previous TINMORRY entries are replaced) and its version is raised so installs reload.
Re-running is idempotent.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PROFILES = os.path.join(ROOT, "resources", "profiles")
BBL_JSON = os.path.join(PROFILES, "BBL.json")
FILAMENT_DIR = os.path.join(PROFILES, "BBL", "filament")
OUT_DIR = os.path.join(FILAMENT_DIR, "TINMORRY")
MANIFEST = os.path.join(os.path.dirname(__file__), "tinmorry_manifest.json")
VENDOR = "TINMORRY"
# Above every 02.09.00.xx online package: Bambu's updater only replaces the BBL folder with a
# newer version of the same major.minor, which would drop these presets again.
MIN_VERSION = "02.09.01.00"

STRIP = {"from", "name", "inherits", "filament_settings_id", "version", "is_custom_defined",
         "filament_id", "setting_id", "instantiation", "type", "filament_vendor", "compatible_prints",
         "compatible_prints_condition", "compatible_printers_condition", "base_id", "user_id", "updated_time"}
UPPER = {"PLA", "PETG", "PET", "TPU", "ABS", "ASA", "PA", "PAHT", "PC", "PP", "CF", "GF", "HS", "ECO", "95A"}
POLYMERS = ["PETG", "PET", "PLA", "TPU", "ABS", "ASA", "PAHT", "PA", "PC", "PP"]
FIBERS = ["CF", "GF"]
TYPOS = {"Melallic": "Metallic"}
# Machine model -> code used in Bambu's filament preset names ("Generic PETG @BBL A1M").
MODEL_CODE = {"Bambu Lab A1": "A1", "Bambu Lab A1 mini": "A1M", "Bambu Lab X1 Carbon": "X1C", "Bambu Lab X1": "X1",
              "Bambu Lab X1E": "X1E", "Bambu Lab P1S": "P1S", "Bambu Lab P1P": "P1P", "Bambu Lab P2S": "P2S",
              "Bambu Lab H2C": "H2C", "Bambu Lab H2D": "H2D", "Bambu Lab H2D Pro": "H2DP", "Bambu Lab H2S": "H2S",
              "Bambu Lab A2L": "A2L", "Bambu Lab X2D": "X2D", "Bambu Lab H3C": "H3C"}


def material_name(raw: str) -> str:
    text = raw.replace("（", "(").replace("）", ")").replace("，", " ").replace("`", " ")
    text = re.sub(r"\(.*$", "", text)                     # "(A1-Bambu-TINMORRY)" and anything after
    text = re.sub(r"@.*$", "", text)                      # "@Bambu Lab A1 0.4 nozzle"
    text = re.sub(r"(?i)\b(tinmorry|bambu)\b", " ", text)
    text = re.sub(r"(?i)\b(TPU)(\d{2}A)\b", r"\1 \2", text)  # "TPU95A"
    tokens = [t for t in re.split(r"[\s\-_]+", text) if t]
    tokens = [t.upper() if t.upper() in UPPER else TYPOS.get(t.capitalize(), t.capitalize()) for t in tokens]
    polymer = next((p for p in POLYMERS if p in tokens), None)
    if polymer is None:
        raise ValueError(f"no polymer in {raw!r}")
    fibers = [f for f in FIBERS if f in tokens]
    rest = [t for t in tokens if t != polymer and t not in fibers]
    head = polymer + ("-" + "/".join(fibers) if fibers else "")
    if polymer == "TPU" and not rest and not fibers:
        rest = ["95A"]  # TINMORRY's TPU is 95A; the A1 files just call it "TPU"
    return " ".join([head] + rest)


def code_of(compatible: list[str]) -> str | None:
    for printer in compatible:
        model = re.sub(r" \d+(?:\.\d+)? nozzle$", "", printer)
        if model in MODEL_CODE:
            return MODEL_CODE[model]
    return None


def code_from_base(base: str) -> str:
    found = re.search(r"@BBL (\S+)", base)
    return found.group(1) if found else "X1C"  # un-suffixed Bambu bases target X1/P1S


def git_head(path: str) -> str:
    try:
        return subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def load_sources(user_repo: str, bundle_repo: str):
    """Yield (source, label, preset dict) for every TINMORRY preset found."""
    for path in sorted(glob.glob(os.path.join(user_repo, "*.json"))):
        with open(path, encoding="utf-8-sig") as fh:
            yield "user", os.path.basename(path), json.load(fh)
    bundles = sorted(glob.glob(os.path.join(bundle_repo, "**", "*.bbsflmt"), recursive=True)) + \
              sorted(glob.glob(os.path.join(user_repo, "*.bbsflmt")))
    for path in bundles:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.endswith(".json") and not name.endswith("bundle_structure.json"):
                    yield "bundle", os.path.relpath(path, os.path.dirname(path.rsplit("/", 2)[0])) + ":" + name, json.loads(zf.read(name))


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    user_repo, bundle_repo = argv[1], argv[2]
    with open(BBL_JSON, encoding="utf-8") as fh:
        bbl_text = fh.read()
    bbl = json.loads(bbl_text)
    base_names = {e["name"]: e["sub_path"] for e in bbl["filament_list"] if not e["sub_path"].startswith("filament/TINMORRY/")}
    # Every option this Studio version defines; unknown keys would only be ignored with a warning.
    with open(os.path.join(ROOT, "src", "libslic3r", "PrintConfig.cpp"), encoding="utf-8") as fh:
        source = fh.read()
    known_keys: set[str] = set(re.findall(r'this->add(?:_nullable)?\("([A-Za-z0-9_]+)"', source))
    for generated in ("filament_extruder_override_keys", "filament_overhang_override_keys"):
        block = re.search(generated + r" = \{(.*?)\};", source, re.S)
        known_keys.update(re.findall(r'"(filament_[a-z0-9_]+)"', block.group(1)) if block else [])
    other_ids: set[str] = set()
    for path in glob.glob(os.path.join(FILAMENT_DIR, "**", "*.json"), recursive=True):
        if os.sep + "TINMORRY" + os.sep in path:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except ValueError:
            continue
        if isinstance(data, dict):
            if isinstance(data.get("setting_id"), str):
                other_ids.add(data["setting_id"])

    presets: dict[str, dict] = {}
    report = {"imported": [], "skipped": [], "dropped_keys": {}}
    for source, label, data in load_sources(user_repo, bundle_repo):
        try:
            material = material_name(data.get("name") if source == "bundle" else label)
        except ValueError as exc:
            report["skipped"].append({"source": label, "reason": str(exc)})
            continue
        if source == "user":
            base = data.get("inherits", "")
            code = code_from_base(base)
            compatible = data.get("compatible_printers")
        else:
            compatible = data.get("compatible_printers") or []
            code = code_of(compatible)
            polymer = material.split()[0].split("-")[0]
            fiber = "-CF" if "CF" in material.split()[0] else ""
            candidates = [f"Generic {polymer}{fiber} @BBL {code}", f"Generic {polymer} @BBL {code}",
                          f"Generic {polymer}{fiber} @BBL {code} 0.4 nozzle", f"Generic {polymer} @BBL {code} 0.4 nozzle",
                          f"Generic {polymer}{fiber}", f"Generic {polymer}"]
            base = next((c for c in candidates if c in base_names), "")
        if not code or base not in base_names:
            report["skipped"].append({"source": label, "reason": f"no Bambu base for {base or material} / {compatible}"})
            continue
        name = f"{VENDOR} {material} @BBL {code}"
        if name in presets and presets[name]["source"] == "bundle" and source == "user":
            report["skipped"].append({"source": label, "reason": f"duplicate of bundle preset {name}"})
            continue
        if name in presets and presets[name]["source"] == source:
            report["skipped"].append({"source": label, "reason": f"duplicate name {name}"})
            continue
        dropped = sorted(k for k in data if k not in STRIP and k not in known_keys)
        if dropped:
            report["dropped_keys"][name] = dropped
        body = {k: v for k, v in data.items() if k not in STRIP and k in known_keys}
        if compatible:
            body["compatible_printers"] = compatible
        presets[name] = {"source": source, "label": label, "base": base, "body": body}

    os.makedirs(OUT_DIR, exist_ok=True)
    for old in glob.glob(os.path.join(OUT_DIR, "*.json")):
        os.remove(old)
    entries = []
    used_ids = set(other_ids)
    for name in sorted(presets):
        item = presets[name]
        digest = hashlib.md5(name.encode()).hexdigest().upper()
        setting_id = "TMS" + digest[:5]
        if setting_id in used_ids:
            raise SystemExit(f"setting_id collision for {name}")
        used_ids.add(setting_id)
        preset = {"type": "filament", "name": name, "inherits": item["base"], "from": "system",
                  "setting_id": setting_id, "instantiation": "true", "filament_vendor": [VENDOR],
                  **item["body"]}
        file_name = re.sub(r'[\\/:*?"<>|]', "_", name) + ".json"
        with open(os.path.join(OUT_DIR, file_name), "w", encoding="utf-8") as fh:
            json.dump(preset, fh, indent=4, ensure_ascii=False)
            fh.write("\n")
        entries.append({"name": name, "sub_path": f"filament/TINMORRY/{file_name}"})
        report["imported"].append({"name": name, "base": item["base"], "setting_id": setting_id, "source": item["label"]})

    bbl_text = replace_filament_entries(bbl_text, entries)
    current = bbl["version"]
    if tuple(int(x) for x in current.split(".")) < tuple(int(x) for x in MIN_VERSION.split(".")):
        bbl_text = bbl_text.replace(f'"version": "{current}"', f'"version": "{MIN_VERSION}"', 1)
    json.loads(bbl_text)  # still valid JSON
    with open(BBL_JSON, "w", encoding="utf-8") as fh:
        fh.write(bbl_text)

    report["sources"] = {"TINMORRY/TINMORRY-filament-profile-for-Bambu-printers": git_head(user_repo),
                         "TINMORRY/Tinmorry-Bambu_BambuStudio": git_head(bundle_repo)}
    with open(MANIFEST, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"imported {len(entries)} presets, skipped {len(report['skipped'])}, "
          f"{len(report['dropped_keys'])} with unknown keys dropped")
    return 0


def replace_filament_entries(text: str, entries: list[dict]) -> str:
    """Drop old TINMORRY list entries and append the new ones, without reformatting the file."""
    text = re.sub(r',\s*\{\s*"name": "TINMORRY [^"]*",\s*"sub_path": "filament/TINMORRY/[^"]*"\s*\}', "", text)
    start = text.index('"filament_list"')
    i = text.index("[", start)
    depth, in_str, esc = 0, False, False
    for j in range(i, len(text)):
        ch = text[j]
        if in_str:
            esc = ch == "\\" and not esc
            if ch == '"' and not esc:
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = j
                break
    body = text[i + 1:end].rstrip()
    added = "".join(',\n        {\n            "name": %s,\n            "sub_path": %s\n        }'
                    % (json.dumps(e["name"], ensure_ascii=False), json.dumps(e["sub_path"], ensure_ascii=False))
                    for e in entries)
    return text[:i + 1] + body + added + "\n    " + text[end:]


if __name__ == "__main__":
    sys.exit(main(sys.argv))
