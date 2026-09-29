#!/usr/bin/env python3
"""Add an app to this source by inspecting its IPA.

Reads the bundle identifier, version, display name, minimum iOS version and
usage descriptions straight out of the IPA, pulls the largest app icon out of it
and writes ``apps/<bundle id>.json`` + ``icons/<bundle id>.png``. Then rebuilds
the sources unless --no-build is given.

The icon is re-encoded: IPAs ship icons as Apple's "CgBI" PNG variant, which
most image stacks (and probably the clients) cannot read.

Usage:
    scripts/add-app.py <ipa-url-or-path> [options]

Examples:
    # A one-off IPA, downloaded from wherever it lives
    scripts/add-app.py https://example.com/App.ipa --subtitle "文件管理器"

    # Track a GitHub release instead of pinning one IPA
    scripts/add-app.py https://github.com/o/r/releases/download/v1/App.ipa \\
        --auto-repo o/r --auto-asset App.ipa --min-os 15.0
"""

from __future__ import annotations

import argparse
import json
import plistlib
import re
import struct
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APPS_DIR = ROOT / "apps"
ICONS_DIR = ROOT / "icons"
USER_AGENT = "trollstore-source (+https://github.com/rangdl/trollstore-source)"

PRIVACY_KEYS = (
    "NSCameraUsageDescription",
    "NSPhotoLibraryUsageDescription",
    "NSPhotoLibraryAddUsageDescription",
    "NSMicrophoneUsageDescription",
    "NSLocalNetworkUsageDescription",
    "NSLocationWhenInUseUsageDescription",
    "NSContactsUsageDescription",
    "NSCalendarsUsageDescription",
    "NSBluetoothAlwaysUsageDescription",
)


def fail(message: str):
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def info(message: str):
    print(f"==> {message}")


# ------------------------------------------------------------------- png codec

def _unfilter(raw: bytes, width: int, height: int, bpp: int) -> bytearray:
    stride = width * bpp
    out = bytearray()
    previous = bytearray(stride)
    pos = 0
    for _ in range(height):
        filter_type = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if filter_type == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif filter_type == 2:
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif filter_type == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif filter_type == 4:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                up = previous[i]
                upleft = previous[i - bpp] if i >= bpp else 0
                estimate = left + up - upleft
                dl, du, dul = abs(estimate - left), abs(estimate - up), abs(estimate - upleft)
                if dl <= du and dl <= dul:
                    predictor = left
                elif du <= dul:
                    predictor = up
                else:
                    predictor = upleft
                line[i] = (line[i] + predictor) & 0xFF
        elif filter_type != 0:
            fail(f"unsupported PNG filter type {filter_type}")
        out += line
        previous = line
    return out


