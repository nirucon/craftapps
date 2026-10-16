#!/usr/bin/env python3
"""craft-suite: install and update every ArtCraft Craft app on Debian and Void.

First run asks which apps you want. Later runs update what you have and offer
anything new. Optionally checks for updates once a day via a systemd timer or a
cron entry.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import craftlib as core  # noqa: E402
import crafttui as tui  # noqa: E402

VERSION = "1.0.0"

# Where the script checks for its own updates. Keep this pointed at your fork
# so self-update only ever fetches code you control.
SELF_REPO = os.environ.get("CRAFT_SUITE_REPO") or "nirucon/craftapps"
SELF_BRANCH = os.environ.get("CRAFT_SUITE_BRANCH") or "main"
SELF_FILES = ("scripts/craft-suite.py", "scripts/craftlib.py", "scripts/crafttui.py")


def _read_or_empty(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def _raw_url(path: str) -> str:
    return f"https://raw.githubusercontent.com/{SELF_REPO}/{SELF_BRANCH}/{path}"


def _api_url(path: str) -> str:
    return f"{core.GITHUB_API}/repos/{SELF_REPO}/contents/{path}?ref={SELF_BRANCH}"


def _decode_api_content(payload: dict) -> bytes | None:
    import base64
    content = payload.get("content")
    if not content:
        return None
    try:
        return base64.b64decode(content)
    except (ValueError, TypeError):
        return None


def _fetch_scripts(relatives: tuple[str, ...]) -> dict[str, bytes]:
    """Fetch several scripts at once, preferring the git protocol.

    Every HTTP path GitHub offers for file contents (raw.githubusercontent.com,
    jsdelivr, and even the Contents API) sits behind a cache that can serve a
    blob from before the latest push for several minutes. That is long enough
    to make self-update wrong, and to let an old copy overwrite a newer one.
    A single shallow git fetch is authoritative and costs one round trip;
    the HTTP sources remain as fallbacks.
    """
    via_git = _fetch_via_git(relatives)
    if via_git is not None:
        return via_git

    fetched: dict[str, bytes] = {}
    for relative in relatives:
        name = Path(relative).name
        data: bytes | None = None
        try:
            payload = core.http_json(_api_url(relative), ttl=60)
            if isinstance(payload, dict):
                data = _decode_api_content(payload)
        except core.CraftError:
            data = None
        if data is None:
            data = _download_bytes(_raw_url(relative))
        if data is not None:
            fetched[name] = data
    return fetched


def _fetch_via_git(relatives: tuple[str, ...]) -> dict[str, bytes] | None:
    """Read the given files out of the upstream repository using git."""
    import tempfile

    work = Path(tempfile.mkdtemp(prefix="craft-selfupdate-git-"))
    try:
        remote = f"https://github.com/{SELF_REPO}.git"
        clone = work / "repo"
        result = core.run(
            ["git", "clone", "--quiet", "--depth", "1", "--filter=blob:none",
             "--no-checkout", "--branch", SELF_BRANCH, remote, str(clone)],
            check=False, capture=True,
        )
        if result.returncode != 0:
            return None
        result = core.run(["git", "-C", str(clone), "checkout", "--quiet", SELF_BRANCH],
                          check=False, capture=True)
        if result.returncode != 0:
            return None
        found: dict[str, bytes] = {}
        for relative in relatives:
            path = clone / relative
            if not path.is_file():
                return None
            found[Path(relative).name] = path.read_bytes()
        return found
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _remote_revision() -> str | None:
    """The current commit on the upstream branch.

    `git ls-remote` uses the git protocol: always current, never rate
    limited. It is preferred, with the API commit endpoint as a fallback.
    """
    try:
        result = core.run(
            ["git", "ls-remote", f"https://github.com/{SELF_REPO}.git",
             f"refs/heads/{SELF_BRANCH}"],
            check=False, capture=True,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout.split()[0]
    except (OSError, subprocess.SubprocessError):
        pass

    try:
        payload = core.http_json(
            f"{core.GITHUB_API}/repos/{SELF_REPO}/commits/{SELF_BRANCH}",
            cache_key="self-head", ttl=120,
        )
    except core.CraftError:
        return None
    return (payload.get("sha") or "") or None


def _local_revision() -> str:
    """The revision we last installed, recorded next to the scripts."""
    stamp = core.STATE_DIR / "self-revision"
    try:
        return stamp.read_text().strip()
    except OSError:
        return ""


def _record_revision(revision: str) -> None:
    stamp = core.STATE_DIR / "self-revision"
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(revision + "\n")
    except OSError:
        pass


def self_update(*, check_only: bool = False, quiet: bool = False) -> str:
    """Fetch the newest version of these scripts from the configured repo.

    Returns one of "current", "updated", "available" or "unavailable".
    Nothing is written unless the downloaded files parse as valid Python, so a
    broken push upstream can never leave you with a script that will not start.
    """
    import py_compile
    import tempfile

    here = Path(__file__).resolve().parent
    stamp = core.STATE_DIR / "self-update.json"

    remote_rev = _remote_revision()
    if not remote_rev:
        return "unavailable"

    # Fast path: if upstream has not moved since we last installed, there is
    # nothing to fetch. Locally edited files are left alone on purpose, the
    # same way a package manager does not revert your changes on a no-op run.
    recorded = _local_revision()
    if recorded and remote_rev == recorded:
        if not quiet:
            tui.info("craft-suite is already up to date.")
        return "current"

    remote_files = _fetch_scripts(SELF_FILES)
    missing = [Path(r).name for r in SELF_FILES if Path(r).name not in remote_files]
    if missing:
        return "unavailable"

    # Compare against what we have. This means a fresh clone (no recorded
    # revision) is correctly seen as current, and a lagging cache cannot make
    # an up-to-date copy look outdated.
    differing = [name for name, data in remote_files.items()
                 if _read_or_empty(here / name) != data]

    if not differing:
        _record_revision(remote_rev)
        if not quiet:
            tui.info("craft-suite is already up to date.")
        return "current"

    if check_only:
        return "available"

    staging = Path(tempfile.mkdtemp(prefix="craft-selfupdate-"))
    try:
        for name, data in remote_files.items():
            candidate = staging / name
            candidate.write_bytes(data)
            try:
                py_compile.compile(str(candidate), doraise=True)
            except py_compile.PyCompileError as exc:
                raise core.CraftError(
                    f"The upstream copy of {name} is broken; not updating. {exc}"
                ) from exc

        if not quiet:
            tui.info("Updating: " + ", ".join(sorted(differing)))
        for name in differing:
            destination = here / name
            os.replace(staging / name, destination)
            destination.chmod(0o755 if name == "craft-suite.py" else 0o644)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    _record_revision(remote_rev)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(json.dumps({"updated_at": core.now()}) + "\n")
    if not quiet:
        tui.success("craft-suite updated. Restart to use it.")
    core.log(f"self-update: {remote_rev[:12]}")
    return "updated"


def _download_bytes(url: str) -> bytes | None:
    import urllib.request
    request = urllib.request.Request(url, headers={"User-Agent": core.USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except Exception:  # noqa: BLE001 - self-update must never be fatal
        return None


# --------------------------------------------------------------------------
# Catalog building
# --------------------------------------------------------------------------


def load_catalog(host: core.Host, *, refresh: bool = False) -> dict[str, core.Release]:
    """Fetch every app's latest release, with a bit of feedback."""
    if refresh:
        for entry in core.CACHE_DIR.glob("release-*.json"):
            entry.unlink(missing_ok=True)

    if tui.interactive():
        tui.banner()
        print("  " + tui.C.dim("Fetching the latest versions from GitHub…"))
        print()

    last = [""]

    def progress(index: int, total: int, repo: str) -> None:
        if not tui.interactive():
            return
        line = f"  {tui.C.cyan('•')} {repo:<16} ({index}/{total})"
        sys.stdout.write(f"\r\033[2K{line}")
        sys.stdout.flush()
        last[0] = line

    try:
        catalog = core.build_catalog(host.arch, progress=progress)
    finally:
        if tui.interactive() and last[0]:
            sys.stdout.write("\r\033[2K")
            sys.stdout.flush()
    if not catalog:
        raise core.CraftError(
            "Found no Craft apps. Check your network, "
            "or set GITHUB_TOKEN if you have hit the GitHub rate limit."
        )
    return catalog


