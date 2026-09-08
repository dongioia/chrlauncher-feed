#!/usr/bin/env python3
"""Build the chrlauncher update feed served from this repository.

chrlauncher (https://github.com/henrypp/chrlauncher) asks its update server for
a single line of ``key=value`` pairs separated by ``;``::

    version=152.0.7977.83;download=https://.../chrome.7z;timestamp=1788624058;...

Only ``version``, ``download`` and ``timestamp`` are read by the launcher
(see ``src/main.c``, ``_app_checkupdate``); ``revision``, ``sha256`` and
``size`` are passed through for humans and for the watchdog.

Strategy, per (architecture, build type):

1. mirror the upstream feed at chromiumbuilds.org verbatim;
2. if that fails or returns something that does not validate, resolve the
   build natively from its own source (GitHub Releases API, Chromium
   snapshot bucket);
3. if that fails too, keep the file already committed, so a broken upstream
   degrades to a stale feed instead of a dead one.

Every candidate must pass ``validate()`` before it is written. That check is
the security boundary of this repository: the download URL it emits is what
gets executed on the machine running chrlauncher.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FEED_DIR = REPO_ROOT / "api" / "chrlauncher"

UPSTREAM = "https://chromiumbuilds.org/api/chrlauncher/windows-{arch}-{kind}.txt"

ARCHS = (64, 32)
KINDS = (
    "dev-official",
    "stable-codecs-sync",
    "dev-nosync",
    "dev-codecs-sync",
    "dev-codecs-nosync",
    "ungoogled-chromium",
)

# Hosts allowed to serve a Chromium build. A mirrored feed is only as
# trustworthy as the download URL it hands to chrlauncher, so anything outside
# this list is rejected rather than republished.
ALLOWED_DOWNLOAD_HOSTS = frozenset(
    {
        "storage.googleapis.com",
        "commondatastorage.googleapis.com",
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)

ALLOWED_ARCHIVE_SUFFIXES = (".zip", ".7z")

VERSION_RE = re.compile(r"^\d+(?:\.\d+){1,3}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# A Chromium build is a few hundred MB. Anything far below that is an upstream
# packaging accident, not a browser.
MIN_PLAUSIBLE_SIZE = 40 * 1024 * 1024

USER_AGENT = "chrlauncher-feed/1.0 (+https://github.com/dongioia/chrlauncher-feed)"
TIMEOUT = 30


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------


def fetch(url: str, accept: str = "*/*") -> bytes:
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def fetch_text(url: str) -> str:
    return fetch(url).decode("utf-8", "replace").strip()


def fetch_json(url: str):
    return json.loads(fetch(url, accept="application/json"))


# --------------------------------------------------------------------------
# feed line format
# --------------------------------------------------------------------------


def parse(line: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for chunk in line.strip().split(";"):
        if not chunk or "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        fields[key.strip().lower()] = value.strip()
    return fields


def serialise(fields: dict[str, str]) -> str:
    order = ("version", "download", "timestamp", "revision", "sha256", "size")
    parts = [f"{key}={fields[key]}" for key in order if fields.get(key)]
    return ";".join(parts) + "\n"


def validate(fields: dict[str, str]) -> list[str]:
    """Return a list of problems; empty means the entry is publishable."""
    problems: list[str] = []

    version = fields.get("version", "")
    if not VERSION_RE.match(version):
        problems.append(f"version {version!r} is not a dotted numeric version")

    download = fields.get("download", "")
    if not download.startswith("https://"):
        problems.append("download URL is not https")
    else:
        host = download.split("/", 3)[2].lower()
        if host not in ALLOWED_DOWNLOAD_HOSTS:
            problems.append(f"download host {host!r} is not in the allowlist")
        path = download.split("?", 1)[0].lower()
        if not path.endswith(ALLOWED_ARCHIVE_SUFFIXES):
            problems.append("download URL does not point at a .zip or .7z archive")

    timestamp = fields.get("timestamp", "")
    if timestamp and not timestamp.isdigit():
        problems.append("timestamp is not an integer")

    sha256 = fields.get("sha256", "")
    if sha256 and not SHA256_RE.match(sha256):
        problems.append("sha256 is not 64 hex characters")

    return problems


def warnings_for(fields: dict[str, str]) -> list[str]:
    """Non-fatal oddities worth surfacing to the watchdog."""
    notes: list[str] = []
    size = fields.get("size", "")
    if size.isdigit() and int(size) < MIN_PLAUSIBLE_SIZE:
        notes.append(
            f"declared size {int(size)} bytes is far too small for a Chromium build; "
            "the upstream release asset is probably wrong"
        )
    return notes


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split(".")) if VERSION_RE.match(version) else ()


# --------------------------------------------------------------------------
# source 1: mirror
# --------------------------------------------------------------------------


def from_mirror(arch: int, kind: str) -> dict[str, str]:
    return parse(fetch_text(UPSTREAM.format(arch=arch, kind=kind)))


# --------------------------------------------------------------------------
# source 2: native resolvers, used when the mirror is unreachable or invalid
# --------------------------------------------------------------------------


def _github_latest(repo: str, pick, tag_pattern: str | None = None) -> dict[str, str]:
    """Newest release of ``repo`` carrying an asset ``pick`` accepts.

    ``tag_pattern`` narrows the search when a project ships several parallel
    release lines from one repository — RobRich999 publishes an avx2 and an
    avx512 build of every revision, and handing the avx512 one to a machine
    without AVX-512 produces a browser that will not start.
    """
    if tag_pattern is None:
        release = fetch_json(f"https://api.github.com/repos/{repo}/releases/latest")
    else:
        regex = re.compile(tag_pattern, re.IGNORECASE)
        releases = fetch_json(f"https://api.github.com/repos/{repo}/releases?per_page=50")
        release = next(
            (
                candidate
                for candidate in releases
                if not candidate.get("draft")
                and not candidate.get("prerelease")
                and regex.search(candidate.get("tag_name", ""))
            ),
            None,
        )
        if release is None:
            raise LookupError(f"{repo}: no release with a tag matching {tag_pattern!r}")

    asset = pick(release.get("assets", []))
    if asset is None:
        raise LookupError(f"{repo}: no matching asset in release {release.get('tag_name')}")

    tag = release.get("tag_name", "")
    version_match = re.search(r"(\d+\.\d+\.\d+\.\d+)", tag) or re.search(
        r"(\d+\.\d+\.\d+\.\d+)", asset["name"]
    )
    if not version_match:
        raise LookupError(f"{repo}: no version in tag {tag!r}")

    fields = {
        "version": version_match.group(1),
        "download": asset["browser_download_url"],
        "timestamp": str(
            int(
                time.mktime(
                    time.strptime(release["published_at"], "%Y-%m-%dT%H:%M:%SZ")
                )
                - time.timezone
            )
        ),
        "size": str(asset.get("size", "")),
    }
    revision_match = re.search(r"-r(\d+)", tag)
    if revision_match:
        fields["revision"] = revision_match.group(1)
    return fields


def _asset_named(*names: str):
    lowered = tuple(name.lower() for name in names)

    def pick(assets):
        for wanted in lowered:
            for asset in assets:
                if asset["name"].lower() == wanted:
                    return asset
        return None

    return pick


def _asset_matching(pattern: str):
    regex = re.compile(pattern, re.IGNORECASE)

    def pick(assets):
        for asset in assets:
            if regex.search(asset["name"]):
                return asset
        return None

    return pick


def from_snapshots(arch: int) -> dict[str, str]:
    """Resolve the newest official Chromium snapshot for Windows.

    The snapshot bucket knows the commit position but not the product version,
    and chromiumdash knows the product version but not which positions were
    actually archived. Take the newest archived position, then the newest
    Canary release at or below it, so the emitted version stays monotonic.
    """
    platform = "Win_x64" if arch == 64 else "Win"
    base = f"https://storage.googleapis.com/chromium-browser-snapshots/{platform}"

    revision = int(fetch_text(f"{base}/LAST_CHANGE"))

    listing = fetch_json(
        "https://www.googleapis.com/storage/v1/b/chromium-browser-snapshots/o"
        f"?delimiter=/&prefix={platform}/{revision}/&fields=items(name,size)"
    )
    archive = next(
        (item for item in listing.get("items", []) if item["name"].endswith("chrome-win.zip")),
        None,
    )
    if archive is None:
        raise LookupError(f"snapshot {platform}/{revision} has no chrome-win.zip")

    releases = fetch_json(
        "https://chromiumdash.appspot.com/fetch_releases"
        "?channel=Canary&platform=Windows&num=20"
    )
    candidates = [
        release
        for release in releases
        if release.get("chromium_main_branch_position", 1 << 62) <= revision
    ]
    if not candidates:
        raise LookupError("chromiumdash returned no Canary release at or below the snapshot")
    newest = max(candidates, key=lambda r: r["chromium_main_branch_position"])

    return {
        "version": newest["version"],
        "download": f"{base}/{revision}/chrome-win.zip",
        "timestamp": str(int(newest["time"] / 1000)),
        "revision": str(revision),
        "size": str(archive["size"]),
    }


RESOLVERS = {
    ("dev-official", 64): lambda: from_snapshots(64),
    ("dev-official", 32): lambda: from_snapshots(32),
    ("dev-nosync", 32): lambda: from_snapshots(32),
    ("stable-codecs-sync", 64): lambda: _github_latest(
        "Hibbiki/chromium-win64", _asset_named("chrome.7z")
    ),
    ("dev-codecs-sync", 64): lambda: _github_latest(
        "Hibbiki/chromium-win64", _asset_named("chrome.7z")
    ),
    ("dev-nosync", 64): lambda: _github_latest(
        "RobRich999/Chromium_Clang",
        _asset_named("chrome.zip", "chrome.7z"),
        tag_pattern=r"win64-avx2",
    ),
    ("ungoogled-chromium", 32): lambda: _github_latest(
        "ungoogled-software/ungoogled-chromium-windows",
        _asset_matching(r"windows_x86\.zip$"),
    ),
    ("ungoogled-chromium", 64): lambda: _github_latest(
        "ungoogled-software/ungoogled-chromium-windows",
        _asset_matching(r"windows_x64\.zip$"),
    ),
    ("dev-codecs-nosync", 64): lambda: _github_latest(
        "ungoogled-software/ungoogled-chromium-windows",
        _asset_matching(r"windows_x64\.zip$"),
    ),
    ("dev-codecs-nosync", 32): lambda: _github_latest(
        "ungoogled-software/ungoogled-chromium-windows",
        _asset_matching(r"windows_x86\.zip$"),
    ),
    ("stable-codecs-sync", 32): lambda: _github_latest(
        "gz83/thorium", _asset_matching(r"WIN32.*\.zip$")
    ),
    ("dev-codecs-sync", 32): lambda: _github_latest(
        "gz83/thorium", _asset_matching(r"WIN32.*\.zip$")
    ),
}


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------


def build_entry(arch: int, kind: str, report: dict) -> None:
    path = FEED_DIR / f"windows-{arch}-{kind}.txt"
    previous = parse(path.read_text()) if path.exists() else {}

    entry = {
        "arch": arch,
        "type": kind,
        "source": None,
        "errors": [],
        "warnings": [],
        "previous_version": previous.get("version"),
        "version": previous.get("version"),
    }
    report["entries"].append(entry)

    attempts = [("mirror", lambda: from_mirror(arch, kind))]
    resolver = RESOLVERS.get((kind, arch))
    if resolver is not None:
        attempts.append(("native", resolver))

    for source, produce in attempts:
        try:
            fields = produce()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError, LookupError) as exc:
            entry["errors"].append(f"{source}: {type(exc).__name__}: {exc}")
            continue

        problems = validate(fields)
        if problems:
            entry["errors"].extend(f"{source}: {problem}" for problem in problems)
            continue

        is_downgrade = bool(previous.get("version")) and version_key(
            fields["version"]
        ) < version_key(previous["version"])

        # The native resolvers approximate what upstream does; on dev-official
        # they can land a snapshot or two behind. Letting one rewrite the file
        # backwards during a brief chromiumbuilds.org outage would churn the
        # feed and hand chrlauncher an older build than it already knows about,
        # so a downgrade is only accepted from the authoritative mirror.
        if is_downgrade and source != "mirror":
            entry["warnings"].append(
                f"{source} resolved {fields['version']}, older than the published "
                f"{previous['version']}; keeping the published entry"
            )
            continue

        entry["source"] = source
        entry["version"] = fields["version"]
        entry["download"] = fields["download"]
        entry["warnings"].extend(warnings_for(fields))

        if is_downgrade:
            entry["warnings"].append(
                f"upstream moved backwards: {previous['version']} -> {fields['version']}"
            )

        line = serialise(fields)
        if not path.exists() or path.read_text() != line:
            path.write_text(line)
            entry["changed"] = True
        else:
            entry["changed"] = False
        return

    entry["source"] = "stale" if previous else "missing"
    entry["changed"] = False
    if not previous:
        entry["errors"].append("no cached entry to fall back on; file not written")


def main() -> int:
    FEED_DIR.mkdir(parents=True, exist_ok=True)

    report = {
        "generated_at": int(time.time()),
        "generated_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "upstream": UPSTREAM,
        "entries": [],
    }

    for kind in KINDS:
        for arch in ARCHS:
            build_entry(arch, kind, report)

    healthy = [e for e in report["entries"] if e["source"] in ("mirror", "native")]
    report["ok"] = len(healthy)
    report["total"] = len(report["entries"])
    report["degraded"] = [
        f"windows-{e['arch']}-{e['type']}" for e in report["entries"] if e["source"] not in ("mirror", "native")
    ]
    report["fell_back_to_native"] = [
        f"windows-{e['arch']}-{e['type']}" for e in report["entries"] if e["source"] == "native"
    ]

    (FEED_DIR / "status.json").write_text(json.dumps(report, indent=2) + "\n")

    for entry in report["entries"]:
        name = f"windows-{entry['arch']}-{entry['type']}"
        marker = {"mirror": "  ", "native": "~ ", "stale": "! ", "missing": "X "}[entry["source"]]
        print(f"{marker}{name:34} {entry['version'] or '-':>18}  via {entry['source']}")
        for problem in entry["errors"]:
            print(f"    error: {problem}")
        for note in entry["warnings"]:
            print(f"    warn:  {note}")

    print(f"\n{report['ok']}/{report['total']} entries resolved")

    # A single dead build type should not fail the whole run and stop the
    # healthy ones from being committed. Only a total blackout is an error.
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
