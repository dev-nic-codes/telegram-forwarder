from __future__ import annotations

import os
import py_compile
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "source"
TESTS = ROOT / "tests"


def compile_sources() -> None:
    paths = [ROOT / "run.py", ROOT / "run_service.py"]
    paths.extend(sorted(SOURCE.rglob("*.py")))
    paths.extend(sorted(TESTS.glob("*.py")))
    for path in paths:
        py_compile.compile(str(path), doraise=True)
    print(f"Compiled {len(paths)} Python files.")


def run_tests() -> bool:
    with tempfile.TemporaryDirectory(prefix="forwarder-tests-") as runtime_dir:
        os.environ["APPDATA"] = runtime_dir
        sys.path.insert(0, str(ROOT))
        sys.path.insert(0, str(SOURCE))
        suite = unittest.defaultTestLoader.discover(str(TESTS), pattern="test_*.py")
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return result.wasSuccessful()


def main() -> int:
    compile_sources()
    if not run_tests():
        return 1
    print("Forwarder validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
