#!/usr/bin/env bash
# Install (or remove) the Contex Studio launcher for the current user — no sudo.
#
#   ./packaging/install.sh              menu entry + icon + ~/Desktop shortcut
#   ./packaging/install.sh --no-desktop  menu entry + icon only
#   ./packaging/install.sh --autostart   also start the studio at login
#   ./packaging/install.sh --uninstall   remove everything this script installed
#
# Everything lands in the invoking user's XDG dirs, so a station where both
# operators use the same account needs exactly one run.
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
APPS_DIR="$DATA_DIR/applications"
ICON_DIR="$DATA_DIR/icons/hicolor/scalable/apps"
AUTOSTART_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
DESKTOP_FILE="$APPS_DIR/contex-studio.desktop"
ICON_FILE="$ICON_DIR/contex-studio.svg"
LAUNCHER_LINK="$BIN_DIR/contex-studio"
DESKTOP_SHORTCUT="$HOME/Desktop/contex-studio.desktop"

DO_DESKTOP=1
DO_AUTOSTART=0
MODE=install

for arg in "$@"; do
    case "$arg" in
        --no-desktop) DO_DESKTOP=0 ;;
        --autostart)  DO_AUTOSTART=1 ;;
        --uninstall)  MODE=uninstall ;;
        -h|--help)    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "install.sh: unknown option '$arg'" >&2; exit 2 ;;
    esac
done

if [ "$MODE" = uninstall ]; then
    rm -f "$DESKTOP_SHORTCUT" "$AUTOSTART_DIR/contex-studio.desktop" \
          "$DESKTOP_FILE" "$ICON_FILE" "$LAUNCHER_LINK"
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS_DIR" || true
    echo "Contex Studio launcher removed."
    exit 0
fi

[ -f "$HERE/contex-studio" ] || { echo "Launcher missing at $HERE/contex-studio" >&2; exit 1; }
[ -x "$ROOT/.venv/bin/python" ] || {
    echo "No virtualenv at $ROOT/.venv — create it first:" >&2
    echo "  python3 -m venv \"$ROOT/.venv\" && \"$ROOT/.venv/bin/pip\" install -r \"$ROOT/requirements.txt\"" >&2
    exit 1
}

chmod +x "$HERE/contex-studio"
mkdir -p "$APPS_DIR" "$ICON_DIR" "$BIN_DIR"

# Symlink (not copy) so `git pull` updates the launcher too.
ln -sfn "$HERE/contex-studio" "$LAUNCHER_LINK"
install -m 644 "$HERE/contex-studio.svg" "$ICON_FILE"
# Exec/TryExec must point at the installed command for this account.
sed "s#^Exec=.*#Exec=$LAUNCHER_LINK#; s#^TryExec=.*#TryExec=$LAUNCHER_LINK#" \
    "$HERE/contex-studio.desktop" >"$DESKTOP_FILE"
chmod 644 "$DESKTOP_FILE"

if command -v desktop-file-validate >/dev/null 2>&1; then
    desktop-file-validate "$DESKTOP_FILE" || echo "warning: desktop-file-validate complained (see above)" >&2
fi
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS_DIR" || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -f -t "$DATA_DIR/icons/hicolor" >/dev/null 2>&1 || true

if [ "$DO_DESKTOP" = 1 ] && [ -d "$HOME/Desktop" ]; then
    cp "$DESKTOP_FILE" "$DESKTOP_SHORTCUT"
    chmod +x "$DESKTOP_SHORTCUT"
    # GNOME 42+ only launches desktop files marked trusted; the user may still
    # need one right-click -> "Allow Launching".
    command -v gio >/dev/null 2>&1 && gio set "$DESKTOP_SHORTCUT" metadata::trusted true 2>/dev/null || true
fi

if [ "$DO_AUTOSTART" = 1 ]; then
    mkdir -p "$AUTOSTART_DIR"
    sed "s#^X-GNOME-Autostart-enabled=.*#X-GNOME-Autostart-enabled=true#" "$DESKTOP_FILE" \
        >"$AUTOSTART_DIR/contex-studio.desktop"
fi

echo "Installed:"
echo "  command   $LAUNCHER_LINK  ->  $HERE/contex-studio"
echo "  menu      $DESKTOP_FILE"
echo "  icon      $ICON_FILE"
[ "$DO_DESKTOP" = 1 ] && [ -f "$DESKTOP_SHORTCUT" ] && echo "  desktop   $DESKTOP_SHORTCUT"
[ "$DO_AUTOSTART" = 1 ] && echo "  autostart $AUTOSTART_DIR/contex-studio.desktop"
echo
echo "Try it:  contex-studio --status   |   click the Contex Studio icon"
