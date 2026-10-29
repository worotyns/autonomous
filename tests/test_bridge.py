"""Unit tests for bridge.py with the gh / herdr-projects CLIs mocked out.

Run with `make check`, or directly: python3 -m unittest discover -s tests -t . -p 'test_*.py'
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # also allows `python3 tests/test_bridge.py`

import bridge  # noqa: E402


def make_cfg(**bridge_cfg) -> dict:
    cfg = {
        "bridge": {"poll_interval": 60, "poll_interval_max": 300, "poll_backoff_factor": 1.5, "quiet_window": 90},
        "github": {"start_label": "start", "allow_authors": []},
        "consult": {"label": "consult", "agent": ""},
        "repo": [],
    }
    cfg["bridge"].update(bridge_cfg)
    return cfg


class ConfigTest(unittest.TestCase):
    def _load(self, text: str) -> dict:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "autonomous.toml"
            path.write_text(text)
            with mock.patch.object(bridge, "CONFIG_PATH", path):
                return bridge.load_config()

    def test_backoff_defaults(self):
        cfg = self._load('[bridge]\npoll_interval = 30\n[[repo]]\nname = "a/b"\n')
        self.assertEqual(cfg["bridge"]["poll_interval"], 30)
        self.assertEqual(cfg["bridge"]["poll_interval_max"], 300)
        self.assertEqual(cfg["bridge"]["poll_backoff_factor"], 1.5)
        self.assertEqual(cfg["bridge"]["auto_recover_max"], 2)

    def test_backoff_is_configurable(self):
        cfg = self._load("[bridge]\npoll_interval = 10\npoll_interval_max = 45\npoll_backoff_factor = 2.0\n")
        self.assertEqual(cfg["bridge"]["poll_interval_max"], 45)
        self.assertEqual(cfg["bridge"]["poll_backoff_factor"], 2.0)


class OnceActivityTest(unittest.TestCase):
    def _once(self, picked: bool, delivered: bool, synced: bool) -> bool:
        with mock.patch.object(bridge, "db", return_value=object()), \
             mock.patch.object(bridge, "pick_up_issues", return_value=picked), \
             mock.patch.object(bridge, "deliver_comments", return_value=delivered), \
             mock.patch.object(bridge, "sync_states", return_value=synced):
            return bridge.cmd_once(make_cfg())

    def test_quiet_poll_reports_no_work(self):
        self.assertFalse(self._once(False, False, False))

    def test_any_job_with_work_reports_true(self):
        for flags in ((True, False, False), (False, True, False), (False, False, True)):
            with self.subTest(flags=flags):
                self.assertTrue(self._once(*flags))


class RunBackoffTest(unittest.TestCase):
    def _sleeps(self, results: list[bool]) -> list[float]:
        sleeps: list[float] = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= len(results):
                raise StopIteration

        with mock.patch.object(bridge, "cmd_once", side_effect=results), \
             mock.patch.object(bridge, "gh_user", return_value="me"), \
             mock.patch.object(bridge.time, "sleep", side_effect=fake_sleep):
            with self.assertRaises(StopIteration):
                bridge.cmd_run(make_cfg())
        return sleeps

    def test_quiet_polls_grow_the_wait_up_to_the_cap(self):
        self.assertEqual(self._sleeps([False] * 6), [90, 135, 202.5, 300, 300, 300])

    def test_activity_returns_to_the_base_interval(self):
        self.assertEqual(self._sleeps([False, False, True, False, False]), [90, 135, 60, 90, 135])


class JobActivityTest(unittest.TestCase):
    """The three polling jobs report activity through mocked gh / herdr-projects CLIs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_state = bridge.STATE_DIR
        bridge.STATE_DIR = Path(self.tmp.name)
        self.c = bridge.db()

    def tearDown(self):
        self.c.close()
        bridge.STATE_DIR = self.old_state
        self.tmp.cleanup()

    def _add_task(self, *, kind="work", cursor="2020-01-01T00:00:00Z",
                  board_state="working/", label="agent:working", state="working"):
        self.c.execute(
            "insert into tasks(repo, issue, task_id, state, label, cursor, created_at, updated_at, board_state, kind)"
            " values(?,?,?,?,?,?,?,?,?,?)",
            ("a/b", 1, "T-1", state, label, cursor, "2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z", board_state, kind))

    def test_new_start_label_starts_a_task(self):
        issue = {"number": 7, "title": "Add a widget", "html_url": "https://example.invalid/7",
                 "body": "please add it", "labels": [{"name": "start"}]}
        cfg = make_cfg()
        cfg["github"]["allow_authors"] = ["alice"]
        cfg["repo"] = [{"name": "a/b", "path": "/nowhere/a/b"}]

        def fake_api(path, *extra, paginate=False):
            return [issue] if "labels=start" in path else []

        with mock.patch.object(bridge, "gh_user", return_value="bot"), \
             mock.patch.object(bridge, "gh_api", side_effect=fake_api), \
             mock.patch.object(bridge, "labeled_by", return_value="alice"), \
             mock.patch.object(bridge, "start_task") as start_task:
            self.assertTrue(bridge.pick_up_issues(cfg, self.c))
        start_task.assert_called_once()

    def test_quiet_when_nothing_labels_the_issues(self):
        cfg = make_cfg()
        cfg["repo"] = [{"name": "a/b", "path": "/nowhere/a/b"}]
        with mock.patch.object(bridge, "gh_user", return_value="bot"), \
             mock.patch.object(bridge, "gh_api", return_value=[]), \
             mock.patch.object(bridge, "hp") as hp:
            self.assertFalse(bridge.pick_up_issues(cfg, self.c))
        hp.assert_not_called()

    def test_forwarded_comment_is_activity(self):
        self._add_task()
        comment = {"created_at": "2020-01-02T00:00:00Z", "user": {"login": "alice"}, "body": "please look at X"}
        cfg = make_cfg()
        cfg["github"]["allow_authors"] = ["alice"]
        with mock.patch.object(bridge, "gh_user", return_value="bot"), \
             mock.patch.object(bridge, "gh_api", return_value=[comment]), \
             mock.patch.object(bridge, "hp") as hp, \
             mock.patch.object(bridge, "run"):
            self.assertTrue(bridge.deliver_comments(cfg, self.c))
        self.assertTrue(any(call.args[1] == "tell" for call in hp.call_args_list))

    def test_no_new_comment_is_quiet(self):
        self._add_task()
        with mock.patch.object(bridge, "gh_user", return_value="bot"), \
             mock.patch.object(bridge, "gh_api", return_value=[]), \
             mock.patch.object(bridge, "hp") as hp, \
             mock.patch.object(bridge, "run"):
            self.assertFalse(bridge.deliver_comments(make_cfg(), self.c))
        hp.assert_not_called()

    def test_board_state_change_is_activity(self):
        self._add_task()
        board = [{"id": "T-1", "state": "blocked", "stage": "approval", "msg": "ready", "pr": 5}]
        with mock.patch.object(bridge, "hp", return_value=board), \
             mock.patch.object(bridge, "run") as run:
            self.assertTrue(bridge.sync_states(make_cfg(), self.c))
        row = self.c.execute("select label from tasks where task_id='T-1'").fetchone()
        self.assertEqual(row["label"], "agent:ready")
        self.assertTrue(any("--add-label" in call.args[0] for call in run.call_args_list))

    def test_unchanged_board_state_is_quiet(self):
        self._add_task(board_state="blocked/approval", label="agent:ready")
        board = [{"id": "T-1", "state": "blocked", "stage": "approval"}]
        with mock.patch.object(bridge, "hp", return_value=board), \
             mock.patch.object(bridge, "run") as run:
            self.assertFalse(bridge.sync_states(make_cfg(), self.c))
        run.assert_not_called()


