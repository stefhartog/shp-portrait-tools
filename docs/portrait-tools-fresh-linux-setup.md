# Get SHP Portrait Tools running on a fresh Linux computer

**Beginner setup guide - 21 November 2026**

We build this in small pieces: first get **Skin Tools** working on its own (Standalone), then connect
**PhotoCraft** if you want to edit in it. Think of Tools as the workbench, the model files as its instruction
books, and the graphics card as the heavy-lifting machine.

**Quicker:** since v0.2.0 one command does all of this (README → Install):
`curl -LsSf https://github.com/stefhartog/shp-portrait-tools/releases/latest/download/install.sh | sh`.
This guide is the step-by-step, source-based way. It was last walked through on Fedora with GNOME
(Wayland) and an NVIDIA GPU; other distributions should work the same way but are not tested, and no
minimum graphics card is published.

Not covered here:

- **AI Fill** (Crop & Expand, Patch Fill) is a separate app now:
  [SHP AI Fill](https://github.com/stefhartog/shp-ai-fill), with its own setup.
- **Windows**: see the [Windows notes](windows-handoff.md).

## 1. Check your computer

You need a Linux desktop session, internet access for downloads and permission to install packages. The
commands assume a normal Bash terminal; `$HOME` means your own home folder.

**Skin Tools needs a compatible NVIDIA GPU and driver.** The pose analysis uses CUDA; CPU-only, AMD and
Intel operation are not guaranteed. Install the driver with your distribution's official tool or
instructions (reboot if asked), then check:

```sh
nvidia-smi
```

**Check:** it lists your GPU and driver instead of saying it cannot talk to the driver. Tools brings its own
CUDA 13/cuDNN libraries, **not the driver**.

| Part | When you need it |
| --- | --- |
| Git, curl, Python 3.13 and uv | Always |
| NVIDIA GPU, driver and the three Skin Tools model files | Always (Skin Mask, FS & Tone, Dodge & Burn, Combine) |
| A tray (on GNOME: the AppIndicator extension) | For the tray icon; without one, closing the window quits Tools |
| PhotoCraft with its control server on | Optional: Get Image from and Send Layers to PhotoCraft |
| ExifTool/Perl | Optional: Sony autofocus-point subject selection |

The desktop window needs no system GUI packages: it uses pywebview with Qt WebEngine, which is installed into
Tools' own Python environment in step 3.

The optional pose backends (**Google Full**, **OpenCV Zoo MediaPipe**) are experimental; keep the default
while following this guide. See [MODELS](../MODELS.md#optional-google-pose-landmarker-full-experimental)
before changing it.

## 2. Install the small system tools

Open **Terminal**. Copy only the commands inside code blocks. `sudo` asks for your Linux password (typing
does not show on screen; that is normal).

Fedora:

```sh
sudo dnf install git curl xdg-user-dirs desktop-file-utils
```

Debian/Ubuntu:

```sh
sudo apt update
sudo apt install git curl xdg-user-dirs desktop-file-utils
```

For other distributions, install the same tools with their package manager.

**On GNOME, add a tray.** GNOME has no tray icons by default. Install the AppIndicator extension (Fedora
and Debian/Ubuntu call the package `gnome-shell-extension-appindicator`), log out and in, and enable it in
the Extensions app. KDE and most other desktops have a tray already.

## 3. Install uv and Tools

uv manages Python and keeps Tools' packages in their own folder. Download its
[official installer](https://docs.astral.sh/uv/getting-started/installation/), read it, and run it
without `sudo`:

```sh
curl -LsSf https://astral.sh/uv/install.sh -o "$HOME/uv-install.sh"
sh "$HOME/uv-install.sh"
```

Follow its PATH instructions, then open a new terminal and check:

```sh
uv --version
git --version
```

**Check:** both print version numbers.

Clone Tools (the repository is public; no sign-in needed). Do not clone over an existing folder:

```sh
git clone https://github.com/stefhartog/shp-portrait-tools.git "$HOME/shp-portrait-tools"
cd "$HOME/shp-portrait-tools"
uv python install 3.13
uv sync --locked --extra gpu
```

This installs Python 3.13 and Tools' locked dependencies into `.venv`, including the NVIDIA user-space
libraries (`--extra gpu`) and the desktop window (PySide6/Qt WebEngine). Keep this folder: the desktop
launcher runs Tools from it. Do not use `sudo pip install` instead.

```sh
uv run --no-sync python --version
```

**Check:** it reports Python 3.13. If `uv sync` failed, fix that error before continuing.

## 4. Get Skin Tools' three model files

The models are **not bundled** and nothing downloads them for you. Create the folder:

```sh
mkdir -p "$HOME/.local/share/shp-portrait-tools/skin/models"
```

Download the files from the pinned links in [MODELS](../MODELS.md#db-three-onnx-files)
and save them there under exactly these names (the binary files, not a web page or Git LFS pointer):

| Saved name | What it is |
| --- | --- |
| `yunet.onnx` | Face detection (YuNet); rename the longer upstream filename |
| `face_landmarks.onnx` | Face landmarks |
| `selfie_multiclass_256x256.onnx` | Skin segmentation |

Similarly named models are not substitutes. Check them:

```sh
cd "$HOME/.local/share/shp-portrait-tools/skin/models"
sha256sum -c <<'EOF'
8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4  yunet.onnx
f38c3321ceffbc9e95103480ad38cc3f52e7e1bde2bcee7cd9355d0b9138ac0c  face_landmarks.onnx
13961350bb2099688eb96182ffe26f24eb12c77b1df278a2ec95bbdc923136e1  selfie_multiclass_256x256.onnx
EOF
```

**Check:** all three say **OK**. A mismatch means the download is wrong; get it again.

Skin Tools also downloads pose weights on its first analysis, so keep internet available then. For offline
use, prefetch them first as described in [MODELS](../MODELS.md#db-automatically-downloaded-pose-weights-and-offline-use).

## 5. Install the desktop launcher and try one picture

```sh
cd "$HOME/shp-portrait-tools"
uv run --no-sync python scripts/install_desktop.py \
  --models "$HOME/.local/share/shp-portrait-tools/skin/models"
```

Add `--exiftool /path/to/exiftool` if you use ExifTool. This creates the **SHP Portrait Tools** app-menu
entry, a Desktop shortcut, the icons, the `~/.local/bin/shp-photocraft-tools` launcher and
`~/.config/shp-portrait-tools/launcher.json`. (Some of these files keep the app's older
`shp-photocraft-tools` name; that is expected.) It never overwrites a different existing file: if it
refuses, read which file it names and decide what to do with that file instead of deleting things.

**Check:** the installer ends without a "Desktop window runtime unavailable" warning. Then start Tools from
the app menu, or:

```sh
"$HOME/.local/bin/shp-photocraft-tools"
```

A Desktop shortcut may need right-click **Allow Launching** first. The window shows a short status page,
then Skin Tools with its tabs: Skin Mask, FS & Tone, Dodge & Burn, Combine and Settings & Info. A tray icon
appears too.

Try one picture without an editor:

1. In **Settings & Info**, set the connection to **Standalone**.
2. Click **Get Image** and choose a portrait. Wait for the analysis (the first one also downloads pose
   weights).
3. Look at the mask and analysis, then try **FS & Tone**, **Dodge & Burn** and **Combine**; they share one
   source.
4. Click **Send Result**: it saves a numbered result beside the original and never overwrites. (The **Save**
   beside the preset menu saves settings, not the picture.)

**Check:** the portrait analyses, previews appear and a result file is saved. The window opening is not the
success test on its own.

Closing the window (**X**) hides it to the tray; use the tray's **Show Tools** to bring it back and
**Quit Tools** to stop everything. Send or save wanted work first: unsent previews are not kept.

## 6. Optional: connect PhotoCraft

Tools controls a running PhotoCraft through PhotoCraft's **control server**. Install a native PhotoCraft
package (the `.rpm` or `.deb` from the PhotoCraft releases page) rather than the Flatpak: the Flatpak's
sandbox cannot see the token and exchange folders without extra permission grants.

**1. Turn on PhotoCraft's control server.** PhotoCraft reads these settings at start. Put them in a file
such as `~/.config/environment.d/photocraft-control.conf` so app-menu launches get them too (log out and in
afterwards), with your own folders:

```sh
PHOTOCRAFT_CONTROL_PORT=29766
PHOTOCRAFT_CONTROL_TOKEN_FILE=/home/you/.local/share/photocraft-control/control.token
PHOTOCRAFT_AUTOMATION_READ_ROOT=/home/you/.local/share/photocraft-control/exchange
PHOTOCRAFT_AUTOMATION_WRITE_ROOT=/home/you/.local/share/photocraft-control/exchange
```

Use full paths (no `~` or `$HOME`) and create the exchange folder. PhotoCraft creates the token file the first
time it starts. If another tool already set these up (for example a PhotoCraft plug-in), reuse its values:
a running PhotoCraft has only one control server, so everything that drives it must share it. Protect the
token file and never share it.

**2. Give Tools the same settings.** Quit Tools from the tray, then add a `photocraft` section to
`~/.config/shp-portrait-tools/launcher.json` (keep the existing entries and valid JSON; add a comma between
sections). All six keys are required, with full paths:

```json
"photocraft": {
  "port": 29766,
  "token_file": "/home/you/.local/share/photocraft-control/control.token",
  "exchange": "/home/you/.local/share/photocraft-control/exchange",
  "read_root": "/home/you/.local/share/photocraft-control/exchange",
  "executable": "/usr/bin/photocraft",
  "editor_config": "/home/you/.config/photocraft"
}
```

**3. Try it.** Start PhotoCraft and open a portrait, start Tools, and set the connection in
**Settings & Info** to **PhotoCraft**. The tray's PhotoCraft line shows whether the port is reachable.

**Check:** **Get Image** fetches the active PhotoCraft document, and **Send Layers** adds editable layers to
it (on the Skin Mask tab, the mask). An authentication error means the token or port does not match; a
reachable port alone does not prove the token is right. Check PhotoCraft before retrying an uncertain send.

More detail: README → [PhotoCraft connection](../README.md#photocraft-connection).

**LightCraft hand-off** (`scripts/install_editor_handoff.py`) is optional, edits PhotoCraft's and
LightCraft's launchers and has not been re-checked with the current desktop window; leave it out for a
fresh setup.

## 7. If something goes wrong

| Problem | What to check |
| --- | --- |
| `uv: command not found` | Open a new terminal; follow the uv installer's PATH instructions. |
| NVIDIA/CUDA/model failure | Driver compatibility (`nvidia-smi`), `uv sync --locked --extra gpu`, the three model hashes, and internet for the first pose download. Do not assume a CPU fallback. |
| "Desktop window runtime unavailable" | Run `uv sync --locked --extra gpu` in the Tools folder again. Meanwhile run `"$HOME/.local/bin/shp-photocraft-tools" --no-browser` and open `http://127.0.0.1:18760/` in a browser. |
| No tray icon; closing the window quits | Install and enable the AppIndicator extension on GNOME (section 2). |
| `xdg-user-dir`/Desktop folder error | Install `xdg-user-dirs`. If Desktop resolves to your home folder, pass the installer `--desktop-dir` with a distinct absolute folder. |
| Installer refuses existing files | Review the named file; it prevents an overwrite. Do not delete settings or rerun as root to get past it. |
| Port already in use | Another Tools may still run; quit it from its tray. Do not kill unrelated processes. |
| PhotoCraft reachable but Get Image fails | Token file, port and exchange folder must match PhotoCraft's control settings exactly; PhotoCraft needs an open document. |
| Light-coloured file dialogs | Tools uses the desktop's own file chooser through the XDG portal; check `xdg-desktop-portal` and your desktop's portal package are installed. |

Logs are in `~/.local/state/shp-portrait-tools/` (`desktop.log` is the window's), and analysis details in
**Skin Mask > Image analysis > Log**. When asking for help, share exact errors and versions, but remove tokens
and private photo information.
