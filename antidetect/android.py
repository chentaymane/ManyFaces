"""Real Android engine — boots the official Android Emulator (AVD) per profile.

Unlike the camoufox/chromium engines (desktop browsers wearing a mobile fingerprint),
this runs a genuine Android OS in the official emulator, so the browser inside is a
*real* Chrome-for-Android / Android WebView (real ARM-ish Blink, real touch, real GPU).
Nothing is spoofed — the device identity is the AVD's real identity.

Everything is free and self-contained under the app data dir:
    <DATA_DIR>/android/
        jre/          portable Temurin JRE (only if no system Java is found)
        sdk/          Android SDK (cmdline-tools, platform-tools, emulator, image)
        avd/          one AVD per profile (ANDROID_AVD_HOME)

The heavy parts (a few GB of SDK + system image, and a 20–60s first boot) are the
real cost of a real device; `install()` streams progress so the UI can show it.

NOTE: `install()` and the emulator require hardware virtualization (WHPX on Windows,
KVM on Linux, Hypervisor.framework on macOS). Detection/errors are surfaced clearly.
"""
from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

from . import config

# --- Downloadable pieces (override via env for pinning/mirrors) ----------------
# cmdline-tools is the bootstrap that gives us sdkmanager/avdmanager. Version numbers
# here are the "latest" build ids at time of writing; bump if Google rotates them.
_CT = {
    "Windows": os.environ.get("ANTIDETECT_CMDLINE_URL",
        "https://dl.google.com/android/repository/commandlinetools-win-11076708_latest.zip"),
    "Linux": os.environ.get("ANTIDETECT_CMDLINE_URL",
        "https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip"),
    "Darwin": os.environ.get("ANTIDETECT_CMDLINE_URL",
        "https://dl.google.com/android/repository/commandlinetools-mac-11076708_latest.zip"),
}
# scrcpy — mirrors the (headless) emulator in a reliable window with real touch input.
# The Android emulator's own Qt window is black on many Windows machines (missing
# opengl32sw / layered-window failures); scrcpy renders independently of it. Windows
# build is a self-contained zip (bundles its own adb + dlls).
_SCRCPY = {
    "Windows": os.environ.get("ANTIDETECT_SCRCPY_URL",
        "https://github.com/Genymobile/scrcpy/releases/download/v2.7/scrcpy-win64-v2.7.zip"),
}
# Portable JRE (Temurin 17) — fetched only when no system Java 17+ is present.
_JRE = {
    "Windows": "https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.12%2B7/OpenJDK17U-jre_x64_windows_hotspot_17.0.12_7.zip",
    "Linux": "https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.12%2B7/OpenJDK17U-jre_x64_linux_hotspot_17.0.12_7.tar.gz",
    "Darwin": "https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.12%2B7/OpenJDK17U-jre_x64_mac_hotspot_17.0.12_7.tar.gz",
}
# Which Android to run. A Play-enabled image ships Chrome as a system app; google_apis
# is smaller. Overridable so users can pick an API level / variant.
SYSTEM_IMAGE = os.environ.get("ANTIDETECT_ANDROID_IMAGE", "system-images;android-34;google_apis_playstore;x86_64")
_ANDROID_API_PKGS = ["platform-tools", "emulator", SYSTEM_IMAGE]
# GPU rendering mode. We run headless + mirror with scrcpy, so the emulator's own
# (black-screening) window is never shown — which means we can safely use HARDWARE
# rendering (`host`) for a huge speed-up over software (`swiftshader_indirect`). The
# old black screen was that hidden window, not the guest GPU. Fall back to software
# only if host rendering misbehaves: ANTIDETECT_ANDROID_GPU=swiftshader_indirect.
ANDROID_GPU = os.environ.get("ANTIDETECT_ANDROID_GPU", "host")
# Guest RAM (MB) and CPU cores. Kept modest so an 8 GB host doesn't swap (swapping is
# a massive lag source). Override for beefier machines: ANTIDETECT_ANDROID_MEMORY.
ANDROID_MEMORY = os.environ.get("ANTIDETECT_ANDROID_MEMORY", "2048")
ANDROID_CORES = os.environ.get("ANTIDETECT_ANDROID_CORES", "4")
# Quickboot: save a snapshot on exit and restore it next launch, so only the FIRST
# boot pays the full cold-boot cost — later launches come up in seconds. Set
# ANTIDETECT_ANDROID_COLDBOOT=1 to always cold-boot instead.
ANDROID_COLDBOOT = os.environ.get("ANTIDETECT_ANDROID_COLDBOOT", "") == "1"

