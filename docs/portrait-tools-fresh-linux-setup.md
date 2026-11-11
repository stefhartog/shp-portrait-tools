# Get SHP Portrait Tools running on a fresh Linux computer

**Beginner setup guide - 11 November 2026**

We will build this in small pieces: get **Skin Tools** working, then add
**AI Fill** if you want image generation, and **PhotoCraft** if you want editor
integration. Think of Tools as the workbench, models as its instruction books,
and the graphics card as the heavy-lifting machine.

**Important:** this is a source-based setup, not a one-click installer. No tested
Linux distribution/version or minimum graphics-card specification is published.
The current name and Patch Fill features are described in the coding session's
updated canonical docs and uncommitted source, based on commit `6e23398`.
Cloning that commit alone will not give you those later features.
The maintainer must supply the matching updated source or identify its eventual
release. This guide does not claim a fresh computer has been tested end to end.

## 1. Check your computer before installing

You need a Linux desktop, internet access for downloads, permission to install
system packages, and access to the **private** Tools repository/current source.
The commands below assume a normal Bash-style terminal and default home-directory
locations. `$HOME` means your home folder, not someone else's `/home/stef`.

**For Skin Tools, first confirm a compatible NVIDIA GPU and driver.** The pose
analysis explicitly uses CUDA; CPU-only, AMD and Intel Skin Tools operation are
not guaranteed. Use your distribution's official driver tool/instructions, then
reboot if instructed. Do not copy a random driver-install command.

```sh
nvidia-smi
```

**Check:** it should list your GPU and driver rather than report that it cannot
communicate with the driver. This alone does not prove Tools' inference will work.
Tools' GPU extra provides CUDA 13/cuDNN libraries, **not the driver**. If your
computer cannot support that combination, resolve it with the maintainer first.
No minimum RAM/VRAM is promised; generation models are large downloads.

| Part | When you need it |
| --- | --- |
| Python 3.13, uv, Tools source/dependencies | Required for Tools |
| Skin models and compatible inference hardware | Required for Skin Mask, FS & Tone, Dodge & Burn and Combine |
| GTK3, WebKit2-4.1, AppIndicator3 and desktop/tray support | Required for the native window and its file pickers |
| ComfyUI, its GPU/PyTorch environment, models and nodes | Required for Crop & Expand/Patch Fill generation; not local skin retouching |
| PhotoCraft with authenticated automation | Optional: Get from and Send to the editor |
| ExifTool/Perl | Optional: Sony autofocus-point subject selection |

Windows support is not implemented or verified. Do not use this as a Windows guide.

## 2. Install the small system tools

Open **Terminal**. Copy only the commands inside code blocks; do not type a `$`
prompt. `sudo` asks for your Linux password, not your GitHub password. Password
characters usually do not appear while typing; that is normal.

**Debian/Ubuntu-family example only:**

```sh
sudo apt update
sudo apt install git curl xdg-user-dirs desktop-file-utils
```

This refreshes the package list and installs downloading/checkout tools plus the
desktop utilities used by the launcher installer. Review any install prompt.
For Fedora, Arch or another distribution, use its documented package equivalents;
do not run `apt` there.

For the native window, README lists these Debian-family package names. Check
availability **before** installing:

```sh
apt-cache show python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1 gir1.2-appindicator3-0.1
```

If all four are available for your distribution, install them:

```sh
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1 gir1.2-appindicator3-0.1
```

**Stop if a package is missing.** An Ayatana-only binding is not automatically
interchangeable with AppIndicator3. Ask for the right native-runtime route for
your distro rather than adding random repositories. GNOME generally also needs
an AppIndicator tray extension; follow your desktop's supported extension setup.
This is a compatibility checkpoint, not a claim every Ubuntu version works.

## 3. Install uv, then get the right Tools source

