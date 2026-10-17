#!/bin/zsh
# Build ClaimFlow's immutable macOS/Apple Silicon backend and its local OCR
# runtime. This script is for release builders, never for application users.
set -euo pipefail

PROJECT_ROOT="${0:A:h:h}"
BACKEND_ROOT="$PROJECT_ROOT/backend"
PYTHON="$BACKEND_ROOT/.venv/bin/python"
DIST_ROOT="$BACKEND_ROOT/dist"
OCR_ROOT="$BACKEND_ROOT/build/ocr"
OCR_BIN="$OCR_ROOT/bin"
OCR_LIB="$OCR_ROOT/lib"
OCR_DATA="$OCR_ROOT/tessdata"
# Avoid relying on a mutable user-global PyInstaller cache. This directory is
# generated under the ignored backend build tree for each release build.
export PYINSTALLER_CONFIG_DIR="$BACKEND_ROOT/build/pyinstaller-config"

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  print -u2 "This release script must run on an Apple Silicon Mac."
  exit 1
fi
if [[ ! -x "$PYTHON" ]]; then
  print -u2 "Missing backend virtual environment: $PYTHON"
  exit 1
fi
if ! command -v tesseract >/dev/null 2>&1; then
  print -u2 "Tesseract is required on the release-builder Mac to bundle local OCR."
  exit 1
fi

if ! "$PYTHON" -m PyInstaller --version >/dev/null 2>&1; then
  "$PYTHON" -m pip install --quiet "pyinstaller>=6.14,<7"
fi
"$PYTHON" -m PyInstaller --noconfirm --clean --onedir --name claimflow-backend \
  --distpath "$DIST_ROOT" --workpath "$BACKEND_ROOT/build/pyinstaller" \
  --specpath "$BACKEND_ROOT/build" --collect-all group_claims_backend \
  --collect-all fitz --collect-all PIL --collect-all openpyxl --collect-all xlrd \
  --collect-all pypdf --collect-all pytesseract --collect-all openai \
  --collect-all openai_codex --collect-all codex_cli_bin \
  "$BACKEND_ROOT/claimflow_backend_entry.py"

TESSERACT_BIN="$(command -v tesseract)"
TESSDATA_ROOT="$(tesseract --list-langs 2>&1 | sed -n 's|.* in "\(.*\)".*|\1|p' | head -n 1)"
if [[ -z "$TESSDATA_ROOT" || ! -f "$TESSDATA_ROOT/eng.traineddata" || ! -f "$TESSDATA_ROOT/deu.traineddata" ]]; then
  print -u2 "English and German Tesseract language data are required on the release-builder Mac."
  exit 1
fi

# This is a generated, ignored build directory with a fixed project-local path.
rm -rf "$OCR_ROOT"
mkdir -p "$OCR_BIN" "$OCR_LIB" "$OCR_DATA"
cp -p "$TESSERACT_BIN" "$OCR_BIN/tesseract"
cp -p "$TESSDATA_ROOT/eng.traineddata" "$OCR_DATA/eng.traineddata"
cp -p "$TESSDATA_ROOT/deu.traineddata" "$OCR_DATA/deu.traineddata"

typeset -a source_libraries
source_libraries=()

copy_library() {
  local source_library="$1"
  local target_library="$OCR_LIB/${source_library:t}"
  if [[ -e "$target_library" ]]; then
    return
  fi
  cp -p "$source_library" "$target_library"
  source_libraries+=("$source_library")
  local dependency dependency_path
  while IFS= read -r dependency; do
    dependency_path="$dependency"
    # Homebrew libraries can express a sibling dependency through @rpath.
    # Resolve that only relative to the copied library's trusted source
    # directory; other dynamic-loader forms are not accepted here.
    if [[ "$dependency" == @rpath/* || "$dependency" == @loader_path/* ]]; then
      dependency_path="${source_library:h}/${dependency:t}"
    fi
    if [[ "$dependency_path" == /opt/homebrew/* && -f "$dependency_path" ]]; then
      copy_library "$dependency_path"
    fi
  done < <(otool -L "$source_library" | awk 'NR > 1 { print $1 }')
}

local_dependency=""
while IFS= read -r local_dependency; do
  if [[ "$local_dependency" == /opt/homebrew/* && -f "$local_dependency" ]]; then
    copy_library "$local_dependency"
  fi
done < <(otool -L "$TESSERACT_BIN" | awk 'NR > 1 { print $1 }')

typeset -a bundle_binaries
bundle_binaries=("$OCR_BIN/tesseract" "$OCR_LIB"/*.dylib)
for binary in "${bundle_binaries[@]}"; do
  install_name_tool -add_rpath "@loader_path/../lib" "$binary" 2>/dev/null || true
  install_name_tool -add_rpath "@loader_path" "$binary" 2>/dev/null || true
done
for source_library in "${source_libraries[@]}"; do
  replacement="@rpath/${source_library:t}"
  for binary in "${bundle_binaries[@]}"; do
    install_name_tool -change "$source_library" "$replacement" "$binary" 2>/dev/null || true
  done
  install_name_tool -id "$replacement" "$OCR_LIB/${source_library:t}" 2>/dev/null || true
done

# Relinking Mach-O files invalidates their original Homebrew signatures. An
# ad-hoc signature makes the copied local OCR runtime executable inside this
# unsigned internal-testing build; a release identity/notarization is handled
# separately for a public distribution.
for binary in "${bundle_binaries[@]}"; do
  codesign --force --sign - --timestamp=none "$binary"
done

TESSDATA_PREFIX="$OCR_DATA" "$OCR_BIN/tesseract" --list-langs >/dev/null
print "Built standalone backend and bundled local OCR resources."
