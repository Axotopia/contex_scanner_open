# Ubuntu one-click launcher

Turns this checkout into a clickable app on the workstation, using the existing
`.venv`. Per-user, no sudo, no packaging:

```bash
./packaging/install.sh               # menu entry + icon + ~/Desktop shortcut
./packaging/install.sh --no-desktop  # menu entry + icon only
./packaging/install.sh --autostart   # also start at login
./packaging/install.sh --uninstall
```

Files:

| File | Purpose |
|---|---|
| `contex-studio` | launcher: start the server if needed, wait for `/api/status`, open the UI; `--status`, `--stop`, `--foreground`, `--no-open`, `--port N` |
| `contex-studio.desktop` | menu entry (installed with `Exec`/`TryExec` rewritten to `~/.local/bin/contex-studio`) |
| `contex-studio.svg` | scalable app icon, drawn from the in-app brand mark |
| `install.sh` | installs/symlinks the above into `~/.local/{bin,share}` and `~/Desktop` |

Installed locations: `~/.local/bin/contex-studio` (symlink), `~/.local/share/applications/contex-studio.desktop`, `~/.local/share/icons/hicolor/scalable/apps/contex-studio.svg`, `~/Desktop/contex-studio.desktop`.

Runtime state: `~/.local/state/contex-studio/` (log per day, `server.pid`).

Two operators sharing one account can both click the icon: the launcher detects a
running instance by probing `/api/status` and just opens the UI. `--stop` only
signals processes whose interpreter is this checkout's `.venv/bin/python` *and*
whose command line includes `-m ctx app`, so it can never kill an unrelated shell.
