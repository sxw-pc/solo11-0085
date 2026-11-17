"""Built-in synthetic recruitment site."""
from .app import create_app
from .data import FIXTURES_DIR, load_scenario, scenario_names
__all__ = ["create_app", "FIXTURES_DIR", "load_scenario", "scenario_names"]
