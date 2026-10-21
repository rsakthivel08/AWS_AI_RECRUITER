"""Project root + src on sys.path so tests import src.shared.* and src.functions.*."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT), str(ROOT / "src")):
    if path not in sys.path:
        sys.path.insert(0, path)

# Optional pytest-mock / moto free markers


def pytest_configure(config):  # noqa: ANN001
    config.addinivalue_line("markers", "awsunsafe: touches the real AWS SDK")