_IS_WIN = platform.system() == "Windows"


# --- Paths --------------------------------------------------------------------
def _root() -> Path:
    return config.DATA_DIR / "android"


def sdk_root() -> Path:
    return _root() / "sdk"


def avd_home() -> Path:
    return _root() / "avd"


def jre_dir() -> Path:
    return _root() / "jre"


def scrcpy_dir() -> Path:
    return _root() / "scrcpy"


def scrcpy_exe() -> Optional[Path]:
    """Locate scrcpy.exe under the scrcpy dir (the zip has a versioned subfolder)."""
    d = scrcpy_dir()
    if not d.exists():
        return None
    direct = _bin(d, "scrcpy")
    if direct.exists():
        return direct
    for p in d.rglob("scrcpy.exe" if _IS_WIN else "scrcpy"):
        return p
    return None


def _bin(p: Path, name: str) -> Path:
    """Resolve an executable in dir `p`, trying platform suffixes (.exe/.bat)."""
    for suffix in ((".exe", ".bat", "") if _IS_WIN else ("",)):
        cand = p / (name + suffix)
        if cand.exists():
            return cand
    return p / name  # non-existent; caller checks .exists()


def _cmdline_bin(name: str) -> Path:
    return _bin(sdk_root() / "cmdline-tools" / "latest" / "bin", name)


def tool_paths() -> dict[str, Path]:
    return {
        "sdkmanager": _cmdline_bin("sdkmanager"),
        "avdmanager": _cmdline_bin("avdmanager"),
        "adb": _bin(sdk_root() / "platform-tools", "adb"),
        "emulator": _bin(sdk_root() / "emulator", "emulator"),
    }


# --- Java ---------------------------------------------------------------------
def _java_major(java: str) -> int:
    """Best-effort major version of a java binary (0 if it can't be determined)."""
    try:
        out = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=15,
                             creationflags=(subprocess.CREATE_NO_WINDOW if _IS_WIN else 0))
        text = (out.stderr or "") + (out.stdout or "")
        import re
        m = re.search(r'version "(\d+)(?:\.(\d+))?', text)
        if not m:
            return 0
        major = int(m.group(1))
        # Legacy "1.8" scheme → real major is the minor (8).
        return int(m.group(2)) if major == 1 and m.group(2) else major
    except Exception:  # noqa: BLE001
        return 0


def _find_java(min_major: int = 17) -> Optional[str]:
    """Return a path to a usable `java` (>= min_major), preferring our portable JRE.

    A system Java older than 17 is ignored so `install()` fetches the portable JRE
    instead of failing inside sdkmanager with a cryptic class-version error.
    """
    # Our own portable JRE is always the right version — use it first.
    local = _bin(jre_dir() / "bin", "java")
    if local.exists():
        return str(local)
    if jre_dir().exists():
        for sub in jre_dir().iterdir():
            cand = _bin(sub / "bin", "java")
            if cand.exists():
                return str(cand)
    candidates: list[str] = []
    jh = os.environ.get("JAVA_HOME")
    if jh and _bin(Path(jh) / "bin", "java").exists():
        candidates.append(str(_bin(Path(jh) / "bin", "java")))
    which = shutil.which("java")
    if which:
        candidates.append(which)
    for c in candidates:
        if _java_major(c) >= min_major:
            return c
    return None