def build_rows(catalog: dict[str, core.Release],
               installed: dict[str, core.InstalledApp],
               state: core.State) -> list[tui.Row]:
    rows: list[tui.Row] = []
    for slug in sorted(catalog, key=lambda s: (
            list(core.KNOWN_APPS).index(s) if s in core.KNOWN_APPS else 999, s)):
        release = catalog[slug]
        current = installed.get(slug)
        known_description = core.APP_DESCRIPTIONS.get(slug, "")
        row = tui.Row(
            slug=slug,
            name=release.name,
            description=known_description,
            latest=release.version,
        )
        if current:
            row.installed = current.version
            row.method = current.method
            row.status = ("update" if core.version_newer(release.version, current.version)
                          else "installed")
        else:
            row.status = "new"
        row.selected = row.status == "update" or slug in state.selected
        rows.append(row)
    return rows


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------


def apply_changes(host: core.Host, state: core.State, rows: list[tui.Row],
                  catalog: dict[str, core.Release], *,
                  prefix: Path = core.LOCAL_PREFIX) -> int:
    """Install or update the selected rows. Returns a process exit code."""
    backend = core.choose_backend(host, state, prefix)
    todo = [r for r in rows if r.selected and r.status in ("new", "update")]
    if not todo:
        tui.info("Nothing to do — everything selected is already current.")
        return 0

    tui.banner()
    print("  " + tui.C.bold(f"Installing/updating {len(todo)} apps "
                           f"with {backend.label}"))
    print()

    progress = tui.Progress()
    progress.start()
    failures: list[str] = []
    state.selected = sorted(r.slug for r in rows if r.selected)

    for row in todo:
        release = catalog[row.slug]
        label = row.name
        reporter = progress.reporter(label)
        try:
            path, verified = core.download_release(release, backend, reporter=reporter)
            if not verified:
                core.log(f"{row.slug}: no checksum available to verify", quiet=True)
            app = backend.install(release, path, reporter=reporter)
            state.installed[row.slug] = app
            state.save()
            # The installer is unpacked into place, so it is no longer needed.
            core.prune_downloads()
            reporter(f"done — {app.version}")
        except core.CraftError as exc:
            failures.append(f"{label}: {exc}")
            reporter(tui.C.red("failed"))
        except Exception as exc:  # noqa: BLE001 - report anything unexpected
            failures.append(f"{label}: {exc}")
            reporter(tui.C.red("error"))

    progress.finish()
    print()

    installed_now = []
    updated_now = []
    for row in todo:
        if any(f.startswith(row.name + ":") for f in failures):
            continue
        if row.status == "update":
            updated_now.append(f"{row.name} → {catalog[row.slug].version}")
            tui.success(f"{row.name} updated to {catalog[row.slug].version}")
        else:
            installed_now.append(f"{row.name} {catalog[row.slug].version}")
            tui.success(f"{row.name} {catalog[row.slug].version} installed")

    for message in failures:
        tui.failure(message)

    state.last_check = time.time()
    state.save()

    print()
    summary = []
    if installed_now:
        summary.append(f"{len(installed_now)} installed")
    if updated_now:
        summary.append(f"{len(updated_now)} updated")
    if failures:
        summary.append(f"{len(failures)} failed")
    tui.info("Done: " + (", ".join(summary) if summary else "nothing changed"))
    if installed_now and not failures:
        tui.info("The apps are in your application menu now.")

    # Keep the window open so the summary can be read before returning to the
    # menu, instead of the program vanishing the moment it finishes.
    if tui.interactive():
        input("\n  Press enter to continue ")

    return 1 if failures else 0


