"""Offline hook checks using disposable Git repositories and synthetic secrets."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCANNER = shutil.which("gitleaks") or str(Path.home() / ".local/bin/gitleaks")


class GitHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="git-hooks-", dir="/tmp/opencode")
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.home = self.repo / "home"
        self.home.mkdir()
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "PATH": "/usr/bin:/bin",
        }
        for key in list(self.env):
            if key.startswith(("GITLEAKS_", "GIT_CONFIG_", "GIT_DIR", "GIT_WORK_TREE",
                               "GIT_INDEX_FILE")):
                del self.env[key]
        self.env["GIT_CONFIG_NOSYSTEM"] = "1"
        self.run_cmd("git", "init", "-q")
        self.run_cmd("git", "config", "user.name", "Hook Test")
        self.run_cmd("git", "config", "user.email", "hook@example.invalid")
        for relative in ["hooks/pre-commit", "scripts/install-git-hooks.sh"]:
            target = self.repo / relative
            target.parent.mkdir(exist_ok=True)
            shutil.copy2(ROOT / relative, target)

    def run_cmd(self, *args, check=True):
        result = subprocess.run(args, cwd=self.repo, env=self.env,
                                text=True, capture_output=True)
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def install_scanner(self):
        if not Path(SCANNER).is_file():
            self.skipTest("Gitleaks is required for integration checks")
        local_bin = self.home / ".local/bin"
        local_bin.mkdir(parents=True)
        (local_bin / "gitleaks").symlink_to(SCANNER)
        self.run_cmd("bash", "scripts/install-git-hooks.sh")

    def test_missing_scanner_blocks_hook_and_setup(self):
        for script in ["hooks/pre-commit", "scripts/install-git-hooks.sh"]:
            result = self.run_cmd("bash", script, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("gitleaks", result.stderr.lower())

    def test_existing_hooks_path_is_preserved(self):
        self.install_scanner()
        self.run_cmd("git", "config", "core.hooksPath", "other-hooks")
        result = self.run_cmd("bash", "scripts/install-git-hooks.sh", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.run_cmd("git", "config", "--get", "core.hooksPath")
                         .stdout.strip(), "other-hooks")

    def test_safe_commit_passes_and_unstaged_secret_is_not_scanned(self):
        self.install_scanner()
        fixture = self.repo / "fixture.txt"
        fixture.write_text("safe staged content\n")
        self.run_cmd("git", "add", "fixture.txt")
        fixture.write_text('aws_access_key_id = "' + "AKIA" + "ABCDEFGHIJKLMNOP" + '"\n')
        self.run_cmd("git", "commit", "-qm", "safe index")

    def test_staged_synthetic_secret_blocks_commit_and_is_redacted(self):
        self.install_scanner()
        # Deliberately fake AWS-shaped token, never a credential.
        fake = "AKIA" + "ABCDEFGHIJKLMNOP"
        (self.repo / "fixture.txt").write_text(f'aws_access_key_id = "{fake}"\n')
        self.run_cmd("git", "add", "fixture.txt")
        result = self.run_cmd("git", "commit", "-qm", "must be blocked", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(fake, result.stdout + result.stderr)
        self.assertNotEqual(self.run_cmd("git", "rev-parse", "--verify", "HEAD",
                                      check=False).returncode, 0)


if __name__ == "__main__":
    unittest.main()
