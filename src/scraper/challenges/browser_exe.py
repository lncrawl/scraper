import logging
import os
import platform
import sys
from functools import lru_cache
from typing import Iterable, List, Optional

logger = logging.getLogger(__name__)


def _find_executables(
    windows_path: Iterable[str],
    mac_app_path: Iterable[str],
    linux_app_path: Iterable[str],
    posix_app_name: Iterable[str],
    windows_exe_name: Iterable[str],
) -> List[str]:
    candidates = []
    if sys.platform.startswith(("darwin", "cygwin", "linux")):
        for item in os.environ["PATH"].split(os.pathsep):
            for subitem in posix_app_name:
                candidates.append(os.path.join(item, subitem))
        if platform.system() == "Linux":
            candidates += list(linux_app_path)
        if platform.system() == "Darwin":
            candidates += list(mac_app_path)

    if platform.system() == "Windows":
        for path in (
            "PROGRAMFILES",
            "PROGRAMFILES(X86)",
            "LOCALAPPDATA",
            "PROGRAMW6432",
        ):
            if path in os.environ:
                item = os.environ[path]
                for inner_path in windows_path:
                    subitem = os.sep.join(inner_path.split("/"))
                    for exe_name in windows_exe_name:
                        candidates.append(os.path.join(item, subitem, exe_name))

    valids: List[str] = []
    for candidate in candidates:
        item = os.path.normpath(candidate)
        if os.path.exists(item) and os.access(item, os.X_OK):
            logger.debug(f"{item} is a valid!")
            valids.append(item)

    return valids


def _find_chrome_executables() -> List[str]:
    """
    Scans standard cross-platform operating system installations
    to auto-detect the chrome, beta, canary, chromium executable path.
    """
    return _find_executables(
        posix_app_name=(
            "google-chrome",
            "chromium",
            "chromium-browser",
            "chrome",
            "google-chrome-stable",
            "com.google.Chrome",
        ),
        linux_app_path=[
            "/var/lib/flatpak/exports/bin/com.google.Chrome",
        ],
        mac_app_path=[
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ],
        windows_path=[
            "Google/Chrome/Application",
            "Google/Chrome Beta/Application",
            "Google/Chrome Canary/Application",
        ],
        windows_exe_name=[
            "chrome.exe",
        ],
    )


def _find_edge_executables() -> List[str]:
    """
    Scans standard cross-platform operating system installations
    to auto-detect the Microsoft Edge executable path.
    """
    return _find_executables(
        posix_app_name=(
            "microsoft-edge",
            "microsoft-edge-stable",
            "microsoft-edge-beta",
            "microsoft-edge-dev",
            "msedge",
        ),
        linux_app_path=[
            "/usr/share/microsoft-edge/microsoft-edge",
            "/var/lib/flatpak/exports/bin/com.microsoft.Edge",
        ],
        mac_app_path=[
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Microsoft Edge Beta.app/Contents/MacOS/Microsoft Edge Beta",
            "/Applications/Microsoft Edge Dev.app/Contents/MacOS/Microsoft Edge Dev",
            "/Applications/Microsoft Edge Canary.app/Contents/MacOS/Microsoft Edge Canary",
        ],
        windows_path=[
            "Microsoft/Edge/Application",
            "Microsoft/Edge Beta/Application",
            "Microsoft/Edge Dev/Application",
            "Microsoft/Edge Canary/Application",
        ],
        windows_exe_name=[
            "msedge.exe",
        ],
    )


def _find_brave_executables() -> List[str]:
    """
    Scans standard cross-platform operating system installations
    to auto-detect the Brave Browser executable path.
    """
    return _find_executables(
        posix_app_name=(
            "brave-browser",
            "brave-browser-stable",
            "brave",
        ),
        linux_app_path=[
            "/var/lib/flatpak/exports/bin/com.brave.Browser",
            "/usr/share/brave-browser/brave-browser",
        ],
        mac_app_path=[
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ],
        windows_path=[
            "BraveSoftware/Brave-Browser/Application",
            "BraveSoftware/Brave-Browser-Beta/Application",
            "BraveSoftware/Brave-Browser-Nightly/Application",
        ],
        windows_exe_name=[
            "brave.exe",
        ],
    )


def _find_vivaldi_executables() -> List[str]:
    """
    Scans standard cross-platform operating system installations
    to auto-detect the Vivaldi browser executable path.
    """
    return _find_executables(
        posix_app_name=(
            "vivaldi",
            "vivaldi-stable",
            "vivaldi-snapshot",
        ),
        linux_app_path=[
            "/usr/share/vivaldi/vivaldi",
            "/var/lib/flatpak/exports/bin/com.vivaldi.Vivaldi",
        ],
        mac_app_path=[
            "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi",
        ],
        windows_path=[
            "Vivaldi/Application",
        ],
        windows_exe_name=[
            "vivaldi.exe",
        ],
    )


def _find_yandex_executables() -> List[str]:
    """
    Scans standard cross-platform operating system installations
    to auto-detect the Yandex Browser executable path.
    """
    return _find_executables(
        posix_app_name=(
            "yandex-browser",
            "yandex-browser-stable",
            "yandex-browser-beta",
        ),
        linux_app_path=[
            "/usr/share/yandex-browser/yandex_browser",
        ],
        mac_app_path=[
            "/Applications/Yandex.app/Contents/MacOS/Yandex",
        ],
        windows_path=[
            "Yandex/YandexBrowser/Application",
        ],
        windows_exe_name=[
            "browser.exe",
        ],
    )


def _find_whale_executables() -> List[str]:
    """
    Scans standard cross-platform operating system installations
    to auto-detect the Naver Whale browser executable path.
    """
    return _find_executables(
        posix_app_name=(
            "naver-whale",
            "naver-whale-stable",
        ),
        linux_app_path=[
            "/usr/share/naver-whale/naver-whale",
        ],
        mac_app_path=[
            "/Applications/Whale.app/Contents/MacOS/Whale",
        ],
        windows_path=[
            "Naver/Naver Whale/Application",
        ],
        windows_exe_name=[
            "whale.exe",
        ],
    )


@lru_cache(1)
def find_all_chromium_executables() -> List[str]:
    """
    Searches for all available Chromium-based browser executables across
    Chrome, Edge, Brave, Vivaldi, Yandex, and Whale.
    Returns a deduplicated list of valid executable paths.
    """
    seen = set()
    results = []
    for exe in (
        _find_chrome_executables()
        + _find_edge_executables()
        + _find_brave_executables()
        + _find_vivaldi_executables()
        + _find_yandex_executables()
        + _find_whale_executables()
    ):
        if exe not in seen:
            seen.add(exe)
            results.append(exe)
    return results


def pick_executable() -> Optional[str]:
    """
    Searches for all available Chromium-based browser executables across
    Chrome, Edge, Brave, Vivaldi, Yandex, and Whale sequentially.
    Returns the first executable with the shorted path, or None if not found.
    """
    available = find_all_chromium_executables()
    if not available:
        return None
    return min(available, key=len)
