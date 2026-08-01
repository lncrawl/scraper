"""Finding an installed browser.

Every test builds a fake filesystem and points the platform switches at it, because the
bug this module exists to fix is platform-shaped: a PATH scan finds Chrome on Linux and
reports the same machine as having none on macOS, with two browsers installed. A test
that only ran the host's own platform would have passed throughout.
"""

from __future__ import annotations

import os
import stat

import pytest

from scraper import browsers


def executable(path):
    """A file that exists and can be run, which is what the finders check for."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(browsers, "_POSIX", True)
    monkeypatch.setattr(browsers, "_LINUX", True)
    monkeypatch.setattr(browsers, "_MAC", False)
    monkeypatch.setattr(browsers, "_WINDOWS", False)


@pytest.fixture
def mac(monkeypatch):
    monkeypatch.setattr(browsers, "_POSIX", True)
    monkeypatch.setattr(browsers, "_LINUX", False)
    monkeypatch.setattr(browsers, "_MAC", True)
    monkeypatch.setattr(browsers, "_WINDOWS", False)


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(browsers, "_POSIX", False)
    monkeypatch.setattr(browsers, "_LINUX", False)
    monkeypatch.setattr(browsers, "_MAC", False)
    monkeypatch.setattr(browsers, "_WINDOWS", True)


def test_finds_chrome_on_the_path(tmp_path, monkeypatch, linux):
    binary = executable(tmp_path / "bin" / "google-chrome")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))

    assert browsers.find_chrome() == [binary]


def test_a_path_entry_that_is_not_executable_is_not_a_browser(tmp_path, monkeypatch, linux):
    # A same-named directory, or a file without the bit, would otherwise be launched
    # and fail as "the browser exited immediately".
    plain = tmp_path / "bin" / "google-chrome"
    plain.parent.mkdir(parents=True)
    plain.write_text("", encoding="utf-8")
    plain.chmod(plain.stat().st_mode & ~stat.S_IXUSR & ~stat.S_IXGRP & ~stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))

    assert browsers.find_chrome() == []


def test_finds_a_macos_application_bundle(tmp_path, monkeypatch, mac):
    # The regression this module was written for: `shutil.which` answers None on a Mac
    # with Chrome installed, because the binary is inside a bundle and not on PATH.
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    bundle = tmp_path / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    binary = executable(bundle)

    assert browsers._executables((), (), (str(bundle),), (), ()) == [binary]


def tables_of(monkeypatch, finder):
    """The five path tables a finder passes down, captured before any disk access."""
    seen = {}

    def capture(posix_names, linux_paths, mac_paths, windows_dirs, windows_exes):
        seen.update(
            posix=list(posix_names),
            linux=list(linux_paths),
            mac=list(mac_paths),
            windows_dirs=list(windows_dirs),
            windows_exes=list(windows_exes),
        )
        return []

    monkeypatch.setattr(browsers, "_candidates", capture)
    finder()
    return seen


def test_the_finders_know_where_each_platform_keeps_a_browser(monkeypatch):
    # Guards the knowledge; the tests around it guard the mechanism that uses it. A
    # finder that lost its macOS or Windows table would still pass every PATH test.
    chrome = tables_of(monkeypatch, browsers.find_chrome)
    assert any("Google Chrome.app" in path for path in chrome["mac"])
    assert any("Google/Chrome" in path for path in chrome["windows_dirs"])

    firefox = tables_of(monkeypatch, browsers.find_firefox)
    assert any("Firefox.app" in path for path in firefox["mac"])
    assert "Mozilla Firefox" in firefox["windows_dirs"]


def test_macos_paths_are_only_consulted_on_macos(tmp_path, monkeypatch, linux):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    bundle = tmp_path / "Applications/Firefox.app/Contents/MacOS/firefox"
    executable(bundle)

    assert browsers._executables((), (), (str(bundle),), (), ()) == []


def test_finds_a_windows_install_under_a_program_directory(tmp_path, monkeypatch, windows):
    for name in browsers._WINDOWS_ROOTS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    binary = executable(tmp_path / "Mozilla Firefox" / "firefox.exe")

    assert browsers.find_firefox() == [binary]


def test_the_same_binary_reached_twice_is_reported_once(tmp_path, monkeypatch, linux):
    # Two PATH entries pointing at one directory, which `PATH=$PATH:$PATH` produces.
    binary = executable(tmp_path / "bin" / "firefox")
    monkeypatch.setenv("PATH", os.pathsep.join([str(tmp_path / "bin")] * 2))

    assert browsers.find_firefox() == [binary]


def test_chromium_covers_every_brand(tmp_path, monkeypatch, linux):
    # Brave and Edge drive over CDP exactly as Chrome does, so refusing them means no
    # solver on a machine that has one.
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    brave = executable(tmp_path / "bin" / "brave-browser")
    edge = executable(tmp_path / "bin" / "microsoft-edge")

    assert sorted(browsers.find_chromium()) == sorted([brave, edge])


def test_firefox_is_not_offered_as_a_chromium(tmp_path, monkeypatch, linux):
    # It speaks BiDi, not CDP. Handing it to CdpSolver would hang until the deadline.
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    executable(tmp_path / "bin" / "firefox")

    assert browsers.find_chromium() == []


def test_picks_the_shortest_path(tmp_path, monkeypatch, linux):
    # Prefers /usr/bin/chromium over a flatpak wrapper or a beta channel beside it.
    short = executable(tmp_path / "b" / "chromium")
    executable(tmp_path / "long" / "nested" / "path" / "chromium")
    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(tmp_path / "long" / "nested" / "path"), str(tmp_path / "b")])
    )

    assert browsers.pick_chromium() == short


def test_picking_nothing_is_none_rather_than_an_error(tmp_path, monkeypatch, linux):
    # The caller decides what a missing browser means; for lncrawl it is "no solver",
    # which is a working configuration rather than a failure.
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    assert browsers.pick_chromium() is None
    assert browsers.pick_firefox() is None


def test_snap_firefox_is_not_offered(monkeypatch):
    # Confined to $HOME and unable to reach a dotfile profile directory, so it would be
    # found and then fail at launch — worse than not being found. Asserted against the
    # table rather than a lookup, or an empty PATH would pass it for the wrong reason.
    firefox = tables_of(monkeypatch, browsers.find_firefox)
    assert not any("snap" in path for path in firefox["linux"])