def remove_apps(host: core.Host, state: core.State, rows: list[tui.Row], *,
                prefix: Path = core.LOCAL_PREFIX) -> int:
    installed_rows = [r for r in rows if r.status in ("installed", "update")]
    if not installed_rows:
        tui.info("No installed apps to remove.")
        return 0
    for row in installed_rows:
        row.selected = False
    chosen = tui.select_apps(installed_rows, title="Choose apps to remove", allow_all=True)
    if not chosen:
        return 0
    if not tui.confirm(f"Remove {len(chosen)} apps?", default=False):
        tui.info("Cancelled.")
        return 0

    backend = None
    failures = []
    for row in chosen:
        app = state.installed.get(row.slug) or core.InstalledApp(
            row.slug, row.installed, "tarball", prefix=str(prefix))
        try:
            active = core.choose_backend(host, state, prefix)
            if app.method == "tarball":
                active = core.TarballBackend(host, prefix)
            active.uninstall(app, reporter=lambda m: tui.info(m))
            state.installed.pop(row.slug, None)
            state.selected = [s for s in state.selected if s != row.slug]
            state.save()
            tui.success(f"{row.name} removed")
        except core.CraftError as exc:
            failures.append(f"{row.name}: {exc}")
            tui.failure(str(exc))
    return 1 if failures else 0


