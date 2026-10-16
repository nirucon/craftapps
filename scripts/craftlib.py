"""Core library for craft-suite.

Deliberately dependency-free: Python 3.11+ standard library only.
Everything that touches the network, the package manager or the filesystem
lives here so the TUI layer stays presentational.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

GITHUB_ORG = "storytold"
GITHUB_API = "https://api.github.com"

# The canonical app list. Discovery can find *more* than this (new apps appear
# regularly), but this list gives nice display names and stable ordering.
KNOWN_APPS: dict[str, str] = {
    "photocraft": "PhotoCraft",
    "vectorcraft": "VectorCraft",
    "filmcraft": "FilmCraft",
    "lightcraft": "LightCraft",
    "pdfcraft": "PdfCraft",
    "effectcraft": "EffectCraft",
    "designcraft": "DesignCraft",
    "figcraft": "FigCraft",
    "wordcraft": "WordCraft",
    "soundcraft": "SoundCraft",
    "deckcraft": "DeckCraft",
    "cadcraft": "CADCraft",
    "gridcraft": "GridCraft",
}

APP_DESCRIPTIONS: dict[str, str] = {
    "photocraft": "Image editing (Photoshop alternative)",
    "vectorcraft": "Vector illustration (Illustrator alternative)",
    "filmcraft": "Video editing (Premiere alternative)",
    "lightcraft": "Photo library and RAW development (Lightroom)",
    "pdfcraft": "PDF workbench (Acrobat alternative)",
    "effectcraft": "Motion graphics and VFX (After Effects)",
    "designcraft": "Page layout and publishing (InDesign alternative)",
    "figcraft": "Interface design and prototyping (Figma alternative)",
    "wordcraft": "Word processing (Word alternative)",
    "soundcraft": "Digital audio workstation (DAW)",
    "deckcraft": "Presentations (PowerPoint alternative)",
    "cadcraft": "Computer-aided design and drafting (CAD)",
    "gridcraft": "Spreadsheets and data analysis (Excel alternative)",
}

STATE_DIR = Path(
    os.environ.get("CRAFT_SUITE_STATE")
    or (Path.home() / ".local" / "state" / "craft-suite")
)
CACHE_DIR = STATE_DIR / "cache"
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = STATE_DIR / "craft-suite.log"

DOWNLOAD_DIR = Path(
    os.environ.get("CRAFT_SUITE_DOWNLOAD_DIR") or (Path.home() / "Downloads" / "craft")
)

# Where a tarball install lands when we are not root.
LOCAL_PREFIX = Path(os.environ.get("CRAFT_SUITE_PREFIX") or (Path.home() / ".local"))

USER_AGENT = "craft-suite/1.0 (+https://getartcraft.com/apps)"
CACHE_TTL = int(os.environ.get("CRAFT_SUITE_CACHE_TTL") or 6 * 3600)


class CraftError(RuntimeError):
    """A condition we can explain to the user."""


# --------------------------------------------------------------------------
# Host / distro detection
# --------------------------------------------------------------------------


@dataclass
class Host:
    distro: str  # "debian" | "void" | "unknown"
    family: str  # "debian" | "void" | "arch" | ...
    arch: str  # "x86_64" | "aarch64" | "riscv64"
    id_like: list[str] = field(default_factory=list)

    @property
    def supported(self) -> bool:
        return self.distro in {"debian", "void"}


def detect_host() -> Host:
    """Work out which distro family we are on and normalise the CPU name."""
    os_release: dict[str, str] = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            os_release[key.strip()] = value.strip().strip('"')
    except OSError:
        pass

    ident = (os_release.get("ID") or "").lower()
    id_like = [p for p in (os_release.get("ID_LIKE") or "").lower().split() if p]
    haystack = {ident, *id_like}

    if "void" in haystack:
        distro, family = "void", "void"
    elif haystack & {"debian", "ubuntu", "linuxmint", "pop", "raspbian"}:
        distro, family = "debian", "debian"
    elif "arch" in haystack or ident == "omarchy":
        distro, family = "unknown", "arch"
    else:
        distro, family = "unknown", ident or "unknown"

    machine = platform.machine().lower()
    arch = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "arm64": "aarch64",
        "aarch64": "aarch64",
        "riscv64": "riscv64",
    }.get(machine, machine)

    return Host(distro=distro, family=family, arch=arch, id_like=id_like)


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------


def log(message: str, *, quiet: bool = False) -> None:
    """Append to the state log and, unless quiet, echo to stderr."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp}  {message}"
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    if not quiet:
        print(line, file=sys.stderr)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()


