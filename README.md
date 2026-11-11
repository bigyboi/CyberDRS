# CyberDRS

A Decky Loader plugin for **Armada OS on the AYN Odin 3** that draws a small HUD
in the top-right corner of the screen while **Cyberpunk 2077** is running, showing
the **live base render resolution** — the size of the frame the game is actually
rendering before upscaling.

```
CYBERPUNK 2077
LIVE 1280 X 720
OUT 1920 X 1080  67%
```

## What it really measures (read this first)

Cyberpunk 2077's built-in **dynamic resolution scaling is a DLSS mode**. DLSS
needs an NVIDIA GPU, and the Odin 3 is Adreno, so on a stock install that mode is
not available and there is no DRS to report. This plugin does not pretend
otherwise.

Instead, the HUD reports the **actual Vulkan render-target size** the game is
using right now:

* With no upscaler, it shows the output resolution (nothing is being reduced).
* With an upscaler or a dynamic-resolution mod (FSR/FSR 3 mods, OptiScaler,
  resolution-scale tweaks), it shows the smaller base target the game renders
  into — i.e. exactly the number you want when tuning performance.
* When a mod changes the scale at runtime, the number follows within a second.

The measurement comes from a tiny optional **Vulkan layer** (the "probe") that
watches `vkCreateImage` calls for colour-attachment render targets. It is
installed as an *implicit* layer gated on `SteamGameId=1091500`, so it loads only
inside Cyberpunk and needs no launch options.

If the probe is not installed, the plugin falls back to reading a
resolution/render scale out of the game's `UserSettings.json` and labels the
result **ESTIMATE**. Most installs will not have such a value (again: DRS is a
DLSS mode), in which case the HUD says `NO DATA` and the QAM tells you to build
the probe.

## How the HUD gets on top of the game

The HUD is a plain Python + ctypes/Xlib process. It creates an override-redirect
X11 window, tags it with the `GAMESCOPE_EXTERNAL_OVERLAY` atom, and sets
`_NET_WM_WINDOW_OPACITY`. Gamescope composites external overlays above the
focused game and below the Steam overlay/QAM, so the HUD is visible in game and
the QAM still draws over it. The same mechanism is used by MangoHud and the
`OverLaid` Decky plugin.

No root, no compiled UI, no fonts or Cairo — the text uses an embedded 3x5
bitmap font.

## Install

1. Copy this folder to the Decky plugins directory on the device:

   ```sh
   ~/homebrew/plugins/CyberDRS/
   ```

   (or install the zip through Decky's developer tools). The folder must contain
   `plugin.json`, `main.py`, `dist/index.js`, `defaults/` and `native/`.

2. Restart Decky (or the device). **CyberDRS** appears in the Quick Access Menu.

3. Build the probe once, from the QAM:

   * Open the plugin.
   * Tap **Build & install probe**.

   If the build tools are missing, install them on the device first:

   ```sh
   sudo dnf install -y gcc vulkan-headers
   ```

   The button compiles `native/cyberdrs_layer.c` into
   `~/.local/lib/cyberdrs/libVkLayer_cyberdrs.so` and writes
   `~/.local/share/vulkan/implicit_layer.d/VK_LAYER_CYBERDRS_render_res.json`.
   You can also run `native/build.sh` by hand.

4. **Fully restart Cyberpunk 2077.** The layer is loaded at process start.

## Using it

* **Show overlay** turns the HUD on/off. The HUD also stops automatically when
  the game exits.
* **Text scale / Background opacity / Edge margin / Position** adjust the HUD.
  Changes apply within a second, no restart.
* **Render target** lists every full-frame target the probe currently sees, plus
  an `Auto` choice. Auto uses the largest target below output size when a
  near-output-size target also exists (the classic render → upscale pair);
  otherwise it uses the largest target. Pick a specific size if a game allocates
  same-aspect intermediates that confuse Auto.
* **Restart overlay** is there for debugging.

## Troubleshooting

**No HUD at all**

* Check **Diagnostics → Overlay log** in the QAM, and
  `~/.cache/cyberdrs/overlay.log`.
* `cannot open display` means the plugin could not connect to gamescope's X
  server. The backend tries the game process's own `DISPLAY` first, then `:0`.
  This plugin is for Steam Game Mode; Desktop Mode has no gamescope external
  overlay plane.
* The overlay only starts once `Cyberpunk2077.exe` is visible in `/proc`.

**HUD shows `NO DATA`**

* The probe is not installed or not loading. Build it, then fully restart the
  game.
* Verify the layer file exists and points at the `.so`:
  `cat ~/.local/share/vulkan/implicit_layer.d/VK_LAYER_CYBERDRS_render_res.json`
* To debug loading, launch the game from a terminal with
  `VK_LOADER_DEBUG=layer` and look for `VK_LAYER_CYBERDRS_render_res` in the
  output. `vulkaninfo --summary` in the same environment should list it when
  `SteamGameId=1091500` is set.
* The probe writes `~/.cache/cyberdrs/status.json` while the game is running;
  if that file is missing or old, the layer is not active.

**HUD shows the wrong size**

* Open **Render target** and choose the size you expect, or use Auto. Targets
  are filtered to the output aspect ratio, single-sample, colour attachments,
  at least 320x180, and no larger than the output.

**Build fails**

* `No C compiler found` / `Vulkan headers missing`: run
  `sudo dnf install -y gcc vulkan-headers`.
* Compiler errors are kept in `~/.cache/cyberdrs/build.log` and shown in the QAM.

**Probe disappeared after a system update**

* `~/.local` survives updates, but reinstall the probe from the QAM if the file
  was removed.

## Removing it

* QAM → **Remove probe** deletes the layer JSON and the `.so`.
* Delete `~/homebrew/plugins/CyberDRS` to remove the plugin.
* Optional leftovers: `~/.cache/cyberdrs/`.

## Files and paths

| Path | Purpose |
| --- | --- |
| `main.py` | Decky backend: process detection, HUD lifecycle, status merge, probe build |
| `src/index.tsx` | Quick Access Menu UI |
| `dist/index.js` | Built frontend bundle (needed at runtime) |
| `defaults/overlay/cyberdrs_overlay.py` | X11 HUD renderer (ctypes/Xlib) |
| `native/cyberdrs_layer.c` | Vulkan probe layer |
| `native/VK_LAYER_CYBERDRS_render_res.json` | Implicit-layer manifest template |
| `native/build.sh` | Manual probe build helper |
| `~/.cache/cyberdrs/status.json` | Probe output (LIVE) |
| `~/.cache/cyberdrs/display.json` | Backend → HUD payload |
| `~/.cache/cyberdrs/overlay.log` | HUD log |
| `~/.cache/cyberdrs/build.log` | Probe build log |

## Development

```sh
npm install
npm run build     # produces dist/index.js
npm run typecheck
```

Python 3 is used as shipped with the OS; the HUD needs only `libX11` (and
optionally `libXext` for click-through). The probe needs a C compiler and the
Vulkan headers at build time.

## Limitations

* Steam Game Mode / gamescope only. The HUD will not appear in Desktop Mode.
* The probe reads render targets, not the game's UI settings. If a frame
  allocates several same-aspect targets, Auto can guess wrong — use the manual
  override.
* Cyberpunk's native DRS is DLSS-only and therefore unavailable on Adreno. The
  plugin reports real render-target sizes, which is what matters when using
  FSR or dynamic-resolution mods.
