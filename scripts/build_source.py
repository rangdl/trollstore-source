#!/usr/bin/env python3
"""Generate source.json and source-cn.json from repo.json + apps/*.json.

An app file describes one app. Two flavours are supported:

* **auto** — the app's versions come from a GitHub repository's latest release
  (``"auto": {"repo": "...", "asset": "...", "versionPattern": "..."}``). The
  refresh workflow re-runs this script on a schedule, so those apps stay current
  without any hand editing.
* **manual** — the file carries its own ``versions`` array, for IPAs that are not
  published as GitHub releases.

``source.json`` links straight to GitHub; ``source-cn.json`` is the same source
with every URL routed through a GitHub acceleration mirror, for networks where
github.com is unreachable.

Usage:
    scripts/build_source.py [--proxy URL] [--branch NAME] [--repo OWNER/NAME]
                            [--check]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APPS_DIR = ROOT / "apps"
DEFAULT_PROXY = "https://gh-proxy.com/"
USER_AGENT = "trollstore-source (+https://github.com/rangdl/trollstore-source)"


def fail(message: str):
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


# --------------------------------------------------------------------------- io

def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(f"{path.relative_to(ROOT)} not found")
    except json.JSONDecodeError as exc:
        fail(f"{path.relative_to(ROOT)} is not valid JSON: {exc}")


def write_json(path: Path, payload: dict) -> bool:
    """Write pretty JSON; return True when the file changed."""
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True


# ------------------------------------------------------------------- github api

def github_token() -> str | None:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    try:
        out = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=15
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    return None


def github_get(path: str, token: str | None) -> dict:
    request = urllib.request.Request(
        f"https://api.github.com/{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.load(exc).get("message", "")
        except Exception:  # noqa: BLE001 - the body is not always JSON
            pass
        fail(f"GitHub API {path} -> {exc.code} {detail}".strip())
    except urllib.error.URLError as exc:
        fail(f"GitHub API {path} unreachable: {exc.reason}")


def version_from_tag(tag: str, pattern: str | None) -> str:
    """v1.0.2-119 -> 1.0.2 (default), or whatever ``pattern`` captures."""
    regex = pattern or r"^v?([0-9]+\.[0-9]+\.[0-9]+)"
    match = re.search(regex, tag)
    return match.group(1) if match else tag.lstrip("v")


def release_for(app: dict, token: str | None) -> dict:
    """Fetch the release an ``auto`` app tracks.

    ``auto.tag`` pins a specific tag (e.g. a rolling ``latest`` prerelease);
    without it GitHub's latest non-prerelease, non-draft release is used.
    """
    repo = app["auto"]["repo"]
    tag = app["auto"].get("tag")
    path = f"repos/{repo}/releases/tags/{tag}" if tag else f"repos/{repo}/releases/latest"
    return github_get(path, token)


def pick_asset(app: dict, release: dict) -> dict:
    """Find the release asset an ``auto`` app installs.

    ``auto.asset`` matches a name exactly; ``auto.assetPattern`` is a regex that
    may match several (e.g. a rolling tag that accumulates dated builds) — the
    newest upload wins.
    """
    auto = app["auto"]
    assets = release.get("assets") or []
    pattern = auto.get("assetPattern")
    if pattern:
        regex = re.compile(pattern)
        matches = [a for a in assets if regex.search(a["name"])]
        if not matches:
            available = ", ".join(a["name"] for a in assets) or "(none)"
            fail(
                f"{app['name']}: no asset matching {pattern!r} in {auto['repo']} "
                f"{release.get('tag_name')} — available: {available}"
            )
        matches.sort(key=lambda a: (a.get("created_at") or "", a["name"]), reverse=True)
        return matches[0]
    asset_name = auto.get("asset")
    asset = next((a for a in assets if a["name"] == asset_name), None)
    if asset is None:
        available = ", ".join(a["name"] for a in assets) or "(none)"
        fail(
            f"{app['name']}: no asset named {asset_name!r} in {auto['repo']} "
            f"{release.get('tag_name')} — available: {available}"
        )
    return asset


def versions_from_release(app: dict, token: str | None) -> list[dict]:
    auto = app["auto"]
    release = release_for(app, token)
    asset = pick_asset(app, release)
    # A rolling tag is pinned (its tag_name is constant), so take the version —
    # and, with ``versionFromAsset``, the pattern itself — from the asset name.
    from_asset = auto.get("versionFromAsset")
    version_source = asset["name"] if from_asset else release.get("tag_name", "")
    version = version_from_tag(version_source, auto.get("versionPattern"))
    notes = (release.get("body") or "").strip().splitlines()
    # Assets of a rolling tag carry their own upload date, which is the honest
    # date for that build; a pinned tag's publish date would never move.
    date = ((asset.get("created_at") if from_asset else None)
            or release.get("published_at") or "")[:10]
    entry = {
        "version": version,
        "date": date,
        "downloadURL": asset["browser_download_url"],
        "size": asset["size"],
        "minOSVersion": auto.get("minOSVersion", "14.0"),
        "localizedDescription": notes[0] if notes else f"{app['name']} {version}",
    }
    # Tags of the form v1.0.2-119 carry the build number after the dash.
    if auto.get("buildFromTag"):
        entry["buildVersion"] = release.get("tag_name", "").split("-")[-1]
    return [entry]


# ------------------------------------------------------------------- assembling

def strip_nulls(value):
    """Drop keys we only set to None (AltStore ignores unknown/absent fields)."""
    if isinstance(value, dict):
        return {k: strip_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [strip_nulls(v) for v in value]
    return value


def load_apps(token: str | None) -> list[dict]:
    if not APPS_DIR.is_dir():
        fail("apps/ directory not found — run this from the repository root")

    apps = []
    for path in sorted(APPS_DIR.glob("*.json")):
        app = read_json(path)
        app["_file"] = path.name
        for key in ("name", "bundleIdentifier", "icon"):
            if not app.get(key):
                fail(f"apps/{path.name}: missing required key {key!r}")

        if app.get("auto"):
            versions = versions_from_release(app, token)
        else:
            versions = app.get("versions") or []
            if not versions:
                fail(f"apps/{path.name}: needs either an \"auto\" block or a \"versions\" array")

        # Newest first: AltStore treats versions[0] as the current one.
        versions.sort(key=lambda v: (v.get("date") or "", v.get("version") or ""), reverse=True)
        for index, version in enumerate(versions):
            for key in ("version", "downloadURL", "size"):
                if version.get(key) in (None, ""):
                    fail(f"apps/{path.name}: version entry {index} is missing {key!r}")
            version.setdefault("minOSVersion", "14.0")
            version.setdefault("date", "")
            version.setdefault("localizedDescription", f"{app['name']} {version['version']}")

        app["versions"] = versions
        apps.append(app)

    if not apps:
        fail("no app files found in apps/")
    return apps


def build_source(repo_meta: dict, apps: list[dict], slug: str, branch: str,
                 proxy: str, identifier_suffix: str, name_suffix: str) -> dict:
    raw_base = f"https://raw.githubusercontent.com/{slug}/{branch}/"

    def url(target: str) -> str:
        """Prefix repo-relative paths and GitHub URLs with the mirror."""
        if not target:
            return target
        absolute = target if target.startswith("http") else raw_base + target.lstrip("/")
        return f"{proxy}{absolute}" if proxy else absolute

    payload = {
        "name": repo_meta["name"] + name_suffix,
        "identifier": repo_meta["identifier"] + identifier_suffix,
        "subtitle": repo_meta.get("subtitle", ""),
        "description": repo_meta.get("description", ""),
        "iconURL": url(repo_meta.get("icon", "")),
        "website": repo_meta.get("website", ""),
        "tintColor": repo_meta.get("tintColor", "#1E1E36"),
        "featuredApps": repo_meta.get("featuredApps", []),
        "apps": [],
        "news": repo_meta.get("news", []),
    }

    for app in apps:
        # "icon" is how this repo locates the file; the client gets iconURL.
        entry = {k: v for k, v in app.items()
                 if not k.startswith("_") and k not in ("auto", "versions", "icon")}
        entry["iconURL"] = url(app["icon"])
        entry["versions"] = [
            {**version, "downloadURL": url(version["downloadURL"])}
            for version in app["versions"]
        ]
        entry.setdefault("screenshots", [])
        entry.setdefault("appPermissions", {"entitlements": [], "privacy": {}})
        payload["apps"].append(entry)

    return strip_nulls(payload)


# -------------------------------------------------------------------------- cli

def remote_slug() -> str:
    for remote in ("origin", "fork"):
        try:
            url = subprocess.run(
                ["git", "remote", "get-url", remote],
                capture_output=True, text=True, timeout=15, check=True,
            ).stdout.strip()
        except (FileNotFoundError, subprocess.SubprocessError):
            continue
        match = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", url)
        if match:
            return match.group(1)
    fail("could not work out OWNER/REPO from a git remote — pass --repo")


def current_branch() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=15, check=True,
        ).stdout.strip()
        return out if out and out != "HEAD" else "main"
    except (FileNotFoundError, subprocess.SubprocessError):
        return "main"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--proxy", default=os.environ.get("PROXY", DEFAULT_PROXY),
                        help=f"acceleration prefix for source-cn.json (default: {DEFAULT_PROXY}; empty disables it)")
    parser.add_argument("--branch", default=os.environ.get("BRANCH") or None,
                        help="branch the sources are served from (default: the checked-out branch)")
    parser.add_argument("--repo", default=os.environ.get("REPO") or None,
                        help="OWNER/REPO the sources live in (default: from the git remote)")
    parser.add_argument("--check", action="store_true",
                        help="don't write anything; exit 1 if the sources are out of date")
    args = parser.parse_args()

    branch = args.branch or current_branch()
    slug = args.repo or remote_slug()
    token = github_token()

    repo_meta = read_json(ROOT / "repo.json")
    apps = load_apps(token)

    plain = build_source(repo_meta, apps, slug, branch, "", "", "")
    accelerated = build_source(repo_meta, apps, slug, branch, args.proxy, ".cn", " (国内加速)") if args.proxy else None

    targets = [(ROOT / "source.json", plain)]
    if accelerated:
        targets.append((ROOT / "source-cn.json", accelerated))

    changed = []
    for path, payload in targets:
        if args.check:
            text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                changed.append(path.name)
        elif write_json(path, payload):
            changed.append(path.name)

    for app in apps:
        latest = app["versions"][0]
        source = "auto:" + app["auto"]["repo"] if app.get("auto") else "manual"
        print(f"  {app['name']:<24} {latest['version']:<12} {latest['date']:<12} {source}")
    print()
    for path, _ in targets:
        print(f"  {path.name}: {len(apps)} app(s)")
    print(f"  https://raw.githubusercontent.com/{slug}/{branch}/source.json")
    if accelerated:
        print(f"  {args.proxy}https://raw.githubusercontent.com/{slug}/{branch}/source-cn.json  (国内)")

    if args.check and changed:
        print(f"\nerror: out of date: {', '.join(changed)}", file=sys.stderr)
        return 1
    if changed and not args.check:
        print(f"\n  updated: {', '.join(changed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