def http_json(url: str, *, timeout: int = 30, cache_key: str | None = None,
              ttl: int = CACHE_TTL) -> dict:
    """GET a JSON document, with an optional on-disk cache.

    GitHub's unauthenticated API allows only 60 requests per hour, so cached
    responses matter for the daily autoupdate run.
    """
    if cache_key:
        cached = CACHE_DIR / f"{cache_key}.json"
        if cached.exists() and (time.time() - cached.stat().st_mtime) < ttl:
            try:
                return json.loads(cached.read_text())
            except (OSError, ValueError):
                pass

    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context()) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        remaining = exc.headers.get("X-RateLimit-Remaining") if exc.headers else None
        if exc.code == 403 and remaining == "0":
            raise CraftError(
                "GitHub rate limit reached (60 requests/hour without a token). "
                "Set GITHUB_TOKEN for a higher limit, or wait for the window to reset."
            ) from exc
        if exc.code == 404:
            raise CraftError(f"Not found: {url}") from exc
        raise CraftError(f"HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise CraftError(f"Network error for {url}: {exc.reason}") from exc

    if cache_key:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            (CACHE_DIR / f"{cache_key}.json").write_text(json.dumps(payload))
        except OSError:
            pass
    return payload


def download(url: str, destination: Path, *, progress=None) -> Path:
    """Stream a URL to disk, reporting bytes as they arrive."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": USER_AGENT}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, headers=headers)
    tmp = destination.with_suffix(destination.suffix + ".part")
    try:
        with urllib.request.urlopen(request, timeout=60, context=_ssl_context()) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with tmp.open("wb") as handle:
                while True:
                    chunk = resp.read(1 << 18)
                    if not chunk:
                        break
                    handle.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
        tmp.replace(destination)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return destination


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prune_downloads(keep: set[str] | None = None, *, max_age_days: int = 7) -> int:
    """Delete leftover installers so the download folder does not grow forever.

    Each release is 16-160 MB, so after a few update rounds a full app set
    would otherwise leave gigabytes behind. The installer for the version
    currently being installed is always kept (see `keep`); anything older than
    `max_age_days` goes, along with stray `.part` files from interrupted runs.
    Returns the number of bytes reclaimed.
    """
    if not DOWNLOAD_DIR.is_dir():
        return 0

    keep = keep or set()
    reclaimer = int((time.time() - max_age_days * 86400) * 1e9)
    reclaimed = 0

    for entry in DOWNLOAD_DIR.iterdir():
        try:
            if entry.name in keep:
                continue
            if entry.name.endswith(".part"):
                reclaimed += entry.stat().st_size
                entry.unlink()
                continue
            if not entry.is_file():
                continue
            if entry.stat().st_mtime_ns < reclaimer:
                reclaimed += entry.stat().st_size
                entry.unlink()
        except OSError:
            continue

    if reclaimed:
        log(f"pruned {reclaimed / 1e6:.0f} MB of old downloads", quiet=True)
    return reclaimed


def fetch_checksums(url: str) -> dict[str, str]:
    """Download a SHA256SUMS.txt and return {filename: hexdigest}."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30, context=_ssl_context()) as resp:
            text = resp.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - checksum file is optional
        log(f"Could not fetch checksums ({url}): {exc}", quiet=True)
        return {}

    sums: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+)$", line.strip())
        if match:
            sums[match.group(2).strip()] = match.group(1).lower()
    return sums


# --------------------------------------------------------------------------
# Release discovery
# --------------------------------------------------------------------------


