from __future__ import annotations

import json
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from check_install import installation_issues  # noqa: E402
from install_launchd import LABEL  # noqa: E402


class CheckInstallTests(unittest.TestCase):
    def make_home(self, root: Path) -> Path:
        config = root / ".config" / "token-activity"
        config.mkdir(parents=True)
        (config / "node_name").write_text("work\n")
        (config / "multica.json").write_text(json.dumps({
            "profile": "profile-one",
            "workspaceId": "workspace-one",
            "dshProfile": "profile-two",
        }))
        (config / "multica_runtime_roles.json").write_text(json.dumps({
            "runtime-one": "work",
        }))
        writer = root / ".cache" / "aether-ledger" / "writer" / "scripts"
        writer.mkdir(parents=True)
        (writer / "sync_usage.py").write_text("# fixture\n")
        plist = {
            "Label": LABEL,
            "ProgramArguments": [str(writer / "sync_usage.py")],
            "EnvironmentVariables": {
                "HOME": str(root),
                "MULTICA_PROFILE": "profile-one",
                "MULTICA_WORKSPACE_ID": "workspace-one",
                "MULTICA_DSH_PROFILE": "profile-two",
            },
        }
        destination = root / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        destination.parent.mkdir(parents=True)
        with destination.open("wb") as stream:
            plistlib.dump(plist, stream)
        return root

    @patch("check_install.shutil.which", return_value="/bin/example")
    def test_accepts_a_reproducible_install(self, _which) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(installation_issues(self.make_home(Path(directory))), [])

    @patch("check_install.shutil.which", return_value="/bin/example")
    def test_reports_launchd_drift_without_printing_private_values(self, _which) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = self.make_home(Path(directory))
            plist_path = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
            with plist_path.open("rb") as stream:
                plist = plistlib.load(stream)
            plist["EnvironmentVariables"]["MULTICA_PROFILE"] = "wrong-private-value"
            with plist_path.open("wb") as stream:
                plistlib.dump(plist, stream)

            issues = installation_issues(home)
            self.assertIn(
                "launchd environment differs from multica.json: MULTICA_PROFILE",
                issues,
            )
            self.assertNotIn("wrong-private-value", "\n".join(issues))

    @patch("check_install.shutil.which", return_value="/bin/example")
    def test_reports_missing_roles_and_dsh_binding_drift(self, _which) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = self.make_home(Path(directory))
            roles = home / ".config" / "token-activity" / "multica_runtime_roles.json"
            roles.unlink()
            binding = home / ".config" / "token-activity" / "multica_dsh_profile"
            binding.write_text("another-profile\n")

            issues = installation_issues(home)
            self.assertIn("multica_runtime_roles.json is missing", issues)
            self.assertIn(
                "Multica DSH source binding differs from multica.json", issues
            )


if __name__ == "__main__":
    unittest.main()