def decode_png(data: bytes) -> tuple[int, int, bytes] | None:
    """Decode a PNG — including Apple's CgBI variant — into (w, h, RGBA)."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None

    pos = 8
    idat = bytearray()
    width = height = None
    depth = colour_type = None
    cgbi = False

    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        tag = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length

        if tag == b"CgBI":
            cgbi = True
        elif tag == b"IHDR":
            width, height, depth, colour_type = struct.unpack(">IIBB", chunk[:10])
        elif tag == b"IDAT":
            idat += chunk
        elif tag == b"IEND":
            break

    if None in (width, height, depth, colour_type):
        return None
    if depth != 8 or colour_type not in (2, 6):
        return None  # only 8-bit RGB / RGBA icons are handled

    try:
        raw = zlib.decompress(bytes(idat), -15) if cgbi else zlib.decompress(bytes(idat))
    except zlib.error:
        return None

    bpp = 3 if colour_type == 2 else 4
    if len(raw) < height * (1 + width * bpp):
        return None
    pixels = _unfilter(raw, width, height, bpp)

    rgba = bytearray()
    if cgbi:
        # Apple stores these as premultiplied BGRA.
        for i in range(0, len(pixels), 4):
            blue, green, red, alpha = pixels[i], pixels[i + 1], pixels[i + 2], pixels[i + 3]
            if 0 < alpha < 255:
                red = min(255, red * 255 // alpha)
                green = min(255, green * 255 // alpha)
                blue = min(255, blue * 255 // alpha)
            rgba += bytes((red, green, blue, alpha))
    elif bpp == 4:
        rgba = bytearray(pixels)
    else:
        for i in range(0, len(pixels), 3):
            rgba += bytes((pixels[i], pixels[i + 1], pixels[i + 2], 255))

    return width, height, bytes(rgba)


def encode_png(width: int, height: int, rgba: bytes) -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + rgba[y * width * 4:(y + 1) * width * 4] for y in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


# ---------------------------------------------------------------------- ipa io

def fetch(source: str) -> Path:
    """Return a local path to the IPA, downloading it when given a URL."""
    if not source.startswith(("http://", "https://")):
        path = Path(source).expanduser()
        if not path.is_file():
            fail(f"{path} not found")
        return path

    info(f"Downloading {source}")
    request = urllib.request.Request(source, headers={"User-Agent": USER_AGENT})
    handle, name = tempfile.mkstemp(suffix=".ipa")
    with urllib.request.urlopen(request, timeout=120) as response, open(handle, "wb") as out:
        while chunk := response.read(1 << 20):
            out.write(chunk)
    return Path(name)


def icon_rank(name: str) -> tuple[int, int]:
    """Sort key for AppIconNNxNN@Nx.png — bigger is better, plain names last."""
    match = re.search(r"AppIcon(\d+)x\d+@(\d)x", name)
    if match:
        return int(match.group(1)) * int(match.group(2)), 1
    return 0, 0


def inspect(ipa: Path) -> tuple[dict, bytes | None]:
    with zipfile.ZipFile(ipa) as archive:
        names = archive.namelist()
        plist_names = [n for n in names
                       if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", n)]
        if not plist_names:
            fail(f"{ipa.name} does not look like an IPA (no Payload/*.app/Info.plist)")
        plist = plistlib.loads(archive.read(plist_names[0]))

        app_dir = plist_names[0].rsplit("/", 1)[0]
        icons = sorted(
            (n for n in names
             if n.startswith(app_dir + "/") and n.lower().endswith(".png")
             and re.search(r"AppIcon|^Payload/[^/]+\.app/Icon", n.split("/")[-1])),
            key=lambda n: icon_rank(n.split("/")[-1]),
            reverse=True,
        )
        icon_bytes = None
        for candidate in icons:
            decoded = decode_png(archive.read(candidate))
            if decoded:
                width, height, rgba = decoded
                icon_bytes = encode_png(width, height, rgba)
                info(f"Icon: {candidate.split('/')[-1]} ({width}×{height})")
                break
        if icon_bytes is None and icons:
            print("warn: could not decode any icon from the IPA — add icons/<bundle id>.png by hand",
                  file=sys.stderr)

    return plist, icon_bytes


# ---------------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ipa", help="path or URL to the .ipa")
    parser.add_argument("--name", help="display name (default: from the IPA)")
    parser.add_argument("--developer", default="", help="developer name")
    parser.add_argument("--subtitle", default="", help="one-line subtitle")
    parser.add_argument("--description", help="long description (default: from the IPA)")
    parser.add_argument("--category", default="utilities",
                        help="AltStore category, e.g. developer/utilities/games (default: utilities)")
    parser.add_argument("--tint-color", default="#1E1E36")
    parser.add_argument("--min-os", help="minimum iOS version (default: from the IPA)")
    parser.add_argument("--auto-repo", help="OWNER/REPO to track for future releases")
    parser.add_argument("--auto-asset", help="asset name inside that repo's releases")
    parser.add_argument("--auto-asset-pattern",
                        help="regex matching the asset name (use when the name carries the version)")
    parser.add_argument("--auto-tag",
                        help="release tag to track instead of the repo's latest release "
                             "(e.g. a rolling 'latest' prerelease)")
    parser.add_argument("--auto-version-pattern", help="regex whose group 1 is the version")
    parser.add_argument("--version-from-asset", action="store_true",
                        help="apply --auto-version-pattern to the asset name instead of the tag, "
                             "and date the version by the asset's upload time")
    parser.add_argument("--force", action="store_true", help="overwrite an existing app file")
    parser.add_argument("--no-build", action="store_true", help="don't rebuild the sources afterwards")
    args = parser.parse_args()

    ipa = fetch(args.ipa)
    plist, icon_bytes = inspect(ipa)

    bundle_id = plist.get("CFBundleIdentifier")
    if not bundle_id:
        fail("the IPA has no CFBundleIdentifier")

    version = plist.get("CFBundleShortVersionString") or "1.0"
    build = plist.get("CFBundleVersion") or ""
    name = args.name or plist.get("CFBundleDisplayName") or plist.get("CFBundleName") or bundle_id
    min_os = args.min_os or plist.get("MinimumOSVersion") or "14.0"

    privacy = {key: plist[key] for key in PRIVACY_KEYS if plist.get(key)}
    description = args.description or (
        f"{name} {version}（未签名 IPA，需要 TrollStore 安装）。"
    )

    app = {
        "name": name,
        "bundleIdentifier": bundle_id,
        "developerName": args.developer,
        "subtitle": args.subtitle,
        "localizedDescription": description,
        "icon": f"icons/{bundle_id}.png",
        "tintColor": args.tint_color,
        "category": args.category,
        "screenshots": [],
        "appPermissions": {"entitlements": [], "privacy": privacy},
    }

    if args.auto_repo:
        if not args.auto_asset and not args.auto_asset_pattern:
            fail("--auto-repo needs --auto-asset (exact name) or --auto-asset-pattern (regex)")
        if args.version_from_asset and not args.auto_asset_pattern:
            fail("--version-from-asset only makes sense with --auto-asset-pattern")
        app["auto"] = {
            "repo": args.auto_repo,
            "minOSVersion": min_os,
            **({"asset": args.auto_asset} if args.auto_asset else {}),
            **({"assetPattern": args.auto_asset_pattern} if args.auto_asset_pattern else {}),
            **({"tag": args.auto_tag} if args.auto_tag else {}),
            **({"versionPattern": args.auto_version_pattern} if args.auto_version_pattern else {}),
            **({"versionFromAsset": True} if args.version_from_asset else {}),
        }
    else:
        app["versions"] = [{
            "version": version,
            "date": "",
            "downloadURL": args.ipa if args.ipa.startswith("http") else "",
            "size": ipa.stat().st_size,
            "minOSVersion": min_os,
            "localizedDescription": f"{name} {version}" + (f" (build {build})" if build else ""),
        }]
        if not app["versions"][0]["downloadURL"]:
            print("warn: the IPA was a local file — fill in its downloadURL in the app file by hand",
                  file=sys.stderr)

    APPS_DIR.mkdir(exist_ok=True)
    ICONS_DIR.mkdir(exist_ok=True)
    app_path = APPS_DIR / f"{bundle_id}.json"
    if app_path.exists() and not args.force:
        fail(f"apps/{bundle_id}.json already exists — pass --force to overwrite")

    if icon_bytes:
        (ICONS_DIR / f"{bundle_id}.png").write_bytes(icon_bytes)
        info(f"Wrote icons/{bundle_id}.png")
    app_path.write_text(json.dumps(app, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    info(f"Wrote apps/{bundle_id}.json")

    print()
    print(f"  {name}  {bundle_id}  {version}" + (f" (build {build})" if build else ""))
    print(f"  minimum iOS {min_os}")

    if args.no_build:
        print("\n  run scripts/build_source.py to regenerate the sources")
        return 0

    print()
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "build_source.py")]).returncode


if __name__ == "__main__":
    raise SystemExit(main())