uv manages Python and keeps Tools' packages in their own folder. Its
[official installer](https://docs.astral.sh/uv/getting-started/installation/)
can be downloaded and inspected before running:

```sh
curl -LsSf https://astral.sh/uv/install.sh -o "$HOME/uv-install.sh"
```

Open `uv-install.sh` in your text editor. Only run it if you trust the official
source and are comfortable installing it:

```sh
sh "$HOME/uv-install.sh"
```

Do not use sudo here. Follow its PATH instructions, then open a new terminal:

```sh
uv --version
git --version
```

**Check:** both print version numbers.

If the maintainer has published the required source to the private repository:

```sh
git clone https://github.com/stefhartog/shp-photocraft-tools.git "$HOME/shp-photocraft-tools"
cd "$HOME/shp-photocraft-tools"
git log -1 --oneline
```

You need GitHub access; use GitHub's supported authentication without putting
tokens in commands or URLs. Do not clone over an existing folder. If you received
a source archive instead, unpack it into a new folder and open Terminal there.

**Check with the maintainer:** this source must contain the current rename and
Patch Fill implementation, not merely the older committed baseline. There is no
published newer commit identifier to paste into this guide. Keep this folder
after installation: the desktop launcher points to its Python environment.

Now, from that Tools folder:

```sh
uv python install 3.13
uv sync --locked --extra gpu
```

The first command gets Python 3.13; the second installs locked dependencies and
NVIDIA user-space libraries into `.venv`. Do not replace this with system-wide
`sudo pip install`: uv's ONNX dependency resolution matters.

```sh
uv run --no-sync python --version
```

**Check:** Python reports 3.13. If dependency installation failed, fix that error
before continuing. A successful install does not yet test the GPU/models.

## 4. Get Skin Tools' three model files

Models are **not bundled**, and the desktop installer does not download them.
Create this folder:

```sh
mkdir -p "$HOME/.local/share/shp-photocraft-tools/dnb/models"
```

Use the pinned download links in [MODELS](../MODELS.md#db-three-manually-supplied-onnx-files).
Save the actual binary files here, not a web page or Git LFS pointer:

| Exact saved name | What to download |
| --- | --- |
| `yunet.onnx` | The linked YuNet binary; rename its longer upstream filename |
| `face_landmarks.onnx` | The linked face-landmarks ONNX conversion |
| `selfie_multiclass_256x256.onnx` | The linked skin-segmentation ONNX conversion |

The links point to specific conversions: similarly named models are not substitutes.
Check the downloads with the documented hashes:

```sh
cd "$HOME/.local/share/shp-photocraft-tools/dnb/models"
sha256sum -c <<'EOF'
8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4  yunet.onnx
f38c3321ceffbc9e95103480ad38cc3f52e7e1bde2bcee7cd9355d0b9138ac0c  face_landmarks.onnx
13961350bb2099688eb96182ffe26f24eb12c77b1df278a2ec95bbdc923136e1  selfie_multiclass_256x256.onnx
EOF
```

**Check:** all three say **OK**. A mismatch means stop and check the download.
Skin Tools also downloads separate pose weights on its first analysis; keep
internet available. For offline use, follow MODELS' explicit pose-prefetch
instructions before disconnecting. Those weights use a separate rtmlib cache.

## 5. Add the desktop launcher and try one picture

Go back to Tools and install its **per-user** launcher:

```sh
cd "$HOME/shp-photocraft-tools"
uv run --no-sync python scripts/install_desktop.py \
  --models "$HOME/.local/share/shp-photocraft-tools/dnb/models"
```

This creates the Tools shortcut, icons and `launcher.json`, using existing models.
It does not install PhotoCraft, ComfyUI or a GPU driver. It refuses to overwrite
different existing Tools files: read that error instead of deleting things.

Check the native bindings without opening the window:

```sh
/usr/bin/python3 src/shp_photocraft_tools/desktop/controller.py \
  --url http://127.0.0.1:18760/ \
  --data-dir "${XDG_DATA_HOME:-$HOME/.local/share}/shp-photocraft-tools/webview" --check
```

**Check:** this exits successfully, not with a missing-GI/namespace error.
Then start:

```sh
"$HOME/.local/bin/shp-photocraft-tools"
```

You can later use the app-menu shortcut; a desktop shortcut may need right-click
**Allow Launching**. Keep a tray available. The shared window should start on
**Skin Mask** when Skin Tools is running.

With PhotoCraft disconnected, click **Get Image** to select a local portrait.
Wait for analysis, including possible first-time pose downloads. Check Mask and
Analysis, then try **FS & Tone**, **Dodge & Burn** and **Combine**. They share one
source. **Send Result** for a local source saves a numbered result beside it;
the **Save** beside the preset selector saves settings, not the picture.

**Check:** a portrait analyzes, previews display, and a local result saves.
Opening a window alone is not the success test. AI Fill can remain unavailable
until ComfyUI is prepared; it does not block the skin workflow. To hide its tabs,
stop AI Fill using the tray service control.

## 6. Optional: add AI Fill generation

Skip this if you only want skin retouching. **Both AI Fill tabs need external
ComfyUI**, and Patch Fill needs FLUX.1 Fill. Tools does not install it for you.

### Prepare ComfyUI separately

Follow [ComfyUI's current manual installation guide](https://docs.comfy.org/installation/manual_install).
Choose the GPU/PyTorch instructions matching your actual driver/hardware, not
nightly packages by default. Its environment must be separate from Tools'.
For Tools-managed startup, ComfyUI must have `main.py` and a Python executable
at **`venv/bin/python` or `.venv/bin/python` inside its checkout**.
A Conda-only environment elsewhere will not satisfy that launcher check.
Have the maintainer help choose compatible ComfyUI/Python/PyTorch versions if
needed; no tested version combination for a fresh machine is recorded.

**Conditional NVIDIA example:** if Python 3.13 and the upstream CUDA 13 PyTorch
build are approved for your machine, this puts ComfyUI in its own environment.
Do not run it over an existing ComfyUI folder:

```sh
git clone https://github.com/Comfy-Org/ComfyUI.git "$HOME/ComfyUI"
cd "$HOME/shp-photocraft-tools"
uv run --no-sync python -m venv "$HOME/ComfyUI/venv"
"$HOME/ComfyUI/venv/bin/python" -m pip install torch torchvision torchaudio \
  --extra-index-url https://download.pytorch.org/whl/cu130
"$HOME/ComfyUI/venv/bin/python" -m pip install -r "$HOME/ComfyUI/requirements.txt"
cd "$HOME/ComfyUI"
venv/bin/python main.py
```

The Torch command is the NVIDIA example in ComfyUI's current manual guide, not
a hardware guarantee. If it no longer matches that guide or your approved driver,
use the matching upstream command instead. Keep this terminal open while testing;
Ctrl+C stops your manually started server after its work has finished.

Use a folder such as `$HOME/ComfyUI`. Install ComfyUI's requirements and custom-node
requirements with **ComfyUI's Python**, not Tools' Python or system sudo pip.
Before connecting Tools, follow the upstream startup instructions and confirm
ComfyUI opens at `http://127.0.0.1:8188/`.

### Install the required nodes and models

Use the exact sources and installation steps in
[MODELS](../MODELS.md#crop-comfyui-models-and-nodes):

- Both methods need **comfyui-inpaint-nodes** (`INPAINT_ColorMatch`) and the
  included **seam-color-correct** node (`SeamColorCorrect`).
- FLUX.2 additionally needs **ComfyUI-GGUF** (`UnetLoaderGGUF`) and a compatible
  ComfyUI revision. Tools constructs its graphs; no workflow JSON install is needed.
- Install each node's requirements in ComfyUI's environment. Check upstream
  model access/licensing, especially FLUX.1 Fill's non-commercial terms.

For a new `$HOME/ComfyUI` installation, copy the included seam node from Tools:

```sh
cd "$HOME/shp-photocraft-tools"
COMFY_DIR="$HOME/ComfyUI"
test -d "$COMFY_DIR/custom_nodes" &&
  test ! -e "$COMFY_DIR/custom_nodes/seam-color-correct" &&
  cp -R comfyui/custom_nodes/seam-color-correct "$COMFY_DIR/custom_nodes/"
```

This intentionally refuses an existing destination. If it does not copy, inspect
the path or existing node; do not overwrite blindly. Restart your manually started
ComfyUI when idle, then check:

```sh
curl -fsS http://127.0.0.1:8188/object_info/SeamColorCorrect
```

**Check:** JSON contains `SeamColorCorrect`, not `{}` or a connection error.

| Method | Default files and ComfyUI folders |
| --- | --- |
| FLUX.1 Fill: Patch Fill and optional expansion | `flux1-fill-dev.safetensors` in `models/diffusion_models/`; `clip_l.safetensors` and `t5xxl_fp8_e4m3fn_scaled.safetensors` in `models/text_encoders/`; `ae.safetensors` in `models/vae/` |
| FLUX.2: default expansion | `flux2-dev-q3_k_m.gguf` in `models/unet/`; `mistral_3_small_flux2_fp8.safetensors` in `models/text_encoders/`; `flux2-vae.safetensors` in `models/vae/` |
| FLUX.2 Turbo option | `Flux_2-Turbo-LoRA_comfyui.safetensors` in `models/loras/`, required when turbo lora is on |
| Neural upscale option | `4xNomos8k_atd_jpg.safetensors` in `models/upscale_models/`, or another installed selected model |

Download from MODELS' linked sources; match filenames/case or select the actual
installed name in the UI. **Lanczos (no model)** skips neural upscaling, not the
generation models. Fill defaults to Lanczos; FLUX.2 defaults to a neural upscaler.

### Connect AI Fill

For a simple standalone check while your manual ComfyUI runs on 8188:

```sh
cd "$HOME/shp-photocraft-tools"
uv run --no-sync shp-photocraft-crop --dir "$HOME/Pictures" \
  --comfy http://127.0.0.1:8188
```

Use an existing pictures folder. Open `http://127.0.0.1:8768/`; standalone Patch
Fill is `http://127.0.0.1:8768/?mode=patch`. Use the local file list; a plain browser
does not have the native chooser. Keep the terminal running; Ctrl+C stops this
standalone service when its work is finished.

For the **shared desktop**, save wanted work and Quit Tools first. Open its
installer-created `~/.config/shp-photocraft-tools/launcher.json` in a text editor
(or your custom XDG config location). Add this top-level property while preserving
the existing models/ports and valid JSON:

```json
"comfy": {"directory": "/absolute/path/to/ComfyUI", "port": 18788}
```

Replace the path with your actual full path: JSON does not expand `$HOME` or `~`.
This is a property to insert, **not an entire configuration file**; add the
necessary comma between properties. The README has a complete config example.
Rerunning the installer with `--comfy-dir` against a different existing config is
refused, so do not use that as a silent upgrade.

Restart Tools. It starts its own ComfyUI on **18788**, not the external 8188 server.
Stop the manually started ComfyUI normally if no longer needed, so two instances
do not compete for GPU memory. The managed instance reuses its existing models
but keeps separate Tools working folders; it does not adopt arbitrary servers.

Try **Crop & Expand** with a little space outside the original frame, or
**Patch Fill** with a painted area. Check readiness, Generate one variation,
then inspect the result. Patch Fill does not resize the canvas. Missing
nodes/models should be reported before Generate; readiness still does not prove
the model fits your GPU memory.

## 7. Optional: connect PhotoCraft

First get local Tools working. Install a compatible PhotoCraft build separately;
this repository does not provide its installation. You need an open document,
authenticated control, a matching token file and permitted read/exchange folders.
A GUI control-port setting alone is not enough.

Use [README's PhotoCraft setup](../README.md#photocraft-connection) for the exact
launch command and environment variables. Its `$HOME` read-root example grants
access broadly; choose an appropriate allowed pictures folder deliberately.
The exchange folder must be writable. Protect the token and never share it.
Start Tools from the same configured terminal; app-menu launches do not inherit
exports from an unrelated terminal.

Close an incompatible existing PhotoCraft normally before launching its
automation-enabled instance; save your work and do not launch duplicates.
The direct `--control`/automation-root command opts out of normal single-instance
file forwarding. **Do not use it as LightCraft's external-editor target.**

**Check:** with the intended PhotoCraft document active, Get Image fetches it.
Send Result adds a flattened correction; FS & Tone/Dodge & Burn/Combine can
Send Layers for editable ingredients/masks. A local source still saves locally.
An authentication error is not a successful connection or a local-file fallback.
Inspect PhotoCraft before retrying an uncertain send.

### If you also use LightCraft

The optional helper installer is **not a fresh PhotoCraft installer**. It requires
the existing recognized PhotoCraft desktop entry/executable and LightCraft settings,
and reuses the existing authenticated configuration. It refuses custom/conflicting
targets and makes backups.

Only after those prerequisites are ready, from Tools:

```sh
uv run --no-sync python scripts/install_editor_handoff.py \
  --backup-dir "$HOME/portrait-tools-handoff-backup" --defer-editor-setting
```

That backup directory must not already exist. This deliberately edits PhotoCraft's
recognized shortcut and installs `photocraft-dnb`/`photocraft-lightcraft`; it is not
required for ordinary Tools use. In running LightCraft, set **Settings > External
Editing** to the **full path** to `~/.local/bin/photocraft-lightcraft`, without
quotes, flags or `%F`. Follow README's close/reopen order and rollback instructions.
The helper does not silently start PhotoCraft for an external-editor request.

## 8. If something goes wrong

| Problem | What to check |
| --- | --- |
| Private clone fails/current tabs missing | Confirm GitHub access and that the maintainer supplied the updated source; old `6e23398` alone lacks later features. |
| `uv: command not found` | Open a new terminal and follow the uv installer's PATH instructions. |
| NVIDIA/CUDA/model failure | Check driver compatibility, locked GPU dependencies, three model hashes and pose downloads. Do not assume CPU fallback will work. |
| Missing GTK/WebKit/AppIndicator | Recheck exact system namespaces/packages and tray support for your distro. The bindings use `/usr/bin/python3`, not the uv environment. |
| `xdg-user-dir`/desktop-folder error | Install the desktop utility. If Desktop resolves to your home folder, create/choose a distinct desktop folder and use installer's `--desktop-dir` with its absolute path. |
| Installer refuses existing files | Review the named file; it is preventing an overwrite. Do not delete your settings or rerun as root to bypass it. |
| Skin/AI Fill tabs disappear | A stopped service hides its tabs. Start it from the tray; Skin Tools owns four tabs and AI Fill owns two. |
| AI Fill cannot Generate | Check painted mask or outward frame area, ComfyUI readiness, selected weights/nodes and GPU memory. |
| Port already occupied | Close the specific app you started normally or choose distinct documented config ports; do not kill unrelated processes. |
| PhotoCraft reachable but Get fails | TCP reachability is not authentication. Check token, automation roots and active document. |

Native window **X** normally hides to the tray; use **Show** to return and **Quit**
to stop. Save/send wanted work first; unsaved browser state is not automatically
recovered. Log details are in **Skin Mask > Image analysis > Log**; timings are in
**Settings & Info**. Share exact errors, launch mode and versions with the maintainer,
but remove tokens and private photo information.

**Sources checked:** current canonical README, HANDOFF and MODELS; desktop/handoff
installer scripts; launcher, ComfyUI-runtime checks and authenticated handoff code;
official uv and ComfyUI installation guides. No driver, software, model or app was
installed/run to write this document. Still to confirm for your machine: distro
version/package availability, GPU/driver compatibility, current source delivery,
and a compatible ComfyUI/PyTorch environment.
