from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "pipeline"))
import prepush


class PrepushTests(unittest.TestCase):
    def fixture(self):
        root = "repos/example/example.github.io"
        data = {
            root: (200, {"full_name": "example/example.github.io", "default_branch": "main",
                         "has_pages": True, "permissions": {"admin": True}}),
            root + "/pages": (200, {"build_type": "workflow", "status": "built"}),
            root + "/actions/workflows?per_page=100": (200, {"total_count": 2, "workflows": [
                {"path": prepush.WORKFLOW, "state": "active"},
                {"path": prepush.LEGACY_WORKFLOW, "state": "active"}]}),
            root + "/contents/" + prepush.WORKFLOW + "?ref=main": (200, {
                "path": prepush.WORKFLOW, "encoding": "base64",
                "content": base64.b64encode((PROJECT / prepush.WORKFLOW).read_bytes().replace(b"\r\n", b"\n")).decode()}),
        }
        for state in prepush.ACTIVE_STATUSES:
            data[root + f"/actions/runs?status={state}&per_page=100"] = (200, {"total_count": 0, "workflow_runs": []})
        return root, data

    def check(self, data):
        return prepush.remote_policy("example/example.github.io", data.__getitem__)

    def test_workflow_mode_is_not_claimed_to_take_existing_site_offline(self):
        _, data = self.fixture()
        result = self.check(data)
        self.assertEqual(result["pages_mode"], "workflow")
        self.assertEqual(result["existing_site_offline"], "not_established_by_this_check")

    def test_legacy_or_unknown_pages_blocks(self):
        root, baseline = self.fixture()
        for mode in ("legacy", None, "unexpected"):
            data = copy.deepcopy(baseline)
            data[root + "/pages"] = (200, {"build_type": mode})
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "branch publishing"):
                self.check(data)

    def test_pages_404_requires_admin_and_consistent_absence(self):
        root, data = self.fixture()
        data[root + "/pages"] = (404, {"message": "Not Found"})
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            self.check(data)
        data[root][1]["has_pages"] = False
        self.assertEqual(self.check(data)["pages_mode"], "absent")
        data[root][1]["permissions"]["admin"] = False
        with self.assertRaisesRegex(ValueError, "visibility"):
            self.check(data)

    def test_other_remote_workflow_or_modified_workflow_blocks(self):
        root, data = self.fixture()
        data[root + "/actions/workflows?per_page=100"][1]["workflows"][0]["path"] = ".github/workflows/deploy.yml"
        with self.assertRaisesRegex(ValueError, "Unreviewed"):
            self.check(data)
        root, data = self.fixture()
        data[root + "/contents/" + prepush.WORKFLOW + "?ref=main"][1]["content"] = base64.b64encode(b"deploy").decode()
        with self.assertRaisesRegex(ValueError, "differs"):
            self.check(data)

    def test_all_active_deployment_states_block_even_in_workflow_mode(self):
        root, baseline = self.fixture()
        for state in prepush.ACTIVE_STATUSES:
            data = copy.deepcopy(baseline)
            data[root + f"/actions/runs?status={state}&per_page=100"] = (200, {
                "total_count": 1, "workflow_runs": [{"path": prepush.LEGACY_WORKFLOW, "status": state}]})
            with self.subTest(state=state), self.assertRaisesRegex(ValueError, "could deploy"):
                self.check(data)

    def test_running_reviewed_build_is_permitted_and_partial_lists_fail(self):
        root, data = self.fixture()
        data[root + "/actions/runs?status=queued&per_page=100"] = (200, {
            "total_count": 1, "workflow_runs": [{"path": prepush.WORKFLOW, "status": "queued"}]})
        self.check(data)
        data[root + "/actions/workflows?per_page=100"][1]["total_count"] = 3
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.check(data)

    def test_http_errors_are_not_treated_as_pages_absence(self):
        for status in (401, 403, 422, 429, 500):
            with self.subTest(status=status), self.assertRaises(ValueError):
                prepush.parse_response(f"HTTP/2.0 {status} Error\r\nX-Test: a\r\n\r\n{{}}", 1)
        self.assertEqual(prepush.parse_response('HTTP/2.0 404 Not Found\nX-Test: a\n\n{"message":"Not Found"}', 1)[0], 404)
        with self.assertRaises(ValueError):
            prepush.parse_response('HTTP/2.0 200 OK\n\n{}', 1)
        with self.assertRaises(ValueError):
            prepush.parse_response('{}', 0)

    def test_api_uses_only_get_and_never_prints_diagnostic_output(self):
        result = type("Result", (), {"stdout": "HTTP/2.0 200 OK\n\n{}", "stderr": "private diagnostic", "returncode": 0})()
        with patch("prepush.subprocess.run", return_value=result) as call:
            self.assertEqual(prepush.github_get("gh", "repos/example/example.github.io"), (200, {}))
        args = call.call_args.args[0]
        self.assertEqual(args[args.index("--method") + 1], "GET")
        self.assertEqual(args[args.index("--hostname") + 1], "github.com")

    def test_local_gate_checks_committed_head_and_exact_origin(self):
        workflow = (PROJECT / prepush.WORKFLOW).read_bytes().replace(b"\r\n", b"\n")
        calls = []
        def git(project, *args):
            calls.append(args)
            return {"symbolic-ref": b"main\n", "remote": b"https://github.com/example/example.github.io.git\n",
                    "rev-parse": b"a" * 40 + b"\n",
                    "ls-tree": (prepush.WORKFLOW + "\0publication/fixture/manifest.json\0").encode(),
                    "show": workflow}[args[0]]
        with patch("prepush.git", side_effect=git):
            self.assertEqual(prepush.local_revision(PROJECT, "example/example.github.io"), "a" * 40)
        self.assertTrue(all(args[0] in {"symbolic-ref", "remote", "rev-parse", "ls-tree", "show"} for args in calls))
        with patch("prepush.git", side_effect=[b"main\n", b"https://github.com/other/repo.git\n"]), self.assertRaisesRegex(ValueError, "destination"):
            prepush.local_revision(PROJECT, "example/example.github.io")
        with self.assertRaisesRegex(ValueError, "owner/name"):
            prepush.local_revision(PROJECT, "../escape")


if __name__ == "__main__":
    unittest.main()