def _tool_env() -> dict[str, str]:
    """Environment for SDK tools: JAVA_HOME + ANDROID_SDK_ROOT + ANDROID_AVD_HOME."""
    env = dict(os.environ)
    java = _find_java()
    if java:
        env["JAVA_HOME"] = str(Path(java).parent.parent)
    env["ANDROID_SDK_ROOT"] = str(sdk_root())
    env["ANDROID_HOME"] = str(sdk_root())
    env["ANDROID_AVD_HOME"] = str(avd_home())
    return env


# --- Status -------------------------------------------------------------------
def accel_check() -> dict[str, Any]:
    """Ask the emulator whether hardware acceleration is available.

    A black-screen / never-boots emulator is very often missing accel. `emulator
    -accel-check` reports it without booting anything. Returns {ok, detail}.
    """
    emu = tool_paths()["emulator"]
    if not emu.exists():
        return {"ok": None, "detail": "emulator not installed yet"}
    try:
        out = subprocess.run([str(emu), "-accel-check"], capture_output=True, text=True,
                             timeout=25, env=_tool_env(),
                             creationflags=(subprocess.CREATE_NO_WINDOW if _IS_WIN else 0))
        text = ((out.stdout or "") + (out.stderr or "")).strip()
        return {"ok": out.returncode == 0, "detail": text.splitlines()[-1] if text else ""}
    except Exception as exc:  # noqa: BLE001
        return {"ok": None, "detail": str(exc)}


def status() -> dict[str, Any]:
    """Report what's installed so the UI can decide: setup screen vs ready."""
    tp = tool_paths()
    comps = {k: v.exists() for k, v in tp.items()}
    img_ok = (sdk_root() / "system-images").exists() and any(
        (sdk_root() / "system-images").rglob("system.img")
    ) if (sdk_root() / "system-images").exists() else False
    java = _find_java()
    ready = bool(comps["adb"] and comps["emulator"] and img_ok)
    return {
        "ready": ready,
        "java": bool(java),
        "components": comps,
        "system_image": img_ok,
        "sdk_root": str(sdk_root()),
        "image_pkg": SYSTEM_IMAGE,
        "gpu_mode": ANDROID_GPU,
        "scrcpy": bool(scrcpy_exe()),
        "accel": accel_check() if comps["emulator"] else {"ok": None, "detail": ""},
    }


# --- Install (streamed) -------------------------------------------------------
class InstallState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.lines: list[str] = []
        self.running = False
        self.done = False
        self.error: Optional[str] = None

    def log(self, msg: str) -> None:
        with self.lock:
            self.lines.append(msg.rstrip())

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "running": self.running,
                "done": self.done,
                "error": self.error,
                "lines": list(self.lines[-400:]),
            }


install_state = InstallState()


def _download(url: str, dest: Path, log: Callable[[str], None]) -> None:
    log(f"↓ downloading {url.split('/')[-1]} …")
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "ManyFaces"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length", 0))
        done = 0
        last = 0.0
        while True:
            chunk = r.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            now = time.time()
            if now - last > 0.7:
                mb = done / 1e6
                pct = f" ({done*100//total}%)" if total else ""
                log(f"   {mb:,.0f} MB{pct}")
                last = now
    log(f"   saved {dest.name}")


def _unzip(src: Path, dest: Path, log: Callable[[str], None]) -> None:
    log(f"⇲ extracting {src.name} …")
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(src) as z:
        z.extractall(dest)


def _run(cmd: list[str], log: Callable[[str], None], input_text: str | None = None,
         timeout: int | None = None) -> int:
    """Run a tool, streaming combined output to `log`. Returns exit code."""
    log("$ " + " ".join(Path(c).name if os.path.sep in c else c for c in cmd))
    cwd = str(sdk_root()) if sdk_root().exists() else None
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=_tool_env(),
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
        creationflags=(subprocess.CREATE_NO_WINDOW if _IS_WIN else 0),
    )
    if input_text is not None and proc.stdin:
        try:
            proc.stdin.write(input_text)
            proc.stdin.flush()
            proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
    start = time.time()
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            log(line)
        if timeout and time.time() - start > timeout:
            proc.kill()
            log("   ! timed out")
            return 1
    return proc.wait()


