"""Repository line-budget checker used by `make lint-size` and tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

CODE_SUFFIXES = {".py", ".html", ".yaml"}
DOC_SUFFIXES = {".md"}
EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "var", "__pycache__", ".pytest_cache",
    "node_modules", ".mypy_cache", "build", "dist", ".idea", ".vscode",
}
MAX_LINES_PER_FILE = 300
MIN_TOTAL_LINES = 2500
MAX_TOTAL_LINES = 6500


@dataclass(frozen=True)
class SizeReport:
    files: dict[str, int]
    total: int
    oversized: dict[str, int]

    @property
    def docs_files(self) -> dict[str, int]:
        return {name: lines for name, lines in self.files.items() if name.endswith(".md")}

    @property
    def ok(self) -> bool:
        return not self.oversized and MIN_TOTAL_LINES <= self.total <= MAX_TOTAL_LINES


def source_files(root: str | Path) -> list[Path]:
    base = Path(root)
    files: list[Path] = []
    for path in base.rglob("*"):
        if not path.is_file() or path.suffix not in CODE_SUFFIXES | DOC_SUFFIXES:
            continue
        relative = path.relative_to(base)
        if any(part in EXCLUDED_DIRS for part in relative.parts):
            continue
        files.append(path)
    return sorted(files, key=lambda item: item.relative_to(base).as_posix())


def count_lines(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def check(root: str | Path) -> SizeReport:
    base = Path(root)
    counts = {
        path.relative_to(base).as_posix(): count_lines(path)
        for path in source_files(base)
    }
    total = sum(lines for name, lines in counts.items() if Path(name).suffix in CODE_SUFFIXES)
    oversized = {name: lines for name, lines in counts.items() if lines > MAX_LINES_PER_FILE}
    return SizeReport(files=counts, total=total, oversized=oversized)
