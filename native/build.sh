#!/usr/bin/env bash
# Manual build helper. The plugin's "Build & install probe" button does the
# same thing from Python, so this is only here for people who prefer a shell.
#
# Requires: gcc (or clang) and vulkan-headers
#   sudo dnf install -y gcc vulkan-headers
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out="${1:-$HOME/.local/lib/cyberdrs/libVkLayer_cyberdrs.so}"

mkdir -p "$(dirname "$out")"
cc -shared -fPIC -O2 -Wall -Wextra -o "$out" "$here/cyberdrs_layer.c" -lpthread -lm

mkdir -p "$HOME/.local/share/vulkan/implicit_layer.d"
sed "s|@LIBRARY_PATH@|$out|g" \
    "$here/VK_LAYER_CYBERDRS_render_res.json" \
    > "$HOME/.local/share/vulkan/implicit_layer.d/VK_LAYER_CYBERDRS_render_res.json"

echo "CyberDRS probe installed:"
echo "  $out"
echo "  $HOME/.local/share/vulkan/implicit_layer.d/VK_LAYER_CYBERDRS_render_res.json"
echo
echo "Fully restart Cyberpunk 2077. Remove with:"
echo "  rm -f \"$out\" \"$HOME/.local/share/vulkan/implicit_layer.d/VK_LAYER_CYBERDRS_render_res.json\""
