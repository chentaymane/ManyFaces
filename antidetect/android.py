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


def ensure_avd(profile, log: Callable[[str], None]) -> str:
    name = _avd_name(profile.id)
    if _avd_exists(name):
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
    return name


class AndroidSession:
    def __init__(self, profile) -> None:
        self.profile = profile
        self.proc: Optional[subprocess.Popen] = None
        self.serial: Optional[str] = None
        self.error: Optional[str] = None
        self._log: list[str] = []

    def log(self, m: str) -> None:
        self._log.append(m)

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, start_url: str) -> None:
        name = ensure_avd(self.profile, self.log)
        port = _free_port()
        self.serial = f"emulator-{port}"
        tp = tool_paths()
        args = [str(tp["emulator"]), "@" + name, "-port", str(port),
                "-no-snapshot-save", "-no-boot-anim", "-netdelay", "none",
                "-netspeed", "full"]
        proxy = self.profile.proxy
        if proxy.is_set and proxy.type in ("http", "https"):
            # The emulator only takes an unauthenticated HTTP proxy on the cmdline.
            args += ["-http-proxy", f"{proxy.host}:{proxy.port}"]
        self.log("$ " + " ".join(Path(a).name if os.sep in a else a for a in args))
        self.proc = subprocess.Popen(
            args, env=_tool_env(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=(subprocess.CREATE_NO_WINDOW if _IS_WIN else 0),
        )
        self._wait_boot()
        self._open_url(start_url)

    def _adb(self, *a: str, timeout: int = 30) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(tool_paths()["adb"]), "-s", self.serial or "", *a],
            env=_tool_env(), capture_output=True, text=True, timeout=timeout,
            creationflags=(subprocess.CREATE_NO_WINDOW if _IS_WIN else 0),
        )

    def _wait_boot(self, timeout: int = 240) -> None:
        deadline = time.time() + timeout
        # First wait for the device to appear on adb, then for full boot.
        try:
            subprocess.run([str(tool_paths()["adb"]), "-s", self.serial or "", "wait-for-device"],
                           env=_tool_env(), timeout=timeout,
                           creationflags=(subprocess.CREATE_NO_WINDOW if _IS_WIN else 0))
        except Exception:  # noqa: BLE001
            pass
        while time.time() < deadline:
            if not self.running:
                raise RuntimeError("Emulator process exited during boot (virtualization/WHPX issue?).")
            try:
                out = self._adb("shell", "getprop", "sys.boot_completed", timeout=10).stdout.strip()
                if out == "1":
                    return
            except Exception:  # noqa: BLE001
                pass
            time.sleep(2)
        raise RuntimeError("Android did not finish booting in time.")

    def _open_url(self, url: str) -> None:
        if not url or url == "about:blank":
            return
        try:
            # Generic VIEW intent → opens the image's default browser (Chrome on Play images).
            self._adb("shell", "am", "start", "-a", "android.intent.action.VIEW",
                      "-d", url, timeout=20)
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> None:
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


class AndroidManager:
    def __init__(self) -> None:
        self._sessions: dict[str, AndroidSession] = {}
        self._lock = threading.Lock()

    def start(self, profile) -> None:
        if not status()["ready"]:
            raise RuntimeError(
                "The Android engine isn't installed yet. Open the Android setup and "
                "run the one-click install first."
            )
        with self._lock:
            s = self._sessions.get(profile.id)
            if s and s.running:
                return
            s = AndroidSession(profile)
            self._sessions[profile.id] = s
        from .browser import normalize_start_url
        s.start(normalize_start_url(profile.start_url))
        if s.error:
            raise RuntimeError(s.error)

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

    def running_ids(self) -> list[str]:
        return [pid for pid, s in self._sessions.items() if s.running]

    def stop_all(self) -> None:
        for pid in list(self._sessions.keys()):
            self.stop(pid)


manager = AndroidManager()
