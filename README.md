# chrlauncher-feed

A replacement update channel for [chrlauncher](https://github.com/henrypp/chrlauncher),
served from GitHub.

chrlauncher 2.7 moved its default update URL from `chromium.woolyss.com` to
`chromiumbuilds.org`. On a network that blocks that host, the launcher stops
finding updates and there is no obvious sign of why. This repository
republishes the same feed under `raw.githubusercontent.com`, so pointing
`ChromiumUpdateUrl` at it gets updates working again without patching or
recompiling anything.

I built it because my work machine blocks `chromiumbuilds.org` but not GitHub.

## Use it

In `chrlauncher.ini`:

```ini
ChromiumUpdateUrl=https://raw.githubusercontent.com/dongioia/chrlauncher-feed/main/api/chrlauncher/windows-%d-%s.txt
```

chrlauncher substitutes `%d` with the architecture (`64` or `32`) and `%s` with
`ChromiumType`, the same way it does for its own default URL. Leave the other
settings alone. To test it right away, set `ChromiumCheckPeriod=-1` once, or run
`chrlauncher.exe -forcecheck`.

### The feed is not the only host involved

Replacing the feed fixes update discovery. The build itself still comes from
whichever host the entry points at, and that host has to be reachable too:

| entry | version at last build | download host |
| --- | --- | --- |
| `64-dev-official` | 155.0.8046.0 | storage.googleapis.com |
| `32-dev-official` | 155.0.8046.0 | storage.googleapis.com |
| `64-stable-codecs-sync` | 152.0.7977.83 | github.com |
| `32-stable-codecs-sync` | 152.0.7977.55 | github.com |
| `64-dev-codecs-sync` | 152.0.7977.83 | github.com |
| `32-dev-codecs-sync` | 152.0.7977.55 | github.com |
| `64-dev-nosync` | 153.0.7993.0 | github.com |
| `32-dev-nosync` | 155.0.8046.0 | storage.googleapis.com |
| `64-dev-codecs-nosync` | 150.0.7871.252 | github.com |
| `32-dev-codecs-nosync` | 152.0.7977.55 | github.com |
| `64-ungoogled-chromium` | 150.0.7871.252 | github.com |
| `32-ungoogled-chromium` | 152.0.7977.82 | github.com |

So if your network only allows GitHub, `dev-official` will find the update and
then fail to download it, because those binaries live on
`storage.googleapis.com`. The types that work end to end in that case are
`stable-codecs-sync` and `dev-codecs-sync`, both Hibbiki builds with
proprietary codecs, published as GitHub releases.

The table is a snapshot from when I wrote this and is not regenerated. Live
values are in [`api/chrlauncher/status.json`](api/chrlauncher/status.json).

## How it is built

`scripts/build_feed.py` runs from GitHub Actions once a day at 05:00 UTC
([`update-feed.yml`](.github/workflows/update-feed.yml)). For each of the twelve
architecture and type combinations it tries three things in order.

First it mirrors: fetch the upstream entry from `chromiumbuilds.org` and
republish it as-is. Upstream decides which project supplies each build type, and
mirroring keeps that decision instead of reimplementing it, so the feed stays
right when upstream switches sources.

If the mirror is unreachable or returns something that fails validation, it
resolves the build directly from its own source instead: the GitHub Releases API
for the Hibbiki, RobRich999, Thorium and ungoogled-chromium builds, and the
Chromium snapshot bucket plus chromiumdash for `dev-official`.

If that fails too, it leaves the committed entry alone. A broken upstream should
degrade to a stale feed, not a dead one.

Every candidate has to pass `validate()` before it gets written, and that check
is the security boundary of this repo, because the URL the feed emits is the URL
chrlauncher downloads and runs. It requires a dotted numeric version, an `https`
download URL whose host is on an allowlist (`storage.googleapis.com`,
`github.com`, `objects.githubusercontent.com` and siblings) and whose path ends
in `.zip` or `.7z`, an integer timestamp, and a 64-hex-character sha256 when one
is present. A mirrored entry pointing anywhere else gets dropped rather than
republished.

A native resolution that comes out *older* than the published entry is rejected
too. The native `dev-official` resolver can land a snapshot or two behind
upstream, and it should not roll the feed backwards during a short outage.

## Feed format

One line, `key=value` pairs separated by `;`:

```
version=152.0.7977.83;download=https://github.com/Hibbiki/chromium-win64/releases/download/v152.0.7977.83-r1669021/chrome.7z;timestamp=1788624058;revision=1669021;sha256=47a5...;size=438854370
```

chrlauncher reads only `version`, `download` and `timestamp` (see
`_app_checkupdate` in `src/main.c`). It ignores `revision`, `sha256` and `size`,
which I keep for my own use and for the monitoring routine. Versions are
compared with `_r_str_versioncompare`. The archive is unpacked with miniz if it
is a zip, falling back to the LZMA SDK for 7z.

## Running it locally

```bash
python3 scripts/build_feed.py
```

Standard library only, no dependencies. Set `GITHUB_TOKEN` if you are hammering
the native resolvers and hit the API rate limit.

## Caveats

`raw.githubusercontent.com` is a different host from `github.com`. If your
network allows one and not the other, this will not help you, and serving the
same files from GitHub Pages is the fallback.

Raw file responses sit behind a CDN cache of roughly five minutes, so a new
entry is not visible the instant it is committed.

Upstream currently maps `ungoogled-chromium` to `macchrome/winchrome`, which has
not published anything since 150.x. My native resolver uses
`ungoogled-software/ungoogled-chromium-windows` instead, so those two entries
will disagree if the mirror ever goes down.

This repository redistributes nothing. It publishes URLs to builds hosted by the
Chromium project, Hibbiki, RobRich999, Thorium and ungoogled-chromium.