# --------------------------------------------------------------------------
# Autoupdate
# --------------------------------------------------------------------------


CRON_MARKER = "# craft-suite autoupdate"


def autoupdate_paths() -> dict[str, Path]:
    home = Path.home()
    return {
        "service": home / ".config" / "systemd" / "user" / "craft-suite-autoupdate.service",
        "timer": home / ".config" / "systemd" / "user" / "craft-suite-autoupdate.timer",
        "runit": home / ".local" / "share" / "craft-suite" / "runit" / "run",
        "log": home / ".local" / "state" / "craft-suite" / "autoupdate.out",
        "bin": home / ".local" / "bin" / "craft-suite",
        "state": core.STATE_DIR,
    }


def autoupdate_status() -> str:
    paths = autoupdate_paths()
    if paths["timer"].exists():
        return "systemd timer"
    if paths["runit"].exists() and (paths["runit"].parent / "enabled").exists():
        return "runit (Void)"
    try:
        crontab = core.run(["crontab", "-l"], check=False, capture=True).stdout or ""
    except Exception:  # noqa: BLE001
        crontab = ""
    if CRON_MARKER in crontab:
        return "cron"
    return "av"


def _runit_service_dir() -> Path:
    return Path.home() / ".local" / "share" / "craft-suite" / "runit"


def enable_runit(script: str) -> bool:
    """Start a small background loop under runit, for Void and other non-systemd hosts.

    There is no user-level runit when you are not root, so we supervise a
    plain `sleep 24h && craft-suite --autoupdate` loop ourselves. It is started
    by the shell profile the next time a login shell runs, which keeps this
    free of sudo.
    """
    service_dir = _runit_service_dir()
    service_dir.mkdir(parents=True, exist_ok=True)
    (service_dir / "run").write_text(
        "#!/bin/sh\n"
        "# Managed by craft-suite. Runs the daily update loop.\n"
        f'while true; do\n'
        f'  "{script}" --autoupdate >>"{autoupdate_paths()["log"]}" 2>&1\n'
        f'  sleep 86400\n'
        f'done\n'
    )
    (service_dir / "run").chmod(0o755)
    (service_dir / "enabled").write_text(core.now() + "\n")

    # Kick it off now, detached, so autoupdate works without another login.
    core.run(["setsid", str(service_dir / "run")], check=False)
    return True


def disable_runit() -> bool:
    service_dir = _runit_service_dir()
    enabled = service_dir / "enabled"
    if not enabled.exists():
        return False
    enabled.unlink(missing_ok=True)
    core.run(["pkill", "-f", str(service_dir / "run")], check=False)
    return True


def enable_autoupdate() -> None:
    """Install a once-a-day update check, using whatever scheduler exists."""
    paths = autoupdate_paths()
    script = shutil.which("craft-suite") or str(Path(__file__).resolve())
    enabled_via = ""

    if core.have("systemctl"):
        try:
            check = core.run(["systemctl", "--user", "is-system-running"],
                             check=False, capture=True)
            usable = check.returncode in (0, 1) or "running" in (check.stdout or "")
        except Exception:  # noqa: BLE001
            usable = False
        if usable:
            paths["service"].parent.mkdir(parents=True, exist_ok=True)
            paths["service"].write_text(
                "[Unit]\n"
                "Description=ArtCraft Suite automatic update\n\n"
                "[Service]\n"
                "Type=oneshot\n"
                f"ExecStart={script} --autoupdate\n"
            )
            paths["timer"].write_text(
                "[Unit]\n"
                "Description=Run the ArtCraft Suite update once a day\n\n"
                "[Timer]\n"
                "OnCalendar=daily\n"
                "Persistent=true\n"
                "RandomizedDelaySec=1h\n\n"
                "[Install]\n"
                "WantedBy=timers.target\n"
            )
            core.run(["systemctl", "--user", "daemon-reload"], check=False)
            result = core.run(["systemctl", "--user", "enable", "--now",
                               "craft-suite-autoupdate.timer"],
                              check=False, capture=True)
            if result.returncode == 0:
                enabled_via = "systemd user timer"

    if not enabled_via and core.have("crontab"):
        existing = core.run(["crontab", "-l"], check=False, capture=True).stdout or ""
        lines = [ln for ln in existing.splitlines() if CRON_MARKER not in ln]
        lines.append(f"0 12 * * * {script} --autoupdate >/dev/null 2>&1 {CRON_MARKER}")
        payload = "\n".join(line for line in lines if line.strip()) + "\n"
        result = core.run(["crontab", "-"], check=False, input_text=payload)
        if result.returncode == 0:
            enabled_via = "cron"

    # Void ships runit and no user cron, so fall back to a supervised loop.
    if not enabled_via:
        enable_runit(script)
        enabled_via = "runit background loop (Void)"

    if not enabled_via:
        raise core.CraftError(
            "Found no systemd (user), cron or runit. Add a line to your own "
            "scheduler that runs: craft-suite --autoupdate"
        )
    tui.success(f"Auto-update enabled ({enabled_via}), runs once a day.")


