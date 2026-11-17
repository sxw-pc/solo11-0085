"""Shared strict YAML primitives used by configuration loaders."""
from __future__ import annotations
from pathlib import Path
from typing import Any
import yaml
from .errors import ConfigError
def read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file(): raise ConfigError(f"configuration file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read YAML file {path}: {exc}") from exc
    if not isinstance(data, dict): raise ConfigError(f"YAML root must be a mapping: {path}")
    return data
def strict_map(value: Any, allowed: set[str], context: str) -> dict[str, Any]:
    if not isinstance(value, dict): raise ConfigError(f"{context} must be a mapping")
    unknown = set(value) - allowed
    missing = allowed - set(value)
    if unknown: raise ConfigError(f"{context} has unknown keys: {sorted(unknown)}")
    if missing: raise ConfigError(f"{context} is missing keys: {sorted(missing)}")
    return value
def text(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{context} must be a non-empty string")
    return value.strip()
def integer(value: Any, context: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{context} must be an integer")
    if not minimum <= value <= maximum:
        raise ConfigError(f"{context} must be between {minimum} and {maximum}")
    return value
def number(value: Any, context: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{context} must be a number")
    if not minimum <= float(value) <= maximum:
        raise ConfigError(f"{context} must be between {minimum} and {maximum}")
    return float(value)
def text_list(value: Any, context: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{context} must be a non-empty list")
    return tuple(text(item, f"{context}[]") for item in value)
def port_list(value: Any, context: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{context} must be a non-empty list")
    return tuple(integer(item, f"{context}[]", 1, 65535) for item in value)