def _ensure_java(log: Callable[[str], None]) -> None:
    if _find_java():
        log("✓ Java found")
        return
    system = platform.system()
    url = _JRE.get(system)
    if not url:
        raise RuntimeError("No Java found and no portable JRE URL for this OS. Install JDK 17+.")
    if system == "Windows":
        zpath = _root() / "jre.zip"
        _download(url, zpath, log)
        _unzip(zpath, jre_dir(), log)
        zpath.unlink(missing_ok=True)
    else:  # tar.gz
        import tarfile
        tpath = _root() / "jre.tar.gz"
        _download(url, tpath, log)
        jre_dir().mkdir(parents=True, exist_ok=True)
        with tarfile.open(tpath) as t:
            t.extractall(jre_dir())
        tpath.unlink(missing_ok=True)
    if not _find_java():
        raise RuntimeError("Portable JRE extracted but no java binary found.")
    log("✓ portable Java installed")


def _ensure_cmdline_tools(log: Callable[[str], None]) -> None:
    if tool_paths()["sdkmanager"].exists():
        log("✓ cmdline-tools present")
        return
    url = _CT.get(platform.system())
    if not url:
        raise RuntimeError("No cmdline-tools download URL for this OS.")
    zpath = _root() / "cmdline-tools.zip"
    _download(url, zpath, log)
    # Zip contains a top-level "cmdline-tools/" dir; sdkmanager expects it under
    # <sdk>/cmdline-tools/latest/. Extract to a temp, then move into place.
    tmp = _root() / "_ct_tmp"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    _unzip(zpath, tmp, log)
    latest = sdk_root() / "cmdline-tools" / "latest"
    latest.parent.mkdir(parents=True, exist_ok=True)
    if latest.exists():
        shutil.rmtree(latest, ignore_errors=True)
    inner = tmp / "cmdline-tools"
    shutil.move(str(inner if inner.exists() else tmp), str(latest))
    shutil.rmtree(tmp, ignore_errors=True)
    zpath.unlink(missing_ok=True)
    log("✓ cmdline-tools installed")


def _ensure_scrcpy(log: Callable[[str], None]) -> None:
    if scrcpy_exe():
        log("✓ scrcpy present")
        return
    url = _SCRCPY.get(platform.system())
    if not url:
        log("• scrcpy auto-download only on Windows; skipping (install scrcpy via your "
            "package manager for the reliable mirror window)")
        return
    zpath = _root() / "scrcpy.zip"
    _download(url, zpath, log)
    _unzip(zpath, scrcpy_dir(), log)
    zpath.unlink(missing_ok=True)
    if scrcpy_exe():
        log("✓ scrcpy installed")
    else:
        log("! scrcpy extracted but scrcpy.exe not found")


def _do_install() -> None:
    log = install_state.log
    try:
        _root().mkdir(parents=True, exist_ok=True)
        sdk_root().mkdir(parents=True, exist_ok=True)
        avd_home().mkdir(parents=True, exist_ok=True)
        _ensure_java(log)
        _ensure_cmdline_tools(log)

        sdkmgr = str(tool_paths()["sdkmanager"])
        # Accept all licenses non-interactively (a stream of prompts wanting "y").
        log("• accepting SDK licenses")
        _run([sdkmgr, "--sdk_root=" + str(sdk_root()), "--licenses"], log,
             input_text="y\n" * 60, timeout=300)
        # Install the actual components (this is the multi-GB step).
        log("• installing platform-tools, emulator, system image (large)")
        rc = _run([sdkmgr, "--sdk_root=" + str(sdk_root()), *_ANDROID_API_PKGS], log,
                  input_text="y\n" * 20, timeout=3600)
        if rc != 0:
            raise RuntimeError(f"sdkmanager exited with code {rc}")
        # scrcpy: the reliable mirror window (the emulator's own window black-screens
        # on many machines). Best-effort — a failure here doesn't block the engine.
        try:
            _ensure_scrcpy(log)
        except Exception as exc:  # noqa: BLE001
            log(f"! scrcpy install skipped: {exc}")
        st = status()
        if not st["ready"]:
            raise RuntimeError(f"Install finished but components missing: {st['components']}")
        log("✅ Android engine ready")
        with install_state.lock:
            install_state.done = True
    except Exception as exc:  # noqa: BLE001
        install_state.log(f"✖ {type(exc).__name__}: {exc}")
        with install_state.lock:
            install_state.error = str(exc)
    finally:
        with install_state.lock:
            install_state.running = False


