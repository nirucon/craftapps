# craft-suite

A small terminal tool that installs and keeps the [ArtCraft Craft apps](https://getartcraft.com/apps) up to date on **Void Linux** and **Debian** (including Ubuntu and their derivatives).

PhotoCraft, VectorCraft, FilmCraft, LightCraft, PdfCraft, EffectCraft, DesignCraft, FigCraft, WordCraft, SoundCraft, DeckCraft, CADCraft, GridCraft — and any new app ArtCraft releases later.

This is a personal script I wrote for my own machines. It is free to use, read, and change. There is no warranty; you run it at your own risk.

## What it does

- On the first run it shows every available app in a checkbox list and installs the ones you pick.
- On later runs it updates the apps you already have and offers any new ones.
- Optionally checks for updates once a day, on its own.
- Verifies every download against the SHA-256 checksum published with the release.
- Notices apps you installed outside the tool by reading the package database or the install directory.

## How it works

It reads each app's latest GitHub release, picks the build that fits your system, and installs it with your distro's own tooling:

| Distro | Format | Where it goes |
| --- | --- | --- |
| Debian, Ubuntu | `.deb` via `apt` | `/usr/bin`, `/usr/share/applications` |
| Void Linux | `.tar.gz` unpacked | `~/.local` (no `sudo` needed) |
| Void Linux, with `xdeb` installed | `.deb` converted to xbps | managed by `xbps-install` |

The app list is never hardcoded. Every run asks the GitHub organisation for its repositories and keeps the ones that ship a Linux build, so a new Craft app appears on its own.

State (which apps you chose, which versions are installed, whether auto-update is on) lives in `~/.local/state/craft-suite/`.

## Install

```sh
git clone https://github.com/nirucon/craftapps
cd craftapps
./install.sh
```

Then run:

```sh
craft-suite
```

If `~/.local/bin` is not on your `PATH` yet, add it:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

## Commands

| Command | What it does |
| --- | --- |
| `craft-suite` | The menu: install, update, uninstall, settings |
| `craft-suite --list` | List every app and its version, change nothing |
| `craft-suite --install photocraft,wordcraft` | Install or update named apps, no menu |
| `craft-suite --method tarball` | Force an install method (`deb`, `tarball`, `xdeb`) |
| `craft-suite --autoupdate` | Quietly update installed apps (for a timer) |
| `craft-suite --enable-autoupdate` | Turn on the daily update check |
| `craft-suite --disable-autoupdate` | Turn it off |
| `craft-suite --self-update` | Fetch the newest craft-suite itself |
| `craft-suite --check-self-update` | Only report whether a newer one exists |

## Auto-update

`craft-suite --enable-autoupdate` installs a once-a-day check using whatever the machine already has:

- a **systemd user timer** on Debian,
- a **cron entry** where cron is set up,
- otherwise a small supervised loop under `runit` (the Void default).

The daily run also updates the tool itself, so new apps and fixes arrive without you opening the menu.

## GitHub rate limits

Without a token GitHub allows only 60 API requests an hour, which a full refresh can use up. To raise the limit, export a token before running it:

```sh
export GITHUB_TOKEN=ghp_your_token_here
```

Responses are cached for a few hours, so normal use stays well under the limit either way.

## How light it is

Measured on x86_64:

| | |
| --- | --- |
| Startup (`--version`) | ~0.1 s |
| Memory while running | ~26 MB |
| CPU while the menu waits for a key | 0% (it blocks on input, it does not poll) |
| Subprocesses on a cached `--list` | none |
| Time for `--list` from cache | ~0.2 s |

The daily check runs once and exits; nothing stays resident in the background
(it is a timer, not a daemon). Installers are deleted after they are used, and
anything older than a week is cleared, so the download folder does not grow.

## Requirements

- Python 3.10 or newer (standard library only — nothing to `pip install`).
- `curl` or `wget` is **not** needed; downloads use Python's own HTTP client.
- On Void, `xdeb` is optional. Without it, apps install into `~/.local` as plain binaries.

## Uninstall

```sh
craft-suite --disable-autoupdate
rm -rf ~/.local/share/craft-suite ~/.local/bin/craft-suite \
       ~/.local/state/craft-suite ~/.local/share/applications/craft-suite.desktop
```

Apps themselves are removed from inside the menu, under **Uninstall apps**.

## Licence

MIT. Do what you like with it.

ArtCraft, the Craft apps, and their names and icons belong to the ArtCraft team. This tool only downloads their official releases.