class FailureHandlingTest(unittest.TestCase):
    """A failure's reason reaches the issue and the log, and the bridge re-drives it so the issue starts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_state = bridge.STATE_DIR
        bridge.STATE_DIR = Path(self.tmp.name)
        self.c = bridge.db()
        # The pre-failure state is already working/working, the worst case for the board_state gate below.
        self.c.execute(
            "insert into tasks(repo, issue, task_id, state, label, cursor, created_at, updated_at, board_state, kind)"
            " values(?,?,?,?,?,?,?,?,?,?)",
            ("a/b", 1, "T-1", "working", "agent:working", "2020-01-01T00:00:00Z",
             "2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z", "working/working", "work"))

    def tearDown(self):
        self.c.close()
        bridge.STATE_DIR = self.old_state
        self.tmp.cleanup()

    def _notify(self, failure):
        calls, runs, recover = [], [], []

        def fake_hp(cfg, *args, **kw):
            calls.append(args)
            if args[0] == "list":
                return [{"id": "T-1", "state": "error", "stage": "failed", "msg": "Setup failed"}]
            if args[0] == "details":
                return {"failure": failure}
            return {"raw": "sent to the agent"}

        def fake_recover(cfg, *args):
            recover.append(args)
            return True, {"raw": "sent to the agent"}

        def fake_run(args, check=True, input=None):
            runs.append(args)
            return ""

        with mock.patch.object(bridge, "hp", side_effect=fake_hp), \
             mock.patch.object(bridge, "hp_result", side_effect=fake_recover), \
             mock.patch.object(bridge, "run", side_effect=fake_run), \
             mock.patch.dict(bridge.os.environ, {"HERDR_PROJECTS_ID": "T-1", "HERDR_PROJECTS_EVENT": "failed"}):
            bridge.cmd_notify(make_cfg())
        return calls, runs, recover

    def test_failure_text_has_the_board_reason(self):
        text = bridge.failure_text({"msg": "Setup failed"},
                                   {"title": "Setup failed", "detail": "Command: uv venv venv\nerror: no venv found",
                                    "next": "Fix the setup problem in the working folder."})
        self.assertIn("Command: uv venv venv", text)
        self.assertIn("Fix the setup problem", text)

    def test_notify_posts_the_detail_and_fixes_an_agent_fixable_failure(self):
        failure = {"stage": "setup", "title": "Setup failed", "detail": "Command: uv venv venv\n\nerror: no venv found",
                   "next": "Fix the setup problem in the working folder.", "agentCanFix": True, "sentAt": None}
        _, runs, recover = self._notify(failure)
        self.assertEqual(recover, [("fix", "T-1")])
        body = next(a[-1] for a in runs if a[:3] == ["gh", "issue", "comment"])
        self.assertIn("Command: uv venv venv", body)
        self.assertIn("Fix the setup problem", body)

    def test_notify_retries_a_failure_the_agent_cannot_fix(self):
        # The live terapeuto/3 case: the agent never took its instructions, and the board's next step is `retry`.
        failure = {"stage": "send", "title": "The agent has not started on its instructions",
                   "agentCanFix": False, "auth": False, "sentAt": None}
        _, _, recover = self._notify(failure)
        self.assertEqual(recover, [("retry", "T-1")])

    def test_auth_failure_is_not_recovered(self):
        _, _, recover = self._notify({"stage": "agent", "title": "Needs a login", "auth": True,
                                      "agentCanFix": False, "sentAt": None})
        self.assertEqual(recover, [])

    def test_a_failure_is_recovered_at_most_auto_recover_max_times(self):
        self.c.execute("update tasks set fixes=2 where task_id='T-1'")
        _, _, recover = self._notify({"title": "Setup failed", "agentCanFix": True, "sentAt": None})
        self.assertEqual(recover, [])

    def test_notify_records_the_board_state(self):
        self._notify({"title": "Agent stopped", "agentCanFix": True, "sentAt": None})
        row = self.c.execute("select board_state from tasks where task_id='T-1'").fetchone()
        self.assertEqual(row["board_state"], "error/failed")

    def test_notify_flips_the_label_back_after_a_successful_redrive(self):
        self._notify({"title": "Agent stopped", "agentCanFix": True, "sentAt": None})
        row = self.c.execute("select label from tasks where task_id='T-1'").fetchone()
        self.assertEqual(row["label"], "agent:working")

    def test_a_poll_clears_agent_failed_when_the_board_recovers_without_a_redrive(self):
        # The regression the reviewer found: notify sets agent:failed but used to leave board_state untouched, so
        # a board that recovers on its own left the poll seeing no change and the label stayed failed. With the
        # redrive budget spent, auto-recover is off and the poll alone must clear it.
        self.c.execute("update tasks set fixes=2 where task_id='T-1'")
        self._notify({"title": "Agent stopped", "agentCanFix": True, "sentAt": None})
        row = self.c.execute("select label from tasks where task_id='T-1'").fetchone()
        self.assertEqual(row["label"], "agent:failed")
        board = [{"id": "T-1", "state": "working", "stage": "working", "msg": "Working"}]
        with mock.patch.object(bridge, "hp", return_value=board), \
             mock.patch.object(bridge, "run", return_value=""):
            self.assertTrue(bridge.sync_states(make_cfg(), self.c))
        row = self.c.execute("select label from tasks where task_id='T-1'").fetchone()
        self.assertEqual(row["label"], "agent:working")

    def test_sync_states_recovers_a_new_failure(self):
        failure = {"title": "Setup failed", "detail": "Command: uv venv venv", "agentCanFix": True, "sentAt": None}
        calls, recover = [], []

        def fake_hp(cfg, *args, **kw):
            calls.append(args)
            if args[0] == "list":
                return [{"id": "T-1", "state": "error", "stage": "failed", "msg": "Setup failed"}]
            if args[0] == "details":
                return {"failure": failure}
            return {"raw": "sent"}

        def fake_recover(cfg, *args):
            recover.append(args)
            return True, {"raw": "sent"}

        with mock.patch.object(bridge, "hp", side_effect=fake_hp), \
             mock.patch.object(bridge, "hp_result", side_effect=fake_recover), \
             mock.patch.object(bridge, "run", return_value=""):
            self.assertTrue(bridge.sync_states(make_cfg(), self.c))
        self.assertEqual(recover, [("fix", "T-1")])


if __name__ == "__main__":
    unittest.main()
