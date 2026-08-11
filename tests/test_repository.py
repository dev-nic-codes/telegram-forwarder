from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOKEN_PATTERN = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b")
REQUIRED_PATHS = (
    "run.py",
    "run_service.py",
    "requirements.txt",
    "start.ps1",
    "start.sh",
    "deploy/telegram-forwarder.service",
    "deploy/telegram-forwarder.env.example",
    "source/app/main.py",
    "source/app/service_runtime.py",
)


class RepositoryIntegrityTests(unittest.TestCase):
    def test_required_runtime_files_exist(self) -> None:
        missing = [path for path in REQUIRED_PATHS if not (ROOT / path).is_file()]
        self.assertEqual(missing, [])

    def test_source_and_examples_contain_no_bot_tokens(self) -> None:
        paths = [ROOT / "run.py", ROOT / "run_service.py"]
        paths.extend((ROOT / "source").rglob("*.py"))
        paths.extend((ROOT / "tests").glob("*.py"))
        paths.append(ROOT / "deploy" / "telegram-forwarder.env.example")
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertFalse(TOKEN_PATTERN.search(path.read_text(encoding="utf-8")))

    def test_sql_parameter_markers_are_not_ellipsis(self) -> None:
        for relative in ("source/app/core/accounts.py", "source/app/database/schema.py"):
            with self.subTest(path=relative):
                source = (ROOT / relative).read_text(encoding="utf-8")
                self.assertNotIn("...", source)


if __name__ == "__main__":
    unittest.main()