def start_install() -> bool:
    """Kick off installation in a background thread. Returns False if already running."""
    with install_state.lock:
        if install_state.running:
            return False
        install_state.running = True
        install_state.done = False
        install_state.error = None
        install_state.lines = []
    threading.Thread(target=_do_install, daemon=True, name="android-install").start()
    return True


# --- AVD lifecycle ------------------------------------------------------------
# Map our phone presets to emulator device skins + a friendly window.
_DEVICE_SKIN = {
    "Google Pixel 6": "pixel_6", "Google Pixel 7": "pixel_7", "Google Pixel 8": "pixel_8",
    "Google Pixel 9": "pixel_8", "Samsung Galaxy S24": "pixel_8", "OnePlus 12": "pixel_8",
}


def _avd_name(profile_id: str) -> str:
    return "mf_" + profile_id.replace("-", "")[:20]


def _free_port() -> int:
    """Emulator console ports must be even, 5554–5682."""
    for port in range(5554, 5682, 2):
        with socket.socket() as s:
            s.settimeout(0.2)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("No free emulator port (too many devices running).")


def _avd_exists(name: str) -> bool:
    return (avd_home() / f"{name}.avd").exists() and (avd_home() / f"{name}.ini").exists()


# Data-partition size for a fresh AVD. The emulator's default is ~7.4 GB, which fails
# to even start on a tight disk ("Not enough space to create userdata partition").
# A browser profile needs far less; ~4.5 GB is plenty and roughly halves the footprint.
DATA_PARTITION = os.environ.get("ANTIDETECT_ANDROID_DATA_MB", "4608") + "M"
# Render resolution for the phone as "WxHxDPI". Fewer pixels = far less GPU/CPU load
# on weak/integrated GPUs — the single biggest perf lever. 720×1600 still looks like a
# real phone. Set ANTIDETECT_ANDROID_LCD="" to keep the device preset's native res.
ANDROID_LCD = os.environ.get("ANTIDETECT_ANDROID_LCD", "720x1600x320")


def _patch_avd_config(name: str, props: dict[str, str]) -> None:
    """Set key=value pairs in an AVD's config.ini (replacing any existing keys)."""
    cfg = avd_home() / f"{name}.avd" / "config.ini"
    if not cfg.exists():
        return
    try:
        lines = cfg.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:  # noqa: BLE001
        return
    lines = [ln for ln in lines if ln.split("=", 1)[0].strip() not in props]
    lines += [f"{k}={v}" for k, v in props.items()]
    try:
        cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _tune_avd_config(name: str) -> None:
    """Apply perf/size settings to a not-yet-booted AVD: smaller data partition and a
    lighter render resolution. Skipped once userdata exists (would fight the snapshot)."""
    props = {"disk.dataPartition.size": DATA_PARTITION}
    if ANDROID_LCD:
        try:
            w, h, dpi = ANDROID_LCD.lower().split("x")
            props.update({"hw.lcd.width": w, "hw.lcd.height": h, "hw.lcd.density": dpi})
        except ValueError:
            pass
    _patch_avd_config(name, props)


def ensure_avd(profile, log: Callable[[str], None]) -> str:
    name = _avd_name(profile.id)
    if _avd_exists(name):
        # Apply perf/size tuning if this AVD predates it and hasn't booted yet
        # (harmless once userdata exists — we skip to avoid fighting the snapshot).
        if not (avd_home() / f"{name}.avd" / "userdata.img").exists():
            _tune_avd_config(name)
        return name
    device = "pixel_6"
    try:
        device = _DEVICE_SKIN.get(profile.fingerprint.to_fingerprint().device_name or "", "pixel_6")
    except Exception:  # noqa: BLE001
        pass
    avdmgr = str(tool_paths()["avdmanager"])
    log(f"• creating AVD {name} ({device})")
    rc = _run([avdmgr, "create", "avd", "-n", name, "-k", SYSTEM_IMAGE,
               "-d", device, "--force"], log, input_text="no\n", timeout=180)
    if rc != 0 or not _avd_exists(name):
        raise RuntimeError(f"Failed to create AVD (code {rc}).")
    _tune_avd_config(name)
    return name


