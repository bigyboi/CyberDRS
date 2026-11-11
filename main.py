"""CyberDRS backend.

Watches for Cyberpunk 2077, keeps an X11 HUD process alive on top of the game
(gamescope external overlay), and merges two resolution sources:

  LIVE  - a small optional Vulkan layer (native/cyberdrs_layer.c) that tracks
          colour-attachment render targets created by the game.
  EST   - Cyberpunk's UserSettings.json (only useful when a render/resolution
          scale is actually stored there).

Nothing here needs root. The overlay is plain Python + ctypes/Xlib.
"""

import asyncio
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import zipfile

import decky

APP_ID = "1091500"
GAME_EXE = "Cyberpunk2077.exe"
LAYER_JSON = "VK_LAYER_CYBERDRS_render_res.json"

DEFAULT_SETTINGS = {
    "enabled": True,
    "scale": 4,
    "opacity": 88,
    "margin": 18,
    "position": "top-right",
    "show_output": True,
    "candidate": "auto",
    "display": "",
}

COLORS = {
    "fg": "#00E5FF",
    "accent": "#FFD400",
    "dim": "#9AA0B4",
    "warn": "#FF6B6B",
    "bg": "#08080C",
}


def _safe_read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _atomic_write_json(path, obj):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, separators=(",", ":"))
        os.replace(tmp, path)
        return True
    except Exception as exc:
        decky.logger.warning("CyberDRS: could not write %s: %s", path, exc)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def _tail(path, limit=40):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
        return lines[-limit:]
    except Exception:
        return []