def disable_autoupdate() -> None:
    paths = autoupdate_paths()
    removed = False
    if paths["timer"].exists():
        core.run(["systemctl", "--user", "disable", "--now",
                  "craft-suite-autoupdate.timer"], check=False, capture=True)
        paths["timer"].unlink(missing_ok=True)
        paths["service"].unlink(missing_ok=True)
        core.run(["systemctl", "--user", "daemon-reload"], check=False)
        removed = True
    if core.have("crontab"):
        existing = core.run(["crontab", "-l"], check=False, capture=True).stdout or ""
        if CRON_MARKER in existing:
            kept = [ln for ln in existing.splitlines() if CRON_MARKER not in ln]
            core.run(["crontab", "-"], check=False,
                     input_text="\n".join(kept) + ("\n" if kept else ""))
            removed = True
    if disable_runit():
        removed = True
    if removed:
        tui.success("Auto-update disabled.")
    else:
        tui.info("Auto-update was not enabled.")


def run_autoupdate(host: core.Host, state: core.State, *, quiet: bool = True) -> int:
    """The unattended path: update everything already selected and installed."""
    # Keep the tool itself current first, so new apps and fixes arrive even if
    # nobody ever starts the TUI by hand.
    try:
        self_update(quiet=True)
    except Exception as exc:  # noqa: BLE001 - never let this block app updates
        core.log(f"autoupdate: self-update failed: {exc}")

    try:
        catalog = load_catalog(host)
    except core.CraftError as exc:
        core.log(f"autoupdate: {exc}")
        return 1

    rows = build_rows(catalog, core.installed_snapshot(host, state), state)
    for row in rows:
        # Unattended runs only ever touch apps that are installed *and* selected.
        row.selected = row.status == "update" and row.slug in state.selected
    pending = [r for r in rows if r.selected]
    if not pending:
        core.log("autoupdate: nothing to update")
        return 0

    core.log(f"autoupdate: updating {', '.join(r.slug for r in pending)}")
    if quiet:
        code = 0
        backend = core.choose_backend(host, state)
        used: set[str] = set()
        for row in pending:
            try:
                path, _ = core.download_release(catalog[row.slug], backend)
                used.add(path.name)
                state.installed[row.slug] = backend.install(catalog[row.slug], path)
                state.save()
                core.log(f"autoupdate: {row.slug} → {catalog[row.slug].version}")
                core.prune_downloads(keep=set(used))
            except Exception as exc:  # noqa: BLE001
                core.log(f"autoupdate: {row.slug} failed: {exc}")
                code = 1
        core.prune_downloads(keep=set(used))
        state.last_check = time.time()
        state.save()
        return code

    return apply_changes(host, state, rows, catalog)


# --------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------