def delete_avd(profile_id: str) -> None:
    """Remove a profile's AVD (dir + .ini + logs) so deleted profiles don't leak GBs.

    Each AVD is ~3–4 GB; without this, deleting a profile orphaned its whole device.
    """
    name = _avd_name(profile_id)
    home = avd_home()
    for path in (home / f"{name}.avd", home / f"{name}.ini", home / f"{name}.boot.log"):
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
        except Exception:  # noqa: BLE001
            pass


class AndroidSession:
    def __init__(self, profile) -> None:
        self.profile = profile
        self.proc: Optional[subprocess.Popen] = None
        self.mirror: Optional[subprocess.Popen] = None   # scrcpy window
        self.serial: Optional[str] = None
        self.error: Optional[str] = None
        self.state = "stopped"                           # launching|running|error|stopped
        self._thread: Optional[threading.Thread] = None
        self._log: list[str] = []
        self._logf = None
        self.boot_log_path: Optional[Path] = None
        self._stopping = False

    def log(self, m: str) -> None:
        self._log.append(m)

    @property
    def status(self) -> str:
        if self.state == "running" and not (self.proc and self.proc.poll() is None):
            return "stopped"   # device died since
        return self.state

    @property
    def running(self) -> bool:
        return self.status == "running"

    def _log_tail(self, n: int = 25) -> str:
        try:
            if self.boot_log_path and self.boot_log_path.exists():
                lines = self.boot_log_path.read_text(errors="replace").splitlines()
                return "\n".join(lines[-n:])
        except Exception:  # noqa: BLE001
            pass
        return ""

    def start(self, start_url: str) -> None:
        """Non-blocking: boot the device on a background thread (first boot is minutes)."""
        self.state = "launching"
        self.error = None
        self._thread = threading.Thread(target=self._boot, args=(start_url,),
                                        daemon=True, name=f"android-{self.profile.id}")
        self._thread.start()

    def _boot(self, start_url: str) -> None:
        try:
            self._launch(start_url)
            self.state = "running"
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            self.state = "error"
            try:
                self.stop()
            except Exception:  # noqa: BLE001
                pass

    def _launch(self, start_url: str) -> None:
        name = ensure_avd(self.profile, self.log)
        port = _free_port()
        self.serial = f"emulator-{port}"
        tp = tool_paths()
        use_mirror = scrcpy_exe() is not None
        # -gpu host        → hardware rendering (fast); safe because we run headless.
        # -no-audio        → avoids audio-backend hangs on headless/VM hosts.
        # -no-boot-anim    → skip the boot animation to save startup time.
        # -no-window       → hide the emulator's own (black-screening) Qt window; scrcpy
        #                    mirrors the device instead (only when scrcpy is available).
        # quickboot        → snapshot restore on later launches (seconds, not minutes).
        args = [str(tp["emulator"]), "@" + name, "-port", str(port),
                "-gpu", ANDROID_GPU, "-no-boot-anim", "-no-audio",
                "-memory", ANDROID_MEMORY, "-cores", ANDROID_CORES,
                "-netdelay", "none", "-netspeed", "full"]
        if ANDROID_COLDBOOT:
            args += ["-no-snapshot"]
        if use_mirror:
            args.append("-no-window")
        proxy = self.profile.proxy
        if proxy.is_set and proxy.type in ("http", "https"):
            # The emulator only takes an unauthenticated HTTP proxy on the cmdline.
            args += ["-http-proxy", f"{proxy.host}:{proxy.port}"]
        self.log("$ " + " ".join(Path(a).name if os.sep in a else a for a in args))
        # Capture the emulator's own output — without it a failed boot is undiagnosable.
        avd_home().mkdir(parents=True, exist_ok=True)
        self.boot_log_path = avd_home() / f"{name}.boot.log"
        self._logf = open(self.boot_log_path, "w", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(
            args, env=_tool_env(),
            stdout=self._logf, stderr=subprocess.STDOUT,
            creationflags=(subprocess.CREATE_NO_WINDOW if _IS_WIN else 0),
        )
        self._wait_boot()
        self._perf_tune()
        self._open_url(start_url)
        if use_mirror:
            self._start_mirror()

    def _perf_tune(self) -> None:
        """Disable guest UI animations — the cheapest, biggest 'feels faster' win on
        weak hardware (transitions stop eating GPU/CPU time)."""
        for key in ("window_animation_scale", "transition_animation_scale",
                    "animator_duration_scale"):
            try:
                self._adb("shell", "settings", "put", "global", key, "0", timeout=10)
            except Exception:  # noqa: BLE001
                pass

    def _start_mirror(self) -> None:
        """Open scrcpy — a reliable window mirroring the device, with real touch input."""
        exe = scrcpy_exe()
        if not exe:
            return
        title = getattr(self.profile, "name", "Android")
        env = _tool_env()
        # Point scrcpy at OUR adb so it talks to the same server the emulator registered on.
        env["ADB"] = str(tool_paths()["adb"])
        # Cap fps + bitrate so mirroring itself doesn't tax a weak GPU; --max-size
        # keeps the stream light. These make the window feel much smoother.
        args = [str(exe), "-s", self.serial or "", "--window-title", f"📱 {title}",
                "--stay-awake", "--no-audio", "--max-size", "1024",
                "--max-fps", "30", "--video-bit-rate", "4M"]
        self.log("$ " + " ".join(Path(a).name if os.sep in a else a for a in args))
        try:
            self.mirror = subprocess.Popen(
                args, env=env, cwd=str(exe.parent),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=(subprocess.CREATE_NO_WINDOW if _IS_WIN else 0),
            )
        except Exception as exc:  # noqa: BLE001
            self.log(f"! scrcpy failed to start: {exc}")
            return
        # When the user closes the mirror window, shut the (headless) device down too,
        # so we never leak an invisible VM.
        threading.Thread(target=self._watch_mirror, daemon=True,
                         name=f"scrcpy-watch-{self.serial}").start()

    def _watch_mirror(self) -> None:
        if not self.mirror:
            return
        try:
            self.mirror.wait()
        except Exception:  # noqa: BLE001
            return
        if not self._stopping:
            self.stop()

    def _adb(self, *a: str, timeout: int = 30) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(tool_paths()["adb"]), "-s", self.serial or "", *a],
            env=_tool_env(), capture_output=True, text=True, timeout=timeout,
            creationflags=(subprocess.CREATE_NO_WINDOW if _IS_WIN else 0),
        )

    def _wait_boot(self, timeout: int = 300) -> None:
        deadline = time.time() + timeout
        # Poll directly (no blocking `wait-for-device`, which would hang the full
        # timeout if the emulator dies at startup). Each loop re-checks the process so a
        # crash — e.g. "not enough disk space" — fails in ~2s, not minutes.
        while time.time() < deadline:
            if self.proc is None or self.proc.poll() is not None:
                raise RuntimeError(self._boot_failure_reason())  # emulator process died
            try:
                out = self._adb("shell", "getprop", "sys.boot_completed", timeout=10).stdout.strip()
                if out == "1":
                    self._wait_settled()
                    return
            except Exception:  # noqa: BLE001
                pass
            time.sleep(2)
        raise RuntimeError(
            "Android didn't finish booting in time (a black screen that never clears is "
            "usually the GPU mode — this build already forces software rendering; if it "
            "persists, hardware acceleration/WHPX is likely unavailable)."
            + self._log_block()
        )

    def _log_block(self) -> str:
        tail = self._log_tail()
        return f"\n\nEmulator log:\n{tail}" if tail else ""

    def _boot_failure_reason(self) -> str:
        """Turn the emulator's exit into a specific, actionable message."""
        tail = self._log_tail(40)
        low = tail.lower()
        if "not enough space" in low or "userdata partition" in low:
            return ("Not enough free disk space to start the Android device. Free up a "
                    "few GB on your system drive (each device needs ~4–5 GB), then try "
                    "again." + self._log_block())
        if "hax" in low or "whpx" in low or "hvf" in low or "kvm" in low or "acceleration" in low:
            return ("The Android emulator couldn't get hardware acceleration. On Windows, "
                    "enable 'Windows Hypervisor Platform' in Windows Features." + self._log_block())
        return ("The Android emulator process exited during boot." + self._log_block())

    def _wait_settled(self, settle: float = 8.0) -> None:
        """`sys.boot_completed` fires while services are still coming up — launching an
        app right then causes "Process system isn't responding". Wait for the package
        manager to answer, then let the system breathe."""
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                out = self._adb("shell", "pm", "list", "packages", timeout=15).stdout
                if "package:" in out:
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(2)
        time.sleep(settle)

    def _open_url(self, url: str) -> None:
        if not url or url == "about:blank":
            return
        # Retry: the browser package can still be warming up right after boot.
        for attempt in range(3):
            try:
                # Generic VIEW intent → opens the image's default browser (Chrome on Play images).
                r = self._adb("shell", "am", "start", "-a", "android.intent.action.VIEW",
                              "-d", url, timeout=20)
                if "Error" not in (r.stderr or "") and "Error" not in (r.stdout or ""):
                    return
            except Exception:  # noqa: BLE001
                pass
            time.sleep(4)

    def stop(self) -> None:
        self._stopping = True
        if self.state != "error":
            self.state = "stopped"
        # Close the mirror window first.
        if self.mirror and self.mirror.poll() is None:
            try:
                self.mirror.terminate()
                self.mirror.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    self.mirror.kill()
                except Exception:  # noqa: BLE001
                    pass
        try:
            if self.serial:
                subprocess.run([str(tool_paths()["adb"]), "-s", self.serial, "emu", "kill"],
                               env=_tool_env(), timeout=15,
                               creationflags=(subprocess.CREATE_NO_WINDOW if _IS_WIN else 0))
        except Exception:  # noqa: BLE001
            pass
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                try:
                    self.proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        if self._logf:
            try:
                self._logf.close()
            except Exception:  # noqa: BLE001
                pass


