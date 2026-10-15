#!/bin/zsh
# Compile the small native bridge that accesses the current user's macOS
# Keychain. Credentials are sent to it only over stdin, never command args.
set -euo pipefail

PROJECT_ROOT="${0:A:h:h}"
SOURCE="$PROJECT_ROOT/electron/credential-helper/main.swift"
OUTPUT_DIRECTORY="$PROJECT_ROOT/electron/credential-helper/build"
OUTPUT="$OUTPUT_DIRECTORY/claimflow-keychain-helper"
MODULE_CACHE="${TMPDIR:-/private/tmp}/claimflow-swift-module-cache"

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  print -u2 "This credential helper must be built on an Apple Silicon Mac."
  exit 1
fi

mkdir -p "$OUTPUT_DIRECTORY"
mkdir -p "$MODULE_CACHE"
export CLANG_MODULE_CACHE_PATH="$MODULE_CACHE"
xcrun swiftc -O -target arm64-apple-macosx11.0 -framework Security "$SOURCE" -o "$OUTPUT"
codesign --force --sign - --timestamp=none "$OUTPUT"
print "Built macOS Keychain credential helper."
