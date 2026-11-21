#!/bin/sh
# SHP Portrait Tools installer for Linux (current user, no root).
#
#   curl -LsSf https://github.com/stefhartog/shp-portrait-tools/releases/latest/download/install.sh | sh
#
# With options:
#
#   curl -LsSf https://github.com/stefhartog/shp-portrait-tools/releases/latest/download/install.sh | sh -s -- --version v0.2.0 --no-launch
#
# Run it again to update to the newest release. It:
#   1. checks for an NVIDIA GPU with at least 8 GB of memory and a CUDA 13 driver (580 or newer);
#   2. installs uv for the current user if it is missing;
#   3. downloads the release's source into the install folder (default ~/.local/share/shp-portrait-tools-app);
#   4. installs Python 3.13 and the locked dependencies (including the Qt desktop window) with uv;
#   5. downloads and verifies the models (see MODELS.md; they are not redistributed by this project);
#   6. installs the desktop launcher (scripts/install_desktop.py) and starts Tools.
# Settings, presets and models live in ~/.local/share and ~/.config/shp-portrait-tools and survive updates.
set -eu

REPO="stefhartog/shp-portrait-tools"
MARKER=".shp-portrait-tools-install"
MIN_MEMORY_MIB=7800   # 8 GB cards report slightly under 8192 MiB
MIN_DRIVER=580        # CUDA 13 user-space libraries
VERSION="${SHP_TOOLS_VERSION:-latest}"
INSTALL_DIR="${SHP_TOOLS_INSTALL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/shp-portrait-tools-app}"
LAUNCH=1
GPU_CHECK=1
SOURCE_DIR=""         # development: install from a local checkout (git archive) instead of a release
SOURCE_REF="HEAD"
EXIFTOOL=""

usage() {
    echo "Usage: install.sh [--version TAG] [--install-dir DIR] [--no-launch] [--skip-gpu-check] [--exiftool PATH]"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --version) VERSION="$2"; shift 2 ;;
        --install-dir) INSTALL_DIR="$2"; shift 2 ;;
        --no-launch) LAUNCH=0; shift ;;
        --skip-gpu-check) GPU_CHECK=0; shift ;;
        --exiftool) EXIFTOOL="$2"; shift 2 ;;
        --source-dir) SOURCE_DIR="$2"; shift 2 ;;
        --source-ref) SOURCE_REF="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

step() { printf '\033[36m==> %s\033[0m\n' "$1"; }
fail() { printf '\033[31mERROR: %s\033[0m\n' "$1" >&2; exit 1; }

check_gpu() {
    command -v nvidia-smi >/dev/null 2>&1 ||
        fail "No NVIDIA driver found (nvidia-smi is missing). Install your distribution's current NVIDIA driver and run this again."
    lines=$(nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits 2>/dev/null) ||
        fail "nvidia-smi could not read the GPU. Reinstall or update the NVIDIA driver."
    best=$(printf '%s\n' "$lines" | sort -t, -k2 -n -r | head -n 1)
    name=$(printf '%s' "$best" | cut -d, -f1 | sed 's/^ *//;s/ *$//')
    memory=$(printf '%s' "$best" | cut -d, -f2 | tr -d ' ')
    driver=$(printf '%s' "$best" | cut -d, -f3 | tr -d ' ')
    major=${driver%%.*}
    echo "    GPU: $name, $memory MiB, driver $driver"
    [ "$memory" -ge "$MIN_MEMORY_MIB" ] ||
        fail "SHP Portrait Tools needs an NVIDIA GPU with at least 8 GB of memory; $name has $memory MiB."
    [ "$major" -ge "$MIN_DRIVER" ] ||
        fail "NVIDIA driver $driver is too old; version $MIN_DRIVER or newer is needed (CUDA 13)."
}

find_uv() {
    if command -v uv >/dev/null 2>&1; then command -v uv; return; fi
    if [ ! -x "$HOME/.local/bin/uv" ]; then
        step "Installing uv for the current user" >&2
        curl -LsSf https://astral.sh/uv/install.sh | sh >&2
    fi
    [ -x "$HOME/.local/bin/uv" ] || fail "uv was not installed to $HOME/.local/bin/uv."
    echo "$HOME/.local/bin/uv"
}