class AndroidManager:
    def __init__(self) -> None:
        self._sessions: dict[str, AndroidSession] = {}
        self._lock = threading.Lock()

    def start(self, profile) -> None:
        # This check is synchronous & fast, so a "not installed" error still surfaces
        # immediately to the caller; the boot itself runs in the background.
        if not status()["ready"]:
            raise RuntimeError(
                "The Android engine isn't installed yet. Open the Android setup and "
                "run the one-click install first."
            )
        with self._lock:
            s = self._sessions.get(profile.id)
            if s and s.status in ("launching", "running"):
                return
            s = AndroidSession(profile)
            self._sessions[profile.id] = s
        from .browser import normalize_start_url
        s.start(normalize_start_url(profile.start_url))  # non-blocking

    def stop(self, profile_id: str) -> bool:
        with self._lock:
            s = self._sessions.pop(profile_id, None)
        if s:
            s.stop()
            return True
        return False

    def is_running(self, profile_id: str) -> bool:
        s = self._sessions.get(profile_id)
        return bool(s and s.running)

    def status(self, profile_id: str) -> str:
        s = self._sessions.get(profile_id)
        return s.status if s else "stopped"

    def error(self, profile_id: str) -> Optional[str]:
        s = self._sessions.get(profile_id)
        return s.error if s else None

    def running_ids(self) -> list[str]:
        return [pid for pid, s in self._sessions.items() if s.status in ("running", "launching")]

    def stop_all(self) -> None:
        for pid in list(self._sessions.keys()):
            self.stop(pid)


manager = AndroidManager()
