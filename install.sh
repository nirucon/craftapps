#!/usr/bin/env bash
# Installer for craft-suite: puts the tool in ~/.local/bin so the TUI,
# the shell command and the autoupdate timer all use the same copy.
set -euo pipefail

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
SHARE_DIR="${HOME}/.local/share/craft-suite"
APPLICATIONS="${HOME}/.local/share/applications"

if [[ ${EUID} -eq 0 ]]; then
  echo "Run this as your normal user, not with sudo." >&2
  echo "Tarball installs go into your home directory and need no root rights." >&2
  exit 1
fi

command -v python3 >/dev/null || { echo "python3 is missing." >&2; exit 1; }

major="$(python3 -c 'import sys; print(sys.version_info.major)')"
minor="$(python3 -c 'import sys; print(sys.version_info.minor if sys.version_info.major == 3 else 0)')"
if [[ ${major} -ne 3 || ${minor} -lt 10 ]]; then
  echo "Python 3.10 or newer is required (found ${major}.${minor})." >&2
  exit 1
fi

mkdir -p "${BIN_DIR}" "${SHARE_DIR}" "${APPLICATIONS}"

install -m 0644 "${SOURCE_DIR}/scripts/craftlib.py" "${SHARE_DIR}/craftlib.py"
install -m 0644 "${SOURCE_DIR}/scripts/crafttui.py" "${SHARE_DIR}/crafttui.py"
install -m 0755 "${SOURCE_DIR}/scripts/craft-suite.py" "${SHARE_DIR}/craft-suite.py"

# The launcher is a thin wrapper: it finds the modules next to itself.
cat > "${BIN_DIR}/craft-suite" <<EOF
#!/usr/bin/env bash
exec python3 "${SHARE_DIR}/craft-suite.py" "\$@"
EOF
chmod 0755 "${BIN_DIR}/craft-suite"

cat > "${APPLICATIONS}/craft-suite.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=ArtCraft Suite
Comment=Install and update the Craft apps
Exec=${BIN_DIR}/craft-suite
Terminal=true
Icon=system-software-install
Categories=System;PackageManager;
Keywords=artcraft;craft;install;update;
EOF
chmod 0644 "${APPLICATIONS}/craft-suite.desktop"

if command -v update-desktop-database >/dev/null; then
  update-desktop-database "${APPLICATIONS}" 2>/dev/null || true
fi

printf '\n  ✓ craft-suite installed\n\n'
printf '    Command:    %s/craft-suite\n' "${BIN_DIR}"
printf '    Modules:    %s\n' "${SHARE_DIR}"
printf '    Menu entry: %s/craft-suite.desktop\n\n' "${APPLICATIONS}"

case ":${PATH}:" in
  *":${BIN_DIR}:"*) ;;
  *) printf '    Note: %s is not on your PATH.\n' "${BIN_DIR}"
     printf '          Add: export PATH="%s:$PATH"\n\n' "${BIN_DIR}" ;;
esac

printf '    Start with:  craft-suite\n\n'
