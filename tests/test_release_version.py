from __future__ import annotations

import json
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path

from soccer_predict import __version__

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_RELEASE_VERSION = "3.6.2"


class ReleaseVersionContractTests(unittest.TestCase):
    def test_release_version_is_consistent_across_package_and_skill_metadata(self):
        project = tomllib.loads(
            (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        marketplace = json.loads(
            (REPO_ROOT / "clawhub.json").read_text(encoding="utf-8")
        )

        self.assertEqual(project["version"], EXPECTED_RELEASE_VERSION)
        self.assertEqual(marketplace["version"], EXPECTED_RELEASE_VERSION)
        self.assertEqual(__version__, EXPECTED_RELEASE_VERSION)

    def test_release_documents_name_the_current_version(self):
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        skill = (REPO_ROOT / "SKILL.md").read_text(encoding="utf-8")
        changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

        self.assertIn(
            f"当前发布版本：**{EXPECTED_RELEASE_VERSION}**",
            readme,
        )
        self.assertIn(
            f"Current package/Skill release: **{EXPECTED_RELEASE_VERSION}**",
            skill,
        )
        release_heading = f"## [{EXPECTED_RELEASE_VERSION}] - 2026-08-08"
        self.assertIn(release_heading, changelog)
        self.assertLess(changelog.index(release_heading), changelog.index("## [3.3.0]"))

    def test_module_entry_point_reports_release_version(self):
        completed = subprocess.run(
            [sys.executable, "-m", "soccer_predict", "--version"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.stdout.strip(), EXPECTED_RELEASE_VERSION)


if __name__ == "__main__":
    unittest.main()