@dataclass
class Release:
    repo: str
    slug: str
    name: str
    version: str
    tag: str
    assets: dict[str, str]  # asset filename -> browser_download_url
    published_at: str = ""
    notes_url: str = ""

    def asset(self, *suffixes: str) -> str | None:
        """Find an asset whose name ends with every given suffix."""
        for name, url in self.assets.items():
            if all(name.endswith(s) for s in suffixes):
                return url
        return None


_SLUG_RE = re.compile(r"^[a-z][a-z0-9]*craft$")


def is_craft_repo(slug: str) -> bool:
    return bool(_SLUG_RE.match(slug))


def display_name(slug: str) -> str:
    if slug in KNOWN_APPS:
        return KNOWN_APPS[slug]
    # cadcraft -> CADCraft, pdfcraft -> PDFCraft, newthingcraft -> NewthingCraft
    stem = slug[: -len("craft")]
    upper = {"cad": "CAD", "pdf": "PDF", "vfx": "VFX"}
    return upper.get(stem, stem.capitalize()) + "Craft"


def discover_repos(*, include_all: bool = True) -> list[str]:
    """List candidate Craft app repositories under the GitHub org.

    New apps are released regularly, so we ask the org for its repositories and
    keep the ones that look like apps and actually ship a Linux build. When the
    API is unreachable or rate-limited we fall back to the known list.
    """
    repos: set[str] = set(KNOWN_APPS)
    if include_all:
        try:
            page = 1
            while page <= 5:
                batch = http_json(
                    f"{GITHUB_API}/orgs/{GITHUB_ORG}/repos"
                    f"?per_page=100&page={page}&sort=updated",
                    cache_key=f"org-repos-{page}",
                )
                if not isinstance(batch, list) or not batch:
                    break
                for item in batch:
                    name = (item.get("name") or "").lower()
                    if is_craft_repo(name) and not item.get("archived"):
                        repos.add(name)
                if len(batch) < 100:
                    break
                page += 1
        except CraftError as exc:
            log(f"Could not list the organisation repositories: {exc}", quiet=True)

    return sorted(repos, key=lambda s: (list(KNOWN_APPS).index(s) if s in KNOWN_APPS else 999, s))


def fetch_latest(repo: str) -> Release:
    """Fetch the latest release for one app repository."""
    data = http_json(
        f"{GITHUB_API}/repos/{GITHUB_ORG}/{repo}/releases/latest",
        cache_key=f"release-{repo}",
    )
    tag = data.get("tag_name") or ""
    version = tag.lstrip("vV") or data.get("name", "")
    assets = {
        asset.get("name", ""): asset.get("browser_download_url", "")
        for asset in data.get("assets", [])
        if asset.get("name")
    }
    if not assets:
        raise CraftError(f"No release found for {repo}")
    return Release(
        repo=f"{GITHUB_ORG}/{repo}",
        slug=repo,
        name=display_name(repo),
        version=version,
        tag=tag,
        assets=assets,
        published_at=(data.get("published_at") or "")[:10],
        notes_url=data.get("html_url") or "",
    )


def has_linux_build(release: Release, arch: str) -> bool:
    return any(
        f"linux-{arch}" in name and (name.endswith(".deb") or name.endswith(".tar.gz"))
        for name in release.assets
    )


def build_catalog(arch: str, *, progress=None) -> dict[str, Release]:
    """Fetch latest releases for every candidate app.

    Apps without a Linux build for this architecture are silently skipped: the
    org has plenty of non-app repositories and a few apps lag behind on arm64.
    """
    catalog: dict[str, Release] = {}
    repos = discover_repos()
    for index, repo in enumerate(repos, start=1):
        if progress:
            progress(index, len(repos), repo)
        try:
            release = fetch_latest(repo)
        except CraftError:
            continue
        if has_linux_build(release, arch):
            catalog[repo] = release
    return catalog


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


@dataclass
class InstalledApp:
    slug: str
    version: str
    method: str  # "deb" | "tarball"
    prefix: str = ""  # install root for tarball installs
    installed_at: str = ""


