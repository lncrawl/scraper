"""Finding a browser that is already installed.

A solver needs a binary before it can do anything, and asking every caller to supply a
path is asking them to solve this. It is not a hard problem, only a wide one: the answer
is a different shape on each platform, and ``shutil.which`` answers for exactly one of
them. On macOS a browser lives inside an application bundle, on Windows under one of
four program directories, and on Linux it may be a PATH entry, a distribution path, or a
flatpak export. A PATH scan alone finds Chrome on Linux and reports the same machine as
having no browser on macOS or Windows, with two of them installed.

Chromium-family builds are looked for under six brands, because they are all the same
engine driven over the same protocol — an installed Brave answers CDP exactly as Chrome
does, and refusing it means falling back to no solver at all.

The finders return every match. Which one to launch is a separate judgement, and
:func:`pick_chromium` and :func:`pick_firefox` make the cheap version of it: the shortest
path. That prefers ``/usr/bin/chromium`` over a flatpak wrapper or a beta channel sitting
beside it, which is the right guess often enough to be worth not asking.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Iterable, List, Optional

logger = logging.getLogger(__name__)

_MAC = sys.platform == "darwin"
_WINDOWS = sys.platform in ("win32", "cygwin")
_POSIX = not _WINDOWS
_LINUX = sys.platform.startswith("linux")

_WINDOWS_ROOTS = ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA", "PROGRAMW6432")


def _candidates(
    posix_names: Iterable[str],
    linux_paths: Iterable[str],
    mac_paths: Iterable[str],
    windows_dirs: Iterable[str],
    windows_exes: Iterable[str],
) -> List[str]:
    """Every place this browser could be, before checking whether it is."""
    found: List[str] = []

    if _POSIX:
        for entry in os.environ.get("PATH", "").split(os.pathsep):
            found.extend(os.path.join(entry, name) for name in posix_names)
        if _LINUX:
            found.extend(linux_paths)
        if _MAC:
            found.extend(mac_paths)

    if _WINDOWS:
        for root in _WINDOWS_ROOTS:
            base = os.environ.get(root)
            if not base:
                continue
            for directory in windows_dirs:
                # Written with forward slashes above and joined per-platform here, so
                # the tables stay readable.
                parts = os.sep.join(directory.split("/"))
                found.extend(os.path.join(base, parts, exe) for exe in windows_exes)

    return found


def _runnable(path: str) -> bool:
    """Whether this path is a program that could actually be launched.

    A named function rather than the expression inline because the distribution paths
    above are absolute: nothing a test does to ``PATH`` hides them, so this is the only
    seam at which a test can be told what filesystem to believe in.
    """
    return os.path.exists(path) and os.access(path, os.X_OK)


def _executables(*args: Iterable[str]) -> List[str]:
    """The candidates that exist and can actually be run."""
    usable: List[str] = []
    seen = set()
    for candidate in _candidates(*args):  # type: ignore[arg-type]
        path = os.path.normpath(candidate)
        if path in seen:
            continue
        seen.add(path)
        if _runnable(path):
            usable.append(path)
    return usable


def find_chrome() -> List[str]:
    """Every Google Chrome or Chromium build installed."""
    return _executables(
        (
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "chrome",
            "com.google.Chrome",
        ),
        ("/var/lib/flatpak/exports/bin/com.google.Chrome",),
        (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ),
        (
            "Google/Chrome/Application",
            "Google/Chrome Beta/Application",
            "Google/Chrome Canary/Application",
        ),
        ("chrome.exe",),
    )


def find_edge() -> List[str]:
    """Every Microsoft Edge build installed."""
    return _executables(
        (
            "microsoft-edge",
            "microsoft-edge-stable",
            "microsoft-edge-beta",
            "microsoft-edge-dev",
            "msedge",
        ),
        (
            "/usr/share/microsoft-edge/microsoft-edge",
            "/var/lib/flatpak/exports/bin/com.microsoft.Edge",
        ),
        (
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Microsoft Edge Beta.app/Contents/MacOS/Microsoft Edge Beta",
            "/Applications/Microsoft Edge Dev.app/Contents/MacOS/Microsoft Edge Dev",
            "/Applications/Microsoft Edge Canary.app/Contents/MacOS/Microsoft Edge Canary",
        ),
        (
            "Microsoft/Edge/Application",
            "Microsoft/Edge Beta/Application",
            "Microsoft/Edge Dev/Application",
            "Microsoft/Edge Canary/Application",
        ),
        ("msedge.exe",),
    )


def find_brave() -> List[str]:
    """Every Brave build installed."""
    return _executables(
        ("brave-browser", "brave-browser-stable", "brave"),
        (
            "/var/lib/flatpak/exports/bin/com.brave.Browser",
            "/usr/share/brave-browser/brave-browser",
        ),
        ("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",),
        (
            "BraveSoftware/Brave-Browser/Application",
            "BraveSoftware/Brave-Browser-Beta/Application",
            "BraveSoftware/Brave-Browser-Nightly/Application",
        ),
        ("brave.exe",),
    )


def find_vivaldi() -> List[str]:
    """Every Vivaldi build installed."""
    return _executables(
        ("vivaldi", "vivaldi-stable", "vivaldi-snapshot"),
        ("/usr/share/vivaldi/vivaldi", "/var/lib/flatpak/exports/bin/com.vivaldi.Vivaldi"),
        ("/Applications/Vivaldi.app/Contents/MacOS/Vivaldi",),
        ("Vivaldi/Application",),
        ("vivaldi.exe",),
    )


def find_yandex() -> List[str]:
    """Every Yandex Browser build installed."""
    return _executables(
        ("yandex-browser", "yandex-browser-stable", "yandex-browser-beta"),
        ("/usr/share/yandex-browser/yandex_browser",),
        ("/Applications/Yandex.app/Contents/MacOS/Yandex",),
        ("Yandex/YandexBrowser/Application",),
        ("browser.exe",),
    )


def find_whale() -> List[str]:
    """Every Naver Whale build installed."""
    return _executables(
        ("naver-whale", "naver-whale-stable"),
        ("/usr/share/naver-whale/naver-whale",),
        ("/Applications/Whale.app/Contents/MacOS/Whale",),
        ("Naver/Naver Whale/Application",),
        ("whale.exe",),
    )


def find_firefox() -> List[str]:
    """Every Firefox build installed, ESR and the forks included.

    LibreWolf is here for the same reason Brave is in the Chromium list: it is Firefox,
    it speaks WebDriver BiDi, and skipping it means no solver on a machine that has one.

    Deliberately no ``/snap/bin/firefox``. A snap-confined Firefox cannot reach a profile
    directory under a dotfile path, which is where the solver puts one, so it would be
    found and then fail at launch — worse than not being found. Ubuntu's ``/usr/bin``
    shim still reaches the snap, so this narrows the odds rather than removing them.
    """
    return _executables(
        (
            "firefox",
            "firefox-esr",
            "firefox-bin",
            "firefox-developer-edition",
            "firefox-nightly",
            "librewolf",
        ),
        (
            "/usr/lib/firefox/firefox",
            "/usr/lib/firefox-esr/firefox-esr",
            "/var/lib/flatpak/exports/bin/org.mozilla.firefox",
        ),
        (
            "/Applications/Firefox.app/Contents/MacOS/firefox",
            "/Applications/Firefox Developer Edition.app/Contents/MacOS/firefox",
            "/Applications/Firefox Nightly.app/Contents/MacOS/firefox",
            "/Applications/LibreWolf.app/Contents/MacOS/librewolf",
        ),
        (
            "Mozilla Firefox",
            "Firefox Developer Edition",
            "Firefox Nightly",
            "LibreWolf",
        ),
        ("firefox.exe", "librewolf.exe"),
    )


def find_chromium() -> List[str]:
    """Every Chromium-family build installed, across all six brands.

    Ordered by brand and deduplicated, since one binary can be reached by more than one
    of the paths above.
    """
    seen = set()
    results: List[str] = []
    for path in (
        find_chrome() + find_edge() + find_brave() + find_vivaldi() + find_yandex() + find_whale()
    ):
        if path not in seen:
            seen.add(path)
            results.append(path)
    return results


def pick_chromium() -> Optional[str]:
    """One Chromium-family browser to drive over CDP, or ``None``."""
    return _shortest(find_chromium())


def pick_firefox() -> Optional[str]:
    """One Firefox to drive over WebDriver BiDi, or ``None``."""
    return _shortest(find_firefox())


def _shortest(available: List[str]) -> Optional[str]:
    if not available:
        return None
    chosen = min(available, key=len)
    logger.debug("picked %s from %d installed", chosen, len(available))
    return chosen