def settings_menu(host: core.Host, state: core.State, *, prefix: Path = core.LOCAL_PREFIX) -> None:
    while True:
        backend = core.choose_backend(host, state, prefix)
        options = [
            f"Install method: {backend.label}",
            f"Auto-update: {autoupdate_status()}",
            f"Status: {autoupdate_status()}",
            "Update craft-suite (self-update)",
            "Back",
        ]
        choice = tui.choose("Settings", options)
        if choice is None or choice == 4:
            return
        if choice == 0:
            methods = [
                ("", "Automatic (recommended)"),
                ("deb", "Debian package (.deb) via apt — needs sudo"),
                ("tarball", f"Tarball into {prefix} — no sudo"),
                ("xdeb", "xdeb: .deb converted to xbps — needs sudo"),
            ]
            labels = [label for _, label in methods]
            picked = tui.choose("Choose install method", labels)
            if picked is not None:
                state.method_override = methods[picked][0]
                state.save()
                tui.success(f"Method set to: {methods[picked][1]}")
        elif choice == 1:
            if autoupdate_status() == "av":
                enable_autoupdate()
                state.autoupdate = True
            else:
                disable_autoupdate()
                state.autoupdate = False
            state.save()
        elif choice == 2:
            print()
            tui.info(f"Distribution:  {host.family} ({host.arch})")
            tui.info(f"Supported:     {'yes' if host.supported else 'no — using tarball'}")
            tui.info(f"Method:        {backend.label}")
            tui.info(f"Auto-update:   {autoupdate_status()}")
            tui.info(f"Version:       craft-suite {VERSION}")
            tui.info(f"Self-update from: {SELF_REPO} ({SELF_BRANCH})")
            tui.info(f"State:         {core.STATE_FILE}")
            tui.info(f"Log:           {core.LOG_FILE}")
            tui.info(f"Downloads:     {core.DOWNLOAD_DIR}")
            input("\n  Press enter to continue ")
        elif choice == 3:
            print()
            state_result = self_update()
            if state_result == "unavailable":
                tui.warning("Could not reach GitHub right now.")
            input("\n  Press enter to continue ")


