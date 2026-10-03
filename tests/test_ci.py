from __future__ import annotations

import hashlib
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "pipeline"))
import ci
from fixture import fixture_files
from publisher.snapshot import _tree, validate
from publisher.tooling import _binary, install_hugo, local_hugo, runtime_check


class CITests(unittest.TestCase):
    def test_deterministic_synthetic_fixture_validates(self):
        files = fixture_files()
        self.assertEqual(files, fixture_files())
        manifest, pages = validate(files)
        self.assertEqual(len(pages), 2)
        self.assertEqual(len(files), 4)
        self.assertFalse(manifest["deployable"])
        self.assertEqual(_tree(PROJECT / "publication/fixture"), files)

    def test_public_repository_allowlist(self):
        for name in (".gitignore", ".gitattributes", "README.md", "pipeline/publish.py", "pipeline/publisher/tooling.py",
                     "tests/test_ci.py", "docs/hugo-ci.md", ".github/workflows/hugo-ci.yml", "publication/fixture/manifest.json"):
            with self.subTest(name=name):
                self.assertTrue(ci.allowed_repository_path(name))
        for name in (".local/state/publisher/identity-registry.json", ".local/reports/publisher/check.json",
                     ".generated/snapshots/manifest.json", "content/private.md", "public/index.html", "quartz/cli.js",
                     ".github/workflows/deploy.yml", "publication/snapshot/manifest.json", "publication/fixture/private.txt",
                     "pipeline/README.md", "pipeline/.private/note.json", "pipeline/__pycache__/foo.py",
                     "docs/hugo-migration-architecture.md", "README-SITE.md"):
            with self.subTest(name=name):
                self.assertFalse(ci.allowed_repository_path(name))

    def test_runtime_and_missing_dependency_or_version_fail(self):
        self.assertTrue(local_hugo().name in {"hugo.exe", "hugo"})
        self.assertEqual(runtime_check()["hugo"], "0.167.0")
        with patch("publisher.tooling.platform.python_version", return_value="0.0.0"), self.assertRaisesRegex(ValueError, "version differs"):
            runtime_check()
        with patch("publisher.tooling.version", return_value="0.0.0"), self.assertRaisesRegex(ValueError, "dependency differs"):
            runtime_check()

    def test_binary_archive_reader_ignores_other_paths_and_rejects_tar_link(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("hugo.exe", b"binary")
            archive.writestr("../../outside.txt", b"not extracted")
        self.assertEqual(_binary(stream.getvalue(), True), b"binary")
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            entry = tarfile.TarInfo("hugo")
            entry.type = tarfile.SYMTYPE
            entry.linkname = "../../outside"
            archive.addfile(entry)
        with self.assertRaisesRegex(ValueError, "regular"):
            _binary(stream.getvalue(), False)

    def test_corrupt_cached_archive_and_existing_binary_refused_no_network(self):
        with tempfile.TemporaryDirectory(prefix="tooling-test-") as directory:
            project = Path(directory)
            (project / "pipeline").mkdir()
            (project / "pipeline/toolchain.toml").write_bytes((PROJECT / "pipeline/toolchain.toml").read_bytes())
            folder = project / ".local/tools/hugo/0.167.0"
            folder.mkdir(parents=True)
            archive = folder / ("hugo.zip" if sys.platform == "win32" else "hugo_0.167.0_linux-amd64.tar.gz")
            archive.write_bytes(b"corrupted")
            with patch("publisher.tooling.urlopen", side_effect=AssertionError("Unexpected network")), self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                install_hugo(project)

    def test_repository_gate_refuses_forced_private_file(self):
        fake = type("Result", (), {"stdout": b".local/state/publisher/identity-registry.json\0"})()
        with patch("ci.subprocess.run", return_value=fake), self.assertRaisesRegex(ValueError, "exceed migration allowlist"):
            ci.repository_check()

    def test_workflow_policy_is_readonly_and_matches_lock(self):
        # The real CI invocation validates the actual Git index as well.
        fake = type("Result", (), {"stdout": b".github/workflows/hugo-ci.yml\0publication/fixture/manifest.json\0"})()
        with patch("ci.subprocess.run", return_value=fake):
            self.assertEqual(ci.repository_check(), 2)

    def test_ci_does_not_accept_required_hugo_integration_skip(self):
        class Result:
            skipped = [("integration", "Checksum-verified pinned Hugo required")]
            testsRun = 1
            def wasSuccessful(self):
                return True
        with patch("ci.unittest.TextTestRunner.run", return_value=Result()), self.assertRaisesRegex(ValueError, "required integration"):
            ci.run_tests()


if __name__ == "__main__":
    unittest.main()
