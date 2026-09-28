import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import github_state as gs
from monitor import atomic_json


class StateTests(unittest.TestCase):
    def setUp(self):
        self.state = {"url": gs.DEFAULT_URL, "sent": [], "candidate": None, "count": 0}

    def test_public_state_rejects_secrets_and_arbitrary_content(self):
        cases = [dict(api_key="private"), dict(phone="private"), dict(count="private"),
                 dict(sent=["private"]), dict(url="https://example.com/?key=private"),
                 dict(last_status="private"), dict(notification_error="private"), dict(count=float("nan"))]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                gs.safe_state({**self.state, **changes})

    def test_git_round_trip_conflict_and_missing_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            previous = Path.cwd()
            try:
                remote = root / "remote.git"
                local = root / "local"
                local.mkdir()
                gs.git("init", "--bare", str(remote))
                os.chdir(local)
                gs.git("init", "-b", "main")
                gs.git("remote", "add", "origin", str(remote))
                with self.assertRaises(subprocess.CalledProcessError):
                    gs.restore(root / "missing")
                self.assertFalse((root / "missing" / "state.json").exists())
                blob = gs.git("hash-object", "-w", "--stdin", input=json.dumps(self.state))
                tree = gs.git("mktree", input=f"100644 blob {blob}\tstate.json\n")
                commit = gs.git("-c", "user.name=Test", "-c", "user.email=test@example.com",
                                "commit-tree", tree, "-m", "Initial state")
                gs.git("push", "origin", f"{commit}:refs/heads/{gs.BRANCH}")
                one, two = root / "one", root / "two"
                gs.restore(one)
                gs.restore(two)
                updated = {**self.state, "sent": ["available"], "last_check": 1000}
                atomic_json(one / "state.json", updated)
                gs.save(one)
                # Una ejecución que leyó el estado anterior no puede borrar el aviso nuevo.
                atomic_json(two / "state.json", {**self.state, "last_check": 1100})
                with self.assertRaises(subprocess.CalledProcessError):
                    gs.save(two)
                gs.restore(two)
                self.assertEqual(json.loads((two / "state.json").read_text()), updated)
                head = gs.git("rev-parse", gs.REF)
                gs.save(two)
                gs.restore(two)
                self.assertEqual(gs.git("rev-parse", gs.REF), head)
            finally:
                os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
