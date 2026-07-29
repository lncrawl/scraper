"""Configuration: defaults that work, and derived paths."""

from __future__ import annotations

from pathlib import Path

import pytest
import requests

from scraper import ScraperConfig, default_data_dir
from scraper.browser import CallableSolver, SolveResult


def test_the_defaults_are_a_working_configuration():
    config = ScraperConfig()
    # Impersonation is the baseline rather than an opt-in, because an ordinary client
    # fails the whole transport group in the first round trip.
    assert config.impersonate == "chrome"
    # Remembering is on, because the layer it exists for cannot be satisfied by a
    # process that forgets everything on exit.
    assert config.remember
    assert config.guard_topic


def test_only_configured_capabilities_are_offered():
    assert ScraperConfig().capabilities_enabled() == (False, False, False)
    solver = CallableSolver(lambda *a, **k: SolveResult(cookies={}, user_agent="x"))
    enabled = ScraperConfig(archive=True, browser=solver, managed=_stub_provider)
    assert enabled.capabilities_enabled() == (True, True, True)


def _stub_provider(method: str, url: str, **options: object) -> requests.Response:
    return requests.Response()


class TestDerivedPaths:
    def test_state_lives_under_the_data_directory(self, tmp_path: Path):
        config = ScraperConfig(data_dir=tmp_path)
        assert config.state_dir == tmp_path
        assert config.memory_path == tmp_path / "origins.json"

    def test_nothing_is_derived_when_not_remembering(self, tmp_path: Path):
        config = ScraperConfig(data_dir=tmp_path, remember=False)
        assert config.state_dir is None
        assert config.memory_path is None
        assert config.profile_root is None

    def test_profiles_are_only_rooted_when_there_is_a_solver(self, tmp_path: Path):
        # Creating profile directories for a solver that does not exist leaves litter
        # and explains nothing.
        assert ScraperConfig(data_dir=tmp_path).profile_root is None
        solver = CallableSolver(lambda *a, **k: SolveResult(cookies={}, user_agent="x"))
        with_solver = ScraperConfig(data_dir=tmp_path, browser=solver)
        assert with_solver.profile_root == tmp_path / "profiles"

    def test_a_user_path_is_expanded(self):
        assert not str(ScraperConfig(data_dir=Path("~/x")).data_dir).startswith("~")

    def test_the_default_location_is_overridable_by_environment(self, monkeypatch, tmp_path: Path):
        # So a deployment can put learned state on a volume.
        monkeypatch.setenv("SCRAPER_DATA_DIR", str(tmp_path / "elsewhere"))
        assert default_data_dir() == tmp_path / "elsewhere"

    def test_the_default_location_is_a_real_place(self, monkeypatch):
        monkeypatch.delenv("SCRAPER_DATA_DIR", raising=False)
        found = default_data_dir()
        assert found.is_absolute()
        assert found.name == "lncrawl-scraper"


def test_a_nonsense_decoy_policy_is_rejected_at_construction():
    with pytest.raises(ValueError, match="on_decoy"):
        ScraperConfig(on_decoy="explode")