resolve_tag() {
    if [ -n "$SOURCE_DIR" ]; then echo "local-$(git -C "$SOURCE_DIR" rev-parse --short HEAD)"; return; fi
    if [ "$VERSION" != "latest" ]; then echo "$VERSION"; return; fi
    tag=$(curl -fsSL -H "User-Agent: shp-portrait-tools-installer" "https://api.github.com/repos/$REPO/releases/latest" |
        sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n 1)
    [ -n "$tag" ] || fail "Could not find the latest release of $REPO."
    echo "$tag"
}

install_source() {
    tag="$1"
    temp=$(mktemp -d)
    trap 'rm -rf "$temp"' EXIT
    if [ -n "$SOURCE_DIR" ]; then
        step "Packing the local checkout $SOURCE_DIR"
        git -C "$SOURCE_DIR" archive --format=tar.gz --prefix=local/ -o "$temp/source.tar.gz" "$SOURCE_REF"
    else
        step "Downloading SHP Portrait Tools $tag"
        curl -fsSL -o "$temp/source.tar.gz" "https://github.com/$REPO/archive/refs/tags/$tag.tar.gz"
    fi
    mkdir "$temp/x"
    tar -xzf "$temp/source.tar.gz" -C "$temp/x"
    set -- "$temp"/x/*
    [ $# -eq 1 ] && [ -f "$1/pyproject.toml" ] || fail "Unexpected archive layout for $tag."
    source="$1"
    if [ -d "$INSTALL_DIR" ]; then
        if [ ! -f "$INSTALL_DIR/$MARKER" ] && [ -n "$(ls -A "$INSTALL_DIR")" ]; then
            fail "$INSTALL_DIR exists and was not made by this installer. Choose another folder with --install-dir."
        fi
        if pgrep -f "$INSTALL_DIR/.venv/bin/python" >/dev/null 2>&1; then
            fail "SHP Portrait Tools is running from $INSTALL_DIR. Save your work, choose Quit Tools in its tray menu, then run this again."
        fi
        # Replace the previous release's files; keep the environment so uv only applies the differences.
        find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 ! -name .venv -exec rm -rf {} +
    else
        mkdir -p "$INSTALL_DIR"
    fi
    cp -a "$source"/. "$INSTALL_DIR"/
    printf '%s\n' "$tag" > "$INSTALL_DIR/$MARKER"
    rm -rf "$temp"
    trap - EXIT
}

[ "$(uname -s)" = "Linux" ] || fail "This installer is for Linux; on Windows use install.ps1."
[ "$(uname -m)" = "x86_64" ] || fail "SHP Portrait Tools needs a 64-bit x86 Linux system."
for tool in curl tar; do command -v "$tool" >/dev/null 2>&1 || fail "$tool is required."; done

step "Checking the system"
if [ "$GPU_CHECK" -eq 1 ]; then check_gpu; else echo "    GPU check skipped: Skin Tools analysis needs an NVIDIA GPU."; fi
UV=$(find_uv)
TAG=$(resolve_tag)
install_source "$TAG"

step "Installing Python 3.13 and dependencies (first run downloads a few GB)"
"$UV" python install 3.13 --no-bin
"$UV" --directory "$INSTALL_DIR" sync --locked --extra gpu

CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/shp-portrait-tools/launcher.json"
MODELS="${XDG_DATA_HOME:-$HOME/.local/share}/shp-portrait-tools/skin/models"
if [ -f "$CONFIG" ]; then
    configured=$("$INSTALL_DIR/.venv/bin/python" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("models") or "")' "$CONFIG")
    [ -n "$configured" ] && MODELS="$configured"
fi
step "Downloading and verifying models"
"$UV" --directory "$INSTALL_DIR" run --no-sync python -m shp_portrait_tools.fetch_models --dest "$MODELS"

step "Installing the desktop launcher"
set -- --models "$MODELS"
[ -n "$EXIFTOOL" ] && set -- "$@" --exiftool "$EXIFTOOL"
if ! "$UV" --directory "$INSTALL_DIR" run --no-sync python scripts/install_desktop.py "$@"; then
    printf '\033[33m    The launcher was not installed (see above). An older install'"'"'s launcher files may be in the way;\n    rename the file it names and run this again.\033[0m\n'
    LAUNCH=0
fi

printf '\n\033[32mSHP Portrait Tools %s is installed in %s\033[0m\n' "$TAG" "$INSTALL_DIR"
if [ "$LAUNCH" -eq 1 ]; then
    step "Starting SHP Portrait Tools"
    nohup "$HOME/.local/bin/shp-photocraft-tools" >/dev/null 2>&1 &
fi
echo "PhotoCraft: see the README's PhotoCraft connection section. GNOME needs the AppIndicator extension for the tray."