class Plugin:
    # ------------------------------------------------------------------ setup

    async def _main(self):
        self._settings_path = os.path.join(
            getattr(decky, "DECKY_PLUGIN_SETTINGS_DIR", ""), "settings.json"
        )
        home = getattr(decky, "DECKY_USER_HOME", None) or os.path.expanduser("~")
        self._home = home
        self._cache = os.path.join(home, ".cache", "cyberdrs")
        os.makedirs(self._cache, exist_ok=True)
        os.makedirs(os.path.dirname(self._settings_path) or self._cache, exist_ok=True)

        self._settings = dict(DEFAULT_SETTINGS)
        loaded = _safe_read_json(self._settings_path)
        if isinstance(loaded, dict):
            self._settings.update(self._sanitize(loaded))

        self._proc = None
        self._game_pid = None
        self._overlay_pid = None
        self._display = {"mode": "OFF", "ts": 0}
        self._output_cache = (0.0, None)
        self._settings_cache = (0.0, None)
        self._last_spawn = 0.0
        self._overlay_since = 0.0
        self._spawn_failures = 0
        self._python_bin = None
        self._python_env = {}
        self._lock = asyncio.Lock()

        self._display_path = os.path.join(self._cache, "display.json")
        self._probe_path = os.path.join(self._cache, "status.json")
        self._fallback_path = os.path.join(self._cache, "fallback.json")
        self._output_path = os.path.join(self._cache, "output.json")
        self._overlay_log = os.path.join(self._cache, "overlay.log")
        self._build_log = os.path.join(self._cache, "build.log")
        self._overlay_script = os.path.join(
            decky.DECKY_PLUGIN_DIR, "defaults", "overlay", "cyberdrs_overlay.py"
        )

        self._kill_stale_overlay()
        self._watch_task = asyncio.create_task(self._watch())
        decky.logger.info("CyberDRS started (cache: %s)", self._cache)

    async def _unload(self):
        task = getattr(self, "_watch_task", None)
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._kill_overlay()
        decky.logger.info("CyberDRS stopped")

    # --------------------------------------------------------------- helpers

    def _sanitize(self, patch):
        out = {}
        if "enabled" in patch:
            out["enabled"] = bool(patch["enabled"])
        if "scale" in patch:
            try:
                out["scale"] = max(2, min(8, int(patch["scale"])))
            except (TypeError, ValueError):
                pass
        if "opacity" in patch:
            try:
                out["opacity"] = max(20, min(100, int(patch["opacity"])))
            except (TypeError, ValueError):
                pass
        if "margin" in patch:
            try:
                out["margin"] = max(0, min(96, int(patch["margin"])))
            except (TypeError, ValueError):
                pass
        if "position" in patch and patch["position"] in (
            "top-right",
            "top-left",
            "bottom-right",
            "bottom-left",
        ):
            out["position"] = patch["position"]
        if "show_output" in patch:
            out["show_output"] = bool(patch["show_output"])
        if "display" in patch and isinstance(patch["display"], str):
            out["display"] = patch["display"].strip()[:64]
        if "candidate" in patch:
            value = str(patch["candidate"])
            if value == "auto" or re.fullmatch(r"\d{3,5}x\d{3,5}", value):
                out["candidate"] = value
        return out

    def _save_settings(self):
        _atomic_write_json(self._settings_path, self._settings)

    def _find_game_pid(self):
        try:
            entries = os.listdir("/proc")
        except OSError:
            return None
        needle = GAME_EXE.encode()
        for entry in entries:
            if not entry.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % entry, "rb") as fh:
                    cmd = fh.read()
            except OSError:
                continue
            if needle in cmd:
                return int(entry)
        return None

    def _game_env(self, pid):
        env = dict(os.environ)
        wanted = {b"DISPLAY", b"XAUTHORITY", b"XDG_RUNTIME_DIR"}
        try:
            with open("/proc/%d/environ" % pid, "rb") as fh:
                raw = fh.read().split(b"\0")
            for item in raw:
                if b"=" not in item:
                    continue
                key, value = item.split(b"=", 1)
                if key in wanted:
                    env[key.decode()] = value.decode("utf-8", "replace")
        except OSError:
            pass
        if not env.get("DISPLAY"):
            env["DISPLAY"] = self._settings.get("display") or ":0"
        env.pop("WAYLAND_DISPLAY", None)
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def _kill_overlay(self):
        proc = getattr(self, "_proc", None)
        if proc is not None:
            try:
                if proc.poll() is None:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    except Exception:
                        proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except Exception:
                        try:
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                        except Exception:
                            pass
            except Exception as exc:
                decky.logger.warning("CyberDRS: overlay kill failed: %s", exc)
        self._proc = None
        self._overlay_pid = None

    def _kill_stale_overlay(self):
        """Kill an overlay left behind by a previous backend instance."""
        pid_path = os.path.join(self._cache, "overlay.pid")
        try:
            with open(pid_path, "r", encoding="utf-8") as fh:
                pid = int(fh.read().strip())
        except Exception:
            return
        if pid <= 0 or pid == os.getpid():
            return
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as fh:
                cmd = fh.read()
            if b"cyberdrs_overlay.py" in cmd:
                os.kill(pid, signal.SIGTERM)
                decky.logger.info("CyberDRS: killed stale overlay %d", pid)
        except OSError:
            pass

    _PY_PROBE = "import ctypes, json, struct, threading; print('CYBERDRS_OK')"

    def _python_candidates(self):
        """Interpreter candidates, best guess first."""
        cands = []
        exe = sys.executable
        if exe and os.path.isfile(exe):
            # The interpreter Decky itself runs on is proven to work.
            cands.append(exe)
        for name in ("python3", "python"):
            found = shutil.which(name)
            if found:
                cands.append(found)
        for ver in ("3.14", "3.13", "3.12", "3.11", "3.10", "3.9"):
            cands.append("/usr/bin/python3." + ver)
        cands.append("/usr/local/bin/python3")
        out = []
        for path in cands:
            if path not in out:
                out.append(path)
        return out

    def _pick_python(self):
        """Find a Python that can actually import its stdlib.

        Some immutable distros ship a /usr/bin/python3 whose build prefix is
        a compile-time path that does not exist on the target (the overlay
        then dies with "Failed to import encodings" before user code runs).
        Probe candidates — and, as a fallback, the same binary with
        PYTHONHOME=/usr, which repairs a mispackaged but intact stdlib.
        """
        for path in self._python_candidates():
            for pyhome in (None, "/usr"):
                env = dict(os.environ)
                if pyhome:
                    env["PYTHONHOME"] = pyhome
                try:
                    proc = subprocess.run(
                        [path, "-u", "-c", self._PY_PROBE],
                        capture_output=True,
                        timeout=10,
                        env=env,
                    )
                except Exception as exc:
                    decky.logger.info("CyberDRS: interpreter probe %s failed: %s", path, exc)
                    continue
                if proc.returncode == 0 and b"CYBERDRS_OK" in proc.stdout:
                    self._python_bin = path
                    self._python_env = {"PYTHONHOME": pyhome} if pyhome else {}
                    decky.logger.info(
                        "CyberDRS: overlay interpreter: %s%s",
                        path,
                        (" (PYTHONHOME=%s)" % pyhome) if pyhome else "",
                    )
                    return path
        decky.logger.error(
            "CyberDRS: no working Python interpreter found; tried: %s",
            ", ".join(self._python_candidates()),
        )
        return None

    def _spawn_overlay(self, pid):
        if not os.path.isfile(self._overlay_script):
            decky.logger.error("CyberDRS: overlay script missing: %s", self._overlay_script)
            return False
        python = getattr(self, "_python_bin", None) or self._pick_python()
        if not python:
            return False
        env = self._game_env(pid)
        env.update(dict(self._python_env))
        try:
            log = open(self._overlay_log, "ab", buffering=0)
            log.write(
                ("\n--- %s starting overlay (game pid %d) ---\n" % (time.strftime("%F %T"), pid)).encode()
            )
            self._proc = subprocess.Popen(
                [python, "-u", self._overlay_script],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                cwd=self._cache,
            )
            log.close()
            self._overlay_pid = self._proc.pid
            self._last_spawn = time.time()
            self._overlay_since = self._last_spawn
            decky.logger.info("CyberDRS: overlay started (pid %d)", self._proc.pid)
            return True
        except Exception as exc:
            decky.logger.error("CyberDRS: overlay spawn failed: %s", exc)
            self._proc = None
            self._overlay_pid = None
            return False

    def _overlay_alive(self):
        proc = getattr(self, "_proc", None)
        return proc is not None and proc.poll() is None

    # ------------------------------------------------------------- data read

    def _probe_status(self):
        data = _safe_read_json(self._probe_path)
        if not isinstance(data, dict):
            return None
        try:
            ts = float(data.get("ts") or 0)
        except (TypeError, ValueError):
            return None
        if time.time() - ts > 6.0:
            return None
        return data

    def _output_size(self, probe):
        now = time.time()
        stamp, cached = self._output_cache
        if cached and now - stamp < 8.0:
            return cached
        size = None
        out = _safe_read_json(self._output_path)
        if isinstance(out, dict) and out.get("w") and out.get("h"):
            size = (int(out["w"]), int(out["h"]))
        if not size and probe:
            po = probe.get("output")
            if isinstance(po, list) and len(po) == 2 and po[0] and po[1]:
                size = (int(po[0]), int(po[1]))
        if not size:
            size = self._xrandr_size()
        # Cache a real result for a while, but retry misses quickly so the
        # overlay's output.json is picked up as soon as it appears.
        self._output_cache = (now, size) if size else (now - 6.5, None)
        return size

    def _xrandr_size(self):
        display = self._settings.get("display") or ":0"
        env = dict(os.environ)
        env["DISPLAY"] = display
        try:
            out = subprocess.run(
                ["xrandr", "--current"],
                capture_output=True,
                text=True,
                timeout=3,
                env=env,
            ).stdout
            match = re.search(r"current\s+(\d+)\s*x\s*(\d+)", out)
            if match:
                return (int(match.group(1)), int(match.group(2)))
        except Exception:
            pass
        return None

    def _user_settings(self):
        now = time.time()
        stamp, cached = self._settings_cache
        if now - stamp < 10.0:
            return cached
        patterns = [
            os.path.join(
                self._home,
                ".steam/steam/steamapps/compatdata",
                APP_ID,
                "pfx/drive_c/users/*/AppData/Local/CD Projekt Red/Cyberpunk 2077/UserSettings.json",
            ),
            os.path.join(
                self._home,
                ".local/share/Steam/steamapps/compatdata",
                APP_ID,
                "pfx/drive_c/users/*/AppData/Local/CD Projekt Red/Cyberpunk 2077/UserSettings.json",
            ),
            os.path.join(
                self._home,
                ".steam/root/steamapps/compatdata",
                APP_ID,
                "pfx/drive_c/users/*/AppData/Local/CD Projekt Red/Cyberpunk 2077/UserSettings.json",
            ),
        ]
        data = None
        for pattern in patterns:
            for path in glob.glob(pattern):
                raw = _safe_read_json(path)
                if isinstance(raw, dict):
                    data = self._scan_user_settings(raw)
                    if data:
                        break
            if data:
                break
        self._settings_cache = (now, data)
        return data

    def _scan_user_settings(self, root):
        found = {}

        def walk(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    low = str(key).lower()
                    if isinstance(value, (dict, list)):
                        walk(value)
                        continue
                    if isinstance(value, str):
                        match = re.fullmatch(r"\s*(\d{3,5})\s*[xX]\s*(\d{3,5})\s*", value)
                        if match and ("resolution" in low or "render" in low):
                            found.setdefault(
                                "resolution", (int(match.group(1)), int(match.group(2)))
                            )
                    elif isinstance(value, (int, float)) and not isinstance(value, bool):
                        scale = float(value)
                        if 0.15 <= scale <= 1.0 and "scale" in low and (
                            "res" in low or "render" in low or "dynamic" in low or "drs" in low
                        ):
                            found.setdefault("scale", scale)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(root)
        return found or None

    def _estimate(self, out_w, out_h):
        data = self._user_settings()
        if not data:
            return None
        if data.get("scale") and out_w and out_h:
            scale = data["scale"]
            return (max(2, round(out_w * scale)), max(2, round(out_h * scale)))
        res = data.get("resolution")
        if res:
            if out_w and out_h and (res[0] * res[1]) >= 0.95 * out_w * out_h:
                return None
            return res
        return None

    def _pick_candidate(self, probe):
        candidates = probe.get("candidates") or []
        candidates = [
            c
            for c in candidates
            if isinstance(c, dict) and c.get("w") and c.get("h")
        ]
        selected = self._settings.get("candidate", "auto")
        if selected != "auto":
            match = re.fullmatch(r"(\d+)x(\d+)", selected)
            if match:
                want = (int(match.group(1)), int(match.group(2)))
                for cand in candidates:
                    if (int(cand["w"]), int(cand["h"])) == want:
                        return cand, candidates
        auto = probe.get("auto")
        if isinstance(auto, dict) and auto.get("w") and auto.get("h"):
            return auto, candidates
        if candidates:
            return candidates[0], candidates
        return None, candidates

    def _build_display(self, probe):
        out_w = out_h = None
        size = self._output_size(probe)
        if size:
            out_w, out_h = size

        mode = "WAIT"
        width = height = None
        age = None
        note = ""

        if probe:
            cand, _ = self._pick_candidate(probe)
            if cand:
                width, height = int(cand["w"]), int(cand["h"])
                mode = "LIVE"
                note = "VULKAN PROBE"
                try:
                    age = max(0.0, time.time() - float(probe.get("ts") or 0))
                except (TypeError, ValueError):
                    age = None

        if mode != "LIVE":
            est = self._estimate(out_w, out_h)
            if est:
                width, height = est
                mode = "EST"
                note = "USER SETTINGS ESTIMATE"

        lines = []
        if width and height:
            lines.append({"t": "%s %d X %d" % (mode, width, height), "c": "accent"})
            if self._settings.get("show_output", True) and out_w and out_h:
                pct = int(round(100.0 * width / float(out_w)))
                lines.append(
                    {"t": "OUT %d X %d  %d%%" % (out_w, out_h, pct), "c": "dim"}
                )
            if mode == "EST":
                lines.append({"t": "ESTIMATE - NOT LIVE", "c": "warn"})
        else:
            lines.append({"t": "NO DATA", "c": "warn"})
            lines.append({"t": "BUILD PROBE IN QAM", "c": "dim"})

        display = {
            "ts": time.time(),
            "mode": mode,
            "note": note,
            "w": width,
            "h": height,
            "out_w": out_w,
            "out_h": out_h,
            "age": age,
            "title": "CYBERPUNK 2077",
            "lines": lines,
            "style": {
                "scale": int(self._settings.get("scale", 4)),
                "opacity": int(self._settings.get("opacity", 88)),
                "margin": int(self._settings.get("margin", 18)),
                "position": self._settings.get("position", "top-right"),
                "fg": COLORS["fg"],
                "accent": COLORS["accent"],
                "dim": COLORS["dim"],
                "warn": COLORS["warn"],
                "bg": COLORS["bg"],
                "border": True,
            },
        }
        return display

    # ---------------------------------------------------------------- watcher

    async def _watch(self):
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                decky.logger.exception("CyberDRS watcher error: %s", exc)
            await asyncio.sleep(1.0)

    async def _tick(self):
        pid = self._find_game_pid()
        self._game_pid = pid
        enabled = bool(self._settings.get("enabled", True))

        if pid and enabled:
            if self._overlay_alive():
                if self._spawn_failures and time.time() - self._overlay_since > 10.0:
                    self._spawn_failures = 0
            else:
                proc = getattr(self, "_proc", None)
                if (
                    proc is not None
                    and time.time() - self._last_spawn < 4.0
                    and proc.poll() is not None
                ):
                    # The overlay died right after spawning (bad display,
                    # missing libs). It will be retried with increasing delay;
                    # overlay.log has the reason.
                    self._spawn_failures = min(self._spawn_failures + 1, 10)
                    decky.logger.warning(
                        "CyberDRS: overlay exited immediately (exit %s, attempt %d); see %s",
                        proc.returncode,
                        self._spawn_failures,
                        self._overlay_log,
                    )
                # Back off if the overlay keeps dying (e.g. no X display yet).
                delay = min(30.0, 3.0 * (2 ** min(self._spawn_failures, 3)))
                if time.time() - self._last_spawn > delay:
                    if not self._spawn_overlay(pid):
                        self._spawn_failures += 1
                        self._last_spawn = time.time()
            probe = self._probe_status()
            display = self._build_display(probe)
            self._display = display
            _atomic_write_json(self._display_path, display)

            fallback = {
                "ts": time.time(),
                "pid": pid,
                "mode": display["mode"],
                "w": display["w"],
                "h": display["h"],
                "out_w": display["out_w"],
                "out_h": display["out_h"],
            }
            _atomic_write_json(self._fallback_path, fallback)
        else:
            if self._overlay_alive():
                self._kill_overlay()
            self._spawn_failures = 0
            self._display = {
                "mode": "OFF",
                "w": None,
                "h": None,
                "out_w": None,
                "out_h": None,
                "age": None,
                "note": "GAME NOT RUNNING" if not pid else "DISABLED",
            }

    # -------------------------------------------------------------------- API

    async def get_status(self):
        probe = self._probe_status()
        candidates = []
        if probe:
            for cand in probe.get("candidates") or []:
                if isinstance(cand, dict) and cand.get("w") and cand.get("h"):
                    candidates.append(
                        {
                            "w": int(cand["w"]),
                            "h": int(cand["h"]),
                            "n": int(cand.get("n") or 0),
                        }
                    )
        display = dict(self._display)
        auto = None
        if probe:
            raw_auto = probe.get("auto")
            if isinstance(raw_auto, dict) and raw_auto.get("w") and raw_auto.get("h"):
                auto = {"w": int(raw_auto["w"]), "h": int(raw_auto["h"])}
        probe_info = self._probe_info()
        return {
            "settings": dict(self._settings),
            "running": self._game_pid is not None,
            "game_pid": self._game_pid,
            "overlay": self._overlay_alive(),
            "overlay_pid": self._overlay_pid,
            "overlay_failures": getattr(self, "_spawn_failures", 0),
            "python": getattr(self, "_python_bin", None),
            "mode": display.get("mode", "OFF"),
            "width": display.get("w"),
            "height": display.get("h"),
            "out_width": display.get("out_w"),
            "out_height": display.get("out_h"),
            "age": display.get("age"),
            "note": display.get("note", ""),
            "candidates": candidates,
            "auto": auto,
            "selected": self._settings.get("candidate", "auto"),
            "probe": probe_info,
            "overlay_log": _tail(self._overlay_log, 30),
            "build_log": _tail(self._build_log, 30),
            "cache_dir": self._cache,
        }

    async def set_settings(self, patch):
        if not isinstance(patch, dict):
            return dict(self._settings)
        self._settings.update(self._sanitize(patch))
        self._save_settings()
        if not self._settings.get("enabled", True):
            self._kill_overlay()
        elif self._game_pid:
            self._last_spawn = 0.0
            self._spawn_failures = 0
        return dict(self._settings)

    async def set_enabled(self, enabled):
        return await self.set_settings({"enabled": bool(enabled)})

    async def restart_overlay(self):
        self._kill_overlay()
        pid = self._find_game_pid()
        if pid and self._settings.get("enabled", True):
            self._last_spawn = 0.0
            self._spawn_failures = 0
            return self._spawn_overlay(pid)
        return False

    def _layer_dirs(self):
        dirs = []
        xdg = os.environ.get("XDG_DATA_HOME")
        if xdg:
            dirs.append(os.path.join(xdg, "vulkan", "implicit_layer.d"))
        dirs.append(os.path.join(self._home, ".local", "share", "vulkan", "implicit_layer.d"))
        unique = []
        for path in dirs:
            if path not in unique:
                unique.append(path)
        return unique

    async def build_probe(self):
        async with self._lock:
            return await self._build_probe()

    async def _build_probe(self):
        native = os.path.join(decky.DECKY_PLUGIN_DIR, "native")
        source = os.path.join(native, "cyberdrs_layer.c")
        template = os.path.join(native, LAYER_JSON)
        if not os.path.isfile(source) or not os.path.isfile(template):
            return {"ok": False, "message": "Layer sources are missing from the plugin."}

        compiler = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
        if not compiler:
            return {
                "ok": False,
                "message": "No C compiler found. Run: sudo dnf install -y gcc vulkan-headers",
            }
        if not os.path.isfile("/usr/include/vulkan/vk_layer.h"):
            return {
                "ok": False,
                "message": "Vulkan headers missing. Run: sudo dnf install -y vulkan-headers",
            }

        so_dir = os.path.join(self._home, ".local", "lib", "cyberdrs")
        os.makedirs(so_dir, exist_ok=True)
        so_path = os.path.join(so_dir, "libVkLayer_cyberdrs.so")
        cmd = [
            compiler,
            "-shared",
            "-fPIC",
            "-O2",
            "-Wall",
            "-Wextra",
            "-o",
            so_path,
            source,
            "-lpthread",
            "-lm",
        ]
        decky.logger.info("CyberDRS: building probe: %s", " ".join(cmd))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=240)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return {"ok": False, "message": "Build timed out after 240s."}
            text = (out or b"").decode("utf-8", "replace")
        except Exception as exc:
            return {"ok": False, "message": "Build failed to start: %s" % exc}

        try:
            with open(self._build_log, "w", encoding="utf-8") as fh:
                fh.write("$ %s\n\n%s" % (" ".join(cmd), text))
        except OSError:
            pass

        if proc.returncode != 0:
            return {
                "ok": False,
                "message": "Compiler failed (exit %s). See build log." % proc.returncode,
                "log": text[-4000:],
            }
        if not os.path.isfile(so_path):
            return {"ok": False, "message": "Compiler produced no .so", "log": text[-4000:]}

        try:
            with open(template, "r", encoding="utf-8") as fh:
                layer_json = fh.read().replace("@LIBRARY_PATH@", so_path)
            targets = []
            for layer_dir in self._layer_dirs():
                os.makedirs(layer_dir, exist_ok=True)
                target = os.path.join(layer_dir, LAYER_JSON)
                with open(target, "w", encoding="utf-8") as fh:
                    fh.write(layer_json)
                targets.append(target)
        except Exception as exc:
            return {"ok": False, "message": "Built, but install failed: %s" % exc}

        return {
            "ok": True,
            "message": "Probe installed. Fully restart Cyberpunk 2077.",
            "path": so_path,
            "log": text[-2000:],
        }

    async def remove_probe(self):
        removed = []
        targets = [
            os.path.join(self._home, ".local", "lib", "cyberdrs", "libVkLayer_cyberdrs.so"),
        ]
        for layer_dir in self._layer_dirs():
            targets.append(os.path.join(layer_dir, LAYER_JSON))
        for path in targets:
            try:
                os.unlink(path)
                removed.append(path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                return {"ok": False, "message": "Could not remove %s: %s" % (path, exc)}
        try:
            os.unlink(self._probe_path)
        except OSError:
            pass
        return {"ok": True, "message": "Probe removed.", "removed": removed}

    def _game_env_subset(self):
        """The few environment bits that are useful and not private."""
        pid = getattr(self, "_game_pid", None)
        lines = []
        if pid:
            wanted = {
                "DISPLAY",
                "HOME",
                "XDG_DATA_HOME",
                "XDG_RUNTIME_DIR",
                "SteamAppId",
                "SteamGameId",
            }
            try:
                with open("/proc/%d/environ" % pid, "rb") as fh:
                    for item in fh.read().split(b"\0"):
                        if b"=" not in item:
                            continue
                        key, _, value = item.partition(b"=")
                        key = key.decode("utf-8", "replace")
                        if key == "XAUTHORITY":
                            lines.append("XAUTHORITY=<set>")
                        elif key in wanted:
                            lines.append("%s=%s" % (key, value.decode("utf-8", "replace")))
            except OSError:
                lines.append("(could not read game environ)")
        return lines

    async def export_logs(self):
        """Bundle every log/state file into a zip in ~/Downloads (or ~)."""
        dest_dir = os.path.join(self._home, "Downloads")
        if not os.path.isdir(dest_dir):
            dest_dir = self._home
        dest = os.path.join(dest_dir, "cyberdrs-diagnostics-%s.zip" % time.strftime("%Y%m%d-%H%M%S"))

        files = [
            ("overlay.log", self._overlay_log),
            ("build.log", self._build_log),
            ("display.json", self._display_path),
            ("fallback.json", self._fallback_path),
            ("status.json", self._probe_path),
            ("output.json", self._output_path),
        ]
        loader_log = os.path.join(self._cache, "loader.log")
        if os.path.isfile(loader_log):
            files.append(("loader.log", loader_log))
        for idx, layer_dir in enumerate(self._layer_dirs()):
            manifest = os.path.join(layer_dir, LAYER_JSON)
            if os.path.isfile(manifest):
                files.append(("layer-manifest-%d.json" % idx, manifest))

        diag = [
            "CyberDRS diagnostics %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
            "python overlay interpreter: %s" % getattr(self, "_python_bin", None),
            "python overlay PYTHONHOME patch: %s" % (getattr(self, "_python_env", {}) or "(none)"),
            "backend sys.executable: %s" % sys.executable,
            "backend sys.version: %s" % sys.version.split()[0],
            "game pid: %s" % getattr(self, "_game_pid", None),
            "overlay pid: %s" % getattr(self, "_overlay_pid", None),
            "spawn failures: %s" % getattr(self, "_spawn_failures", 0),
            "settings: %s" % json.dumps(self._settings, sort_keys=True),
            "cache dir: %s" % self._cache,
            "layer dirs: %s" % ", ".join(self._layer_dirs()),
            "",
            "-- interpreter candidates --",
        ]
        for cand in self._python_candidates():
            diag.append("  %s exists=%s" % (cand, os.path.exists(cand)))
        diag += ["", "-- game environment (subset) --"] + self._game_env_subset()
        diag += ["", "-- cache directory --"]
        try:
            for name in sorted(os.listdir(self._cache)):
                try:
                    info = os.stat(os.path.join(self._cache, name))
                    diag.append(
                        "  %-20s %8d bytes  mtime %s"
                        % (name, info.st_size, time.strftime("%F %T", time.localtime(info.st_mtime)))
                    )
                except OSError:
                    pass
        except OSError:
            pass

        try:
            with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("diagnostics.txt", "\n".join(diag) + "\n")
                for arcname, path in files:
                    try:
                        zf.write(path, arcname)
                    except OSError:
                        zf.writestr(arcname, "(unreadable)")
            return {"ok": True, "message": "Saved %s" % dest}
        except Exception as exc:
            decky.logger.exception("CyberDRS: log export failed: %s", exc)
            return {"ok": False, "message": "Export failed: %s" % exc}

    def _probe_info(self):
        so_path = os.path.join(self._home, ".local", "lib", "cyberdrs", "libVkLayer_cyberdrs.so")
        json_path = None
        for layer_dir in self._layer_dirs():
            candidate = os.path.join(layer_dir, LAYER_JSON)
            if os.path.isfile(candidate):
                json_path = candidate
                break
        if json_path is None:
            json_path = os.path.join(self._layer_dirs()[0], LAYER_JSON)
        compiler = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
        return {
            "compiler": compiler,
            "headers": os.path.isfile("/usr/include/vulkan/vk_layer.h"),
            "built": os.path.isfile(so_path),
            "installed": os.path.isfile(json_path),
            "so_path": so_path,
            "json_path": json_path,
            "live": self._probe_status() is not None,
        }