def main_screen(host: core.Host, state: core.State, *, prefix: Path = core.LOCAL_PREFIX) -> int:
    if not host.supported:
        tui.banner()
        tui.warning(f"This machine runs '{host.family}', not Debian or Void.")
        tui.info("Apps use the tarball and install into ~/.local (no sudo).")
        if not tui.confirm("Continue anyway?", default=True):
            return 0

    catalog = load_catalog(host)
    installed = core.installed_snapshot(host, state)
    state.installed.update(installed)

    # Treat any machine that already has apps as past its first run, even when
    # the state file predates this tool. Someone upgrading, or who installed
    # the apps by hand, should land straight in the menu.
    if not state.first_run_done and installed:
        state.first_run_done = True
        if not state.selected:
            state.selected = sorted(installed)
        state.save()

    if not state.first_run_done:
        tui.banner()
        print("  " + tui.C.bold("Welcome!"))
        print()
        tui.info("First run: choose which Craft apps you want.")
        tui.info("Later runs update them and offer any new apps.")
        print()
        input("  Press enter to fetch the app list ")

    rows = build_rows(catalog, installed, state)

    while True:
        updates = [r for r in rows if r.status == "update"]
        fresh = [r for r in rows if r.status == "new"]

        if state.first_run_done:
            options = []
            if updates:
                options.append(("update", f"Update {len(updates)} apps"))
            if fresh:
                options.append(("install", f"Install new apps ({len(fresh)} available)"))
            options.append(("manage", "Manage everything (free selection)"))
            options.append(("refresh", "Check for new versions"))
            options.append(("settings", "Settings"))
            options.append(("remove", "Uninstall apps"))
            options.append(("quit", "Quit"))

            lines = []
            for r in rows:
                if r.status == "update":
                    lines.append(f"    {r.name:<16} {r.installed} → {r.latest}")
                elif r.status == "installed":
                    lines.append(f"    {r.name:<16} {r.latest}  (current)")
            choice = tui.choose("What would you like to do?", [label for _, label in options])
            if choice is None:
                return 0
            action = options[choice][0]

            if action == "update":
                for row in rows:
                    row.selected = row.status == "update"
                apply_changes(host, state, rows, catalog, prefix=prefix)
                catalog = load_catalog(host)
                rows = build_rows(catalog, core.installed_snapshot(host, state), state)
                continue
            if action == "quit":
                return 0
            if action == "refresh":
                catalog = load_catalog(host, refresh=True)
                installed = core.installed_snapshot(host, state)
                rows = build_rows(catalog, installed, state)
                continue
            if action == "settings":
                settings_menu(host, state, prefix=prefix)
                continue
            if action == "remove":
                remove_apps(host, state, rows, prefix=prefix)
                catalog = load_catalog(host)
                rows = build_rows(catalog, core.installed_snapshot(host, state), state)
                continue
            if action == "install":
                candidates = [r for r in rows if r.status == "new"]
                for row in candidates:
                    row.selected = False
                chosen = tui.select_apps(candidates, title="Choose apps to install")
                if chosen is None:
                    continue
                for row in rows:
                    row.selected = row in chosen
                apply_changes(host, state, rows, catalog, prefix=prefix)
                catalog = load_catalog(host)
                rows = build_rows(catalog, core.installed_snapshot(host, state), state)
                continue
            # action == "manage" falls through to the picker below.

        else:
            chosen = tui.select_apps(rows, title="Choose which apps to install")
            if chosen is None:
                return 0
            state.first_run_done = True
            state.selected = sorted(r.slug for r in rows if r.selected)
            state.save()
            if not chosen:
                tui.info("No apps selected. Run the script again whenever you like.")
                return 0
            apply_changes(host, state, rows, catalog, prefix=prefix)
            # Back to the menu so the summary is followed by something usable,
            # rather than the program closing on the user.
            continue

        # "Manage everything": free selection, updates pre-ticked, in-place changes.
        for row in rows:
            row.selected = row.status in ("update",)
        chosen = tui.select_apps(rows, title="Choose apps (updates are pre-selected)")
        if chosen is None:
            continue
        apply_changes(host, state, rows, catalog, prefix=prefix)
        catalog = load_catalog(host)
        rows = build_rows(catalog, core.installed_snapshot(host, state), state)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="craft-suite",
        description="Install and update the ArtCraft Craft apps (Debian and Void).",
    )
    parser.add_argument("--autoupdate", action="store_true",
                        help="quietly update installed apps (for timer/cron)")
    parser.add_argument("--self-update", action="store_true",
                        help="fetch the latest craft-suite from the GitHub repo")
    parser.add_argument("--check-self-update", action="store_true",
                        help="only report whether a newer craft-suite exists")
    parser.add_argument("--list", action="store_true",
                        help="list apps and versions, change nothing")
    parser.add_argument("--install", metavar="APP[,APP…]",
                        help="install/update the given apps without the menu")
    parser.add_argument("--method", choices=["deb", "tarball", "xdeb"],
                        help="force an install method")
    parser.add_argument("--prefix", metavar="DIR",
                        help="root for tarball installs (default ~/.local)")
    parser.add_argument("--enable-autoupdate", action="store_true",
                        help="enable the daily automatic update")
    parser.add_argument("--disable-autoupdate", action="store_true",
                        help="disable the daily automatic update")
    parser.add_argument("--version", action="version", version=f"craft-suite {VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tui.setup_colour()
    core.STATE_DIR.mkdir(parents=True, exist_ok=True)

    host = core.detect_host()
    state = core.State.load()
    if args.method:
        state.method_override = args.method
    prefix = Path(args.prefix).expanduser() if args.prefix else core.LOCAL_PREFIX

    try:
        if args.check_self_update:
            state = self_update(check_only=True)
            labels = {
                "available": "A newer craft-suite is available — run --self-update",
                "current": "craft-suite is already up to date",
                "unavailable": "Could not reach GitHub to check",
            }
            print(labels.get(state, state))
            return 0
        if args.self_update:
            self_update()
            return 0
        if args.enable_autoupdate:
            enable_autoupdate()
            return 0
        if args.disable_autoupdate:
            disable_autoupdate()
            return 0
        if args.autoupdate:
            return run_autoupdate(host, state)
        if args.list:
            catalog = load_catalog(host)
            rows = build_rows(catalog, core.installed_snapshot(host, state), state)
            print()
            for row in rows:
                marker = {"installed": "installed", "update": "update",
                          "new": "not installed"}[row.status]
                version = row.latest
                if row.status == "update":
                    version = f"{row.installed} → {row.latest}"
                print(f"  {row.name:<16} {version:<18} {marker}")
            return 0
        if args.install:
            wanted = [s.strip().lower() for s in args.install.split(",") if s.strip()]
            catalog = load_catalog(host)
            unknown = [s for s in wanted if s not in catalog]
            if unknown:
                raise core.CraftError(f"Unknown apps: {', '.join(unknown)}")
            rows = build_rows(catalog, core.installed_snapshot(host, state), state)
            for row in rows:
                row.selected = row.slug in wanted
            # A deliberate command-line install counts as having chosen apps,
            # so a later interactive run goes straight to the menu.
            state.selected = sorted(set(state.selected) | set(wanted))
            state.first_run_done = True
            state.save()
            return apply_changes(host, state, rows, catalog, prefix=prefix)
        return main_screen(host, state, prefix=prefix)
    except KeyboardInterrupt:
        print()
        tui.info("Cancelled.")
        return 130
    except core.CraftError as exc:
        print()
        tui.failure(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