@dataclass
class State:
    selected: list[str] = field(default_factory=list)
    installed: dict[str, InstalledApp] = field(default_factory=dict)
    autoupdate: bool = False
    last_check: float = 0.0
    method_override: str = ""  # "" = automatic
    first_run_done: bool = False

    @classmethod
    def load(cls) -> "State":
        if not STATE_FILE.exists():
            return cls()
        try:
            raw = json.loads(STATE_FILE.read_text())
        except (OSError, ValueError):
            return cls()
        installed = {
            slug: InstalledApp(**data)
            for slug, data in (raw.get("installed") or {}).items()
            if isinstance(data, dict)
        }
        return cls(
            selected=list(raw.get("selected") or []),
            installed=installed,
            autoupdate=bool(raw.get("autoupdate")),
            last_check=float(raw.get("last_check") or 0),
            method_override=raw.get("method_override") or "",
            first_run_done=bool(raw.get("first_run_done")),
        )

    def save(self) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "selected": sorted(self.selected),
            "installed": {s: vars(a) for s, a in sorted(self.installed.items())},
            "autoupdate": self.autoupdate,
            "last_check": self.last_check,
            "method_override": self.method_override,
            "first_run_done": self.first_run_done,
        }
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        tmp.replace(STATE_FILE)


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------


def run(cmd: list[str], *, check: bool = True, capture: bool = False,
        input_text: str | None = None) -> subprocess.CompletedProcess:
    log("run: " + " ".join(cmd), quiet=True)
    return subprocess.run(
        cmd,
        check=check,
        text=True,
        input=input_text,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def have(program: str) -> bool:
    return shutil.which(program) is not None


class DebianBackend:
    """Install .deb packages through apt, which resolves the Depends: fields."""

    name = "deb"
    label = "Debian package (.deb) via apt"

    def __init__(self, host: Host) -> None:
        self.host = host

    def applicable(self) -> bool:
        return self.host.distro == "debian" and have("apt-get") and have("dpkg")

    def asset_suffix(self) -> str:
        return f"linux-{self.host.arch}.deb"

    def install(self, release: Release, path: Path, *, reporter=None) -> InstalledApp:
        if reporter:
            reporter("Installing " + release.name + " with apt (needs sudo)…")
        env = dict(os.environ)
        # Never let apt open an interactive prompt we cannot answer.
        env["DEBIAN_FRONTEND"] = "noninteractive"
        cmd = ["sudo", "-n", "apt-get", "install", "-y", "--no-install-recommends", str(path)]
        result = subprocess.run(cmd, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if result.returncode != 0:
            if "password is required" in (result.stdout or "") or "sudo:" in (result.stdout or ""):
                raise CraftError(
                    "sudo requires a password. Run the script in a real terminal, "
                    "or run 'sudo -v' first."
                )
            raise CraftError(f"apt misslyckades:\n{result.stdout.strip()[-2000:]}")
        if reporter:
            reporter(result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "done")
        return InstalledApp(release.slug, release.version, "deb", installed_at=now())

    def uninstall(self, app: InstalledApp, *, reporter=None) -> None:
        if reporter:
            reporter(f"Removing {app.slug} with apt…")
        result = subprocess.run(["sudo", "-n", "apt-get", "remove", "-y", app.slug],
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if result.returncode != 0:
            raise CraftError(f"apt remove misslyckades:\n{result.stdout.strip()[-1000:]}")

    def guess_installed(self) -> dict[str, InstalledApp]:
        """Read dpkg's database so apps installed by hand are recognised."""
        found: dict[str, InstalledApp] = {}
        if not have("dpkg-query"):
            return found
        result = subprocess.run(
            ["dpkg-query", "-W", "-f=${Package}\t${Version}\t${Status}\n"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return found
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 3 or "installed" not in parts[2]:
                continue
            pkg = parts[0]
            if is_craft_repo(pkg):
                version = parts[1].split("-")[0]
                found[pkg] = InstalledApp(pkg, version, "deb")
        return found


class TarballBackend:
    """Unpack the release tarball into a prefix (~/.local by default).

    This is the Void Linux path: Void has no apt, but the tarball is a plain
    bin/ + share/ tree, which is all a desktop app needs.
    """

    name = "tarball"
    label = "Tarball (bin/ + share/)"

    def __init__(self, host: Host, prefix: Path = LOCAL_PREFIX) -> None:
        self.host = host
        self.prefix = Path(prefix)

    def applicable(self) -> bool:
        return True

    def asset_suffix(self) -> str:
        return f"linux-{self.host.arch}.tar.gz"

    # -- paths ------------------------------------------------------------
    @property
    def lib_root(self) -> Path:
        return self.prefix / "lib" / "craft-suite"

    def app_dir(self, slug: str) -> Path:
        return self.lib_root / slug

    @property
    def bin_dir(self) -> Path:
        return self.prefix / "bin"

    @property
    def applications_dir(self) -> Path:
        return self.prefix / "share" / "applications"

    @property
    def icons_dir(self) -> Path:
        return self.prefix / "share" / "icons" / "hicolor"

    def install(self, release: Release, path: Path, *, reporter=None) -> InstalledApp:
        target = self.app_dir(release.slug)
        staging = target.parent / (release.slug + ".new")
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)

        if reporter:
            reporter(f"Unpacking {release.name} into {target}…")
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            # The tarball has a single top-level directory; strip it.
            roots = {m.name.split("/")[0] for m in members if m.name.strip("/")}
            if len(roots) != 1:
                raise CraftError(f"Unexpected structure in {path.name}")
            prefix = roots.pop()
            self._safe_extract(archive, members, staging, prefix)

        # Swap the new tree into place so a half-written update never lands.
        old = target.parent / (release.slug + ".old")
        if old.exists():
            shutil.rmtree(old)
        if target.exists():
            target.rename(old)
        staging.rename(target)
        if old.exists():
            shutil.rmtree(old, ignore_errors=True)

        self._link_binaries(release.slug, target)
        self._install_desktop(release.slug, target)
        return InstalledApp(release.slug, release.version, "tarball",
                            prefix=str(self.prefix), installed_at=now())

    @staticmethod
    def _safe_extract(archive: tarfile.TarFile, members, destination: Path,
                      strip_prefix: str) -> None:
        """Extract, refusing paths that escape the destination."""
        base = destination.resolve()
        for member in members:
            name = member.name
            if not name.startswith(strip_prefix + "/") and name != strip_prefix:
                continue
            relative = name[len(strip_prefix):].lstrip("/")
            if not relative:
                continue
            if member.issym() or member.islnk():
                # Keep symlinks only when they stay inside the tree.
                link_target = os.path.normpath(str(Path(relative).parent / member.linkname))
                if link_target.startswith(".."):
                    continue
            target = (base / relative).resolve()
            if not str(target).startswith(str(base)):
                raise CraftError(f"Unsafe path inside the archive: {name}")
            if member.isdir():
                (base / relative).mkdir(parents=True, exist_ok=True)
                continue
            source = archive.extractfile(member)
            if source is None:
                continue
            out = base / relative
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open("wb") as handle:
                shutil.copyfileobj(source, handle)
            os.chmod(out, member.mode & 0o777)

    def _link_binaries(self, slug: str, target: Path) -> None:
        """Link every executable in the release's bin/ into the prefix bin."""
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        source_dir = target / "bin"
        if not source_dir.is_dir():
            source_dir = target / "usr" / "bin"
        for binary in sorted(source_dir.iterdir()):
            if not binary.is_file():
                continue
            binary.chmod(binary.stat().st_mode | 0o111)
            link = self.bin_dir / binary.name
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(binary)

    def _install_desktop(self, slug: str, target: Path) -> None:
        """Copy the app's own .desktop and icons into the prefix."""
        self.applications_dir.mkdir(parents=True, exist_ok=True)
        source_dir = target / "share" / "applications"
        if source_dir.is_dir():
            for desktop in source_dir.glob("*.desktop"):
                text = desktop.read_text()
                # Point Exec/TryExec at the prefix bin so the entry works
                # whether or not ~/.local/bin is on PATH.
                text = self._rewrite_desktop(text)
                destination = self.applications_dir / desktop.name
                destination.write_text(text)
                destination.chmod(0o644)

        icon_source = target / "share" / "icons" / "hicolor"
        if icon_source.is_dir():
            for icon in icon_source.rglob("*"):
                if icon.is_file():
                    relative = icon.relative_to(icon_source)
                    out = self.icons_dir / relative
                    out.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(icon, out)

        self._refresh_caches()

    def _rewrite_desktop(self, text: str) -> str:
        lines = []
        for line in text.splitlines():
            if line.startswith("Exec="):
                command = line[5:].split()[0] if line[5:].strip() else ""
                rest = line[5:].strip()[len(command):]
                line = f"Exec={self.bin_dir / command}{rest}"
            elif line.startswith("TryExec="):
                line = f"TryExec={self.bin_dir / line[8:].strip()}"
            lines.append(line)
        return "\n".join(lines) + "\n"

    @staticmethod
    def _refresh_caches() -> None:
        for command in (["update-desktop-database", str(LOCAL_PREFIX / "share" / "applications")],
                        ["gtk-update-icon-cache", "-q", "-f", "-t",
                         str(LOCAL_PREFIX / "share" / "icons" / "hicolor")]):
            if have(command[0]):
                subprocess.run(command, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=False)

    def uninstall(self, app: InstalledApp, *, reporter=None) -> None:
        if reporter:
            reporter(f"Removing {app.slug}…")
        target = self.app_dir(app.slug)
        if target.exists():
            shutil.rmtree(target)
        for link in self.bin_dir.glob("*"):
            if link.is_symlink() and str(link.resolve()).startswith(str(target)):
                link.unlink()
        for desktop in self.applications_dir.glob(f"ai.storyteller.{app.slug}.desktop"):
            desktop.unlink()
        self._refresh_caches()

    def guess_installed(self) -> dict[str, InstalledApp]:
        """Detect tarball installs this tool (or the launcher) made earlier."""
        found: dict[str, InstalledApp] = {}
        if not self.lib_root.is_dir():
            return found
        for directory in sorted(self.lib_root.iterdir()):
            if not directory.is_dir() or not is_craft_repo(directory.name):
                continue
            version = ""
            binary = directory / "bin" / directory.name
            if binary.exists():
                version = sniff_version([str(binary)])
            found[directory.name] = InstalledApp(
                directory.name, version, "tarball", prefix=str(self.prefix)
            )
        return found


class XdebBackend(TarballBackend):
    """Optional: convert the .deb to an xbps package using xdeb.

    Some people prefer a real xbps entry over a prefix tree. xdeb is a
    third-party script, so this stays opt-in and falls back to the tarball.
    """

    name = "xdeb"
    label = "Debian package converted to xbps (xdeb)"

    def applicable(self) -> bool:
        return self.host.distro == "void" and have("xdeb")

    def asset_suffix(self) -> str:
        return f"linux-{self.host.arch}.deb"

    def install(self, release: Release, path: Path, *, reporter=None) -> InstalledApp:
        if reporter:
            reporter(f"Converting {release.name} with xdeb…")
        work = Path(tempfile.mkdtemp(prefix="craft-xdeb-"))
        env = dict(os.environ)
        env["XDEB_PKGROOT"] = str(work)
        result = subprocess.run(["xdeb", "-Sedf", str(path)], cwd=work, env=env,
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if result.returncode != 0:
            shutil.rmtree(work, ignore_errors=True)
            raise CraftError(f"xdeb misslyckades:\n{result.stdout.strip()[-1500:]}")
        repo = work / "binpkgs"
        packages = sorted(repo.glob("*.xbps"))
        if not packages:
            shutil.rmtree(work, ignore_errors=True)
            raise CraftError("xdeb produced no package")
        if reporter:
            reporter("Installing with xbps-install (needs sudo)…")
        cmd = ["sudo", "-n", "xbps-install", "-R", str(repo), "-y", packages[0].name]
        result = subprocess.run(cmd, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        shutil.rmtree(work, ignore_errors=True)
        if result.returncode != 0:
            raise CraftError(f"xbps-install misslyckades:\n{result.stdout.strip()[-1500:]}")
        return InstalledApp(release.slug, release.version, "deb", installed_at=now())


def sniff_version(candidates: list[str]) -> str:
    """Ask a binary for its version; many Rust CLIs answer --version."""
    for candidate in candidates:
        for flag in ("--version", "-V"):
            try:
                result = subprocess.run([candidate, flag], text=True, timeout=10,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            except (OSError, subprocess.SubprocessError):
                continue
            match = re.search(r"(\d+\.\d+(?:\.\d+)?(?:-rc\.\d+)?)", result.stdout or "")
            if match:
                return match.group(1)
    return ""


def choose_backend(host: Host, state: State, prefix: Path = LOCAL_PREFIX):
    """Pick the installation backend for this host and configured preference."""
    debian = DebianBackend(host)
    tarball = TarballBackend(host, prefix)
    xdeb = XdebBackend(host, prefix)
    order = {
        "deb": [debian, tarball],
        "tarball": [tarball],
        "xdeb": [xdeb, tarball],
        "": [debian, xdeb, tarball] if host.distro == "debian"
           else ([xdeb, tarball] if host.distro == "void" else [tarball]),
    }[state.method_override if state.method_override in {"deb", "tarball", "xdeb"} else ""]
    for backend in order:
        if backend.applicable():
            return backend
    return tarball


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------
# High-level operations
# --------------------------------------------------------------------------


def installed_snapshot(host: Host, state: State, prefix: Path = LOCAL_PREFIX) -> dict[str, InstalledApp]:
    """Merge what dpkg/the prefix tree know with what we recorded ourselves."""
    found: dict[str, InstalledApp] = {}
    found.update(TarballBackend(host, prefix).guess_installed())
    if host.distro == "debian":
        found.update(DebianBackend(host).guess_installed())
    # Our own records win, since they carry the method we actually used.
    for slug, app in state.installed.items():
        found.setdefault(slug, app)
        if slug in found and not found[slug].version:
            found[slug] = app
    return found


def version_newer(candidate: str, current: str) -> bool:
    """Compare two version strings the way a human would expect."""
    if not current:
        return True
    if not candidate:
        return False

    def key(value: str) -> list:
        parts = re.split(r"[.\-+]", value)
        out: list = []
        for part in parts:
            if part.isdigit():
                out.append((0, int(part), ""))
            else:
                out.append((1, 0, part))
        return out

    return key(candidate) > key(current)


def download_release(release: Release, backend, *, reporter=None) -> tuple[Path, bool]:
    """Download the right asset, verify its checksum, return (path, verified)."""
    suffix = backend.asset_suffix()
    url = release.asset(suffix)
    if not url:
        raise CraftError(f"{release.name} {release.version} has no {suffix}")
    filename = url.rsplit("/", 1)[-1]
    destination = DOWNLOAD_DIR / filename

    if destination.exists() and destination.stat().st_size > 0:
        if reporter:
            reporter(f"Using already downloaded {filename}")
    else:
        if reporter:
            reporter(f"Downloading {filename}…")

        def progress(done: int, total: int) -> None:
            if reporter and total:
                percent = done * 100 // total
                reporter(f"  {filename}: {percent}% "
                         f"({done / 1e6:.0f}/{total / 1e6:.0f} MB)", replace=True)

        download(url, destination, progress=progress)

    verified = False
    sums_url = release.asset("SHA256SUMS.txt")
    if sums_url:
        sums = fetch_checksums(sums_url)
        expected = sums.get(filename)
        if expected:
            actual = sha256_of(destination)
            if actual != expected:
                destination.unlink(missing_ok=True)
                raise CraftError(
                    f"Checksum mismatch for {filename}.\n"
                    f"  expected: {expected}\n  got:      {actual}"
                )
            verified = True
            if reporter:
                reporter("Checksum verified (SHA-256)")
        elif reporter:
            reporter("No checksum published for this file")
    return destination, verified
