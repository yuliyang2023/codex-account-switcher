import base64
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import fcntl
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


path = Path(__file__).resolve().parents[1] / "codex-switcher.py"
spec = importlib.util.spec_from_file_location("codex_switcher", path)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


class CodexSwitcherTests(unittest.TestCase):
    @staticmethod
    def _jwt(claims):
        def encode(value):
            return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

        return "%s.%s.signature" % (encode({"alg": "none"}), encode(claims))

    def test_device_auth_is_default(self):
        args = module.parser().parse_args(["login", "2"])
        self.assertFalse(args.browser_auth)

    def test_commands_accept_more_accounts_and_reject_invalid_slots(self):
        for command in ("login", "import", "logout", "quota", "use", "switch", "path", "env", "resume"):
            self.assertEqual(module.parser().parse_args([command, "12"]).slot, "12")
        for value in ("0", "-1", "01", "../3", "3/4", "abc", "３"):
            with self.assertRaises(module.SwitcherError):
                module.slot(value)

    def test_import_and_switch_third_account_preserves_existing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "switcher"
            source = Path(directory) / "source"
            source.mkdir()
            original = '{"tokens":{"access_token":"third"}}'
            (source / module.AUTH_FILE).write_text(original)
            module.init_layout(root)
            state = module.load_state(root)
            state["labels"]["1"] = "原账号"
            module.save_state(root, state)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(module.main(["--root", str(root), "import", "3",
                                              "--source", str(source), "--yes"]), 0)
                self.assertEqual(module.main(["--root", str(root), "use", "3"]), 0)
            self.assertEqual(module.current_slot(root), "3")
            self.assertEqual(module.load_state(root)["labels"]["1"], "原账号")
            self.assertEqual((module.account_home(root, "3") / module.AUTH_FILE).read_text(), original)
            self.assertIn(module.STORE_LINE, (module.account_home(root, "3") / module.CONFIG_FILE).read_text())

    def test_status_and_quota_discover_all_accounts_in_numeric_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module.init_layout(root)
            for value in ("10", "3", "junk", "01"):
                (root / "accounts" / value).mkdir()
            expected = ["1", "2", "3", "10"]
            self.assertEqual(module.account_slots(root), expected)
            def status(_root, value):
                return {"slot": value, "logged_in": False}
            with patch.object(module, "login_status", side_effect=status) as mocked, redirect_stdout(io.StringIO()):
                self.assertEqual(module.show_status(root, True), 0)
                self.assertEqual([call.args[1] for call in mocked.call_args_list], expected)
            with patch.object(module, "query_quota", side_effect=lambda _root, value: {"slot": value}) as mocked, redirect_stdout(io.StringIO()):
                self.assertEqual(module.show_quota(root, None, True), 0)
                self.assertEqual([call.args[1] for call in mocked.call_args_list], expected)

    def test_init_keeps_file_auth_store(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module.init_layout(root)
            for account_slot in module.DEFAULT_SLOTS:
                config = module.account_home(root, account_slot) / module.CONFIG_FILE
                self.assertIn(module.STORE_LINE, config.read_text())

    def test_window_text(self):
        result = {
            "rateLimits": {
                "primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 0},
                "secondary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": 0},
            }
        }
        lines = module.quota_text({"slot": "1", "result": result}, True)
        self.assertTrue(any("5 小时" in line and "剩余 75%" in line for line in lines))
        self.assertTrue(any("每周" in line and "剩余 60%" in line for line in lines))

    def test_account_email_reads_id_token_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            auth = {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": "not printed",
                    "id_token": self._jwt({
                        "https://api.openai.com/profile": {"email": "one@example.com"}
                    }),
                },
            }
            (home / module.AUTH_FILE).write_text(json.dumps(auth), encoding="utf-8")
            self.assertEqual(module.account_email(home), "one@example.com")

    def test_status_text_includes_email(self):
        root = Path(tempfile.mkdtemp())
        try:
            output = io.StringIO()
            responses = [
                {"slot": "1", "email": "one@example.com", "logged_in": True, "mode": "chatgpt"},
                {"slot": "2", "email": "two@example.com", "logged_in": True, "mode": "chatgpt"},
            ]
            with patch.object(module, "login_status", side_effect=responses), redirect_stdout(output):
                self.assertEqual(module.show_status(root, False), 0)
            self.assertIn("当前账号：账号 1（one@example.com）", output.getvalue())
            self.assertIn("* 账号 1（one@example.com）：已登录（chatgpt）", output.getvalue())
            self.assertIn("  账号 2（two@example.com）：已登录（chatgpt）", output.getvalue())
        finally:
            # The test only creates the temporary directory itself.
            root.rmdir()

    def test_shared_runtime_preserves_session_and_refreshes_account(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "accounts"
            shared = Path(directory) / "shared"
            module.init_layout(root)
            state = module.load_state(root)
            state["active"] = "3"
            module.save_state(root, state)
            module.init_layout(root)
            shared.mkdir()
            session = shared / "sessions" / "previous.jsonl"
            session.parent.mkdir()
            session.write_text("previous conversation")
            original = '{"tokens":{"access_token":"original"}}'
            (shared / "auth.json").write_text(original)
            credential = module.account_home(root, "3") / "auth.json"
            credential.write_text('{"tokens":{"access_token":"account-2"}}')
            fake = Path(directory) / "fake-codex"
            fake.write_text("#!" + sys.executable + "\n" +
                "import os,sys,json\nfrom pathlib import Path\n"
                "if '--help' in sys.argv:\n print('--no-daemon'); sys.exit(0)\n"
                "home=Path(os.environ['CODEX_HOME'])\n"
                "assert json.loads((home/'auth.json').read_text())['tokens']['access_token']=='account-2'\n"
                "assert (home/'sessions/previous.jsonl').read_text()=='previous conversation'\n"
                "(home/'invocation.json').write_text(json.dumps(sys.argv[1:]))\n"
                "(home/'auth.json').write_text(json.dumps({'tokens':{'access_token':'refreshed'}}))\n"
                "sys.exit(7)\n")
            fake.chmod(0o700)
            with patch.dict(os.environ, {"CODEX_BIN": str(fake), "CODEX_SESSION_HOME": str(shared),
                                         "CODEX_ACCOUNT_SLOT": "3"}):
                self.assertEqual(module.run_codex(root, ["resume", "--last"]), 7)
            self.assertEqual(json.loads(credential.read_text())["tokens"]["access_token"], "refreshed")
            self.assertEqual((shared / "auth.before-switcher.json").read_text(), original)
            self.assertEqual(session.read_text(), "previous conversation")
            self.assertIn("--no-daemon", json.loads((shared / "invocation.json").read_text()))
            self.assertEqual(credential.stat().st_mode & 0o777, 0o600)

    def test_new_account_cannot_use_credential_directory_as_session_home(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module.init_layout(root)
            home = module.account_home(root, "3")
            home.mkdir()
            original = '{"tokens":{"access_token":"third"}}'
            (home / module.AUTH_FILE).write_text(original)
            state = module.load_state(root)
            state.update(active="3", session_home=str(home))
            module.save_state(root, state)
            with patch.object(module, "codex_binary", return_value="codex"):
                with self.assertRaises(module.SwitcherError):
                    module.run_codex(root, [])
            self.assertEqual((home / module.AUTH_FILE).read_text(), original)

    def test_shell_resume_switches_to_third_account(self):
        for shell in ("bash", "zsh"):
            if not shutil.which(shell):
                continue
            with self.subTest(shell=shell), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "switcher"
                shared = Path(directory) / "shared"
                source = path.parent / "codex-switcher.zsh"
                module.init_layout(root)
                home = module.account_home(root, "3")
                home.mkdir()
                (home / module.AUTH_FILE).write_text('{"tokens":{"access_token":"third"}}')
                fake = Path(directory) / "fake-codex"
                fake.write_text("#!" + sys.executable + "\n" +
                    "import os,sys\nfrom pathlib import Path\n"
                    "if '--help' in sys.argv: sys.exit(0)\n"
                    "assert os.environ['CODEX_ACCOUNT_SLOT']=='3'\n"
                    "assert sys.argv[-2:]==['resume','--last']\n")
                fake.chmod(0o700)
                environment = os.environ.copy()
                environment.update(CODEX_SWITCHER_HOME=str(root), CODEX_SESSION_HOME=str(shared),
                                   CODEX_BIN=str(fake), CODEX_ACCOUNT_SLOT="1")
                result = subprocess.run([shell, "-c", 'source "$1"; cxs resume 3 || exit; '
                                         'test "$CODEX_ACCOUNT_SLOT" = 3', "cxs-test", str(source)],
                                        env=environment, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(module.current_slot(root), "3")

    def test_missing_credentials_does_not_replace_runtime_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "accounts"
            shared = Path(directory) / "shared"
            module.init_layout(root)
            state = module.load_state(root)
            state["active"] = "2"
            module.save_state(root, state)
            shared.mkdir()
            (shared / "auth.json").write_text("original")
            with patch.dict(os.environ, {"CODEX_SESSION_HOME": str(shared), "CODEX_ACCOUNT_SLOT": "2"}):
                with self.assertRaises(module.SwitcherError):
                    module.run_codex(root, [])
            self.assertEqual((shared / "auth.json").read_text(), "original")

    def test_concurrent_run_is_rejected_before_credentials_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "accounts"
            shared = Path(directory) / "shared"
            module.init_layout(root)
            state = module.load_state(root)
            state["active"] = "2"
            module.save_state(root, state)
            shared.mkdir()
            (shared / "auth.json").write_text("original")
            (module.account_home(root, "2") / "auth.json").write_text('{"tokens":{"access_token":"two"}}')
            with (shared / ".switcher.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.dict(os.environ, {"CODEX_SESSION_HOME": str(shared), "CODEX_ACCOUNT_SLOT": "2"}), \
                        patch.object(module, "codex_binary", return_value="codex"):
                    with self.assertRaises(module.SwitcherError):
                        module.run_codex(root, [])
            self.assertEqual((shared / "auth.json").read_text(), "original")

    def test_query_quota_performs_handshake(self):
        class FakeStdin:
            def __init__(self):
                self.payload = ""

            def write(self, value):
                self.payload += value

            def flush(self):
                pass

            def close(self):
                pass

        class FakeProcess:
            def __init__(self):
                self.stdin = FakeStdin()
                self.stdout = None
                self.stderr = None
                self.running = True

            def kill(self):
                self.running = False

            def poll(self):
                return None if self.running else 0

            def wait(self, timeout):
                self.running = False

        process = FakeProcess()
        responses = [
            ({"id": 1, "result": {}}, []),
            ({"id": 2, "result": {"rateLimits": {}}}, []),
        ]
        with patch.object(module.subprocess, "Popen", return_value=process), patch.object(
            module, "read_app_server", side_effect=responses
        ):
            result = module.query_quota(Path("/tmp/cxs-test"), "1")
        self.assertEqual(result, {"slot": "1", "result": {"rateLimits": {}}})
        messages = [json.loads(line) for line in process.stdin.payload.splitlines()]
        self.assertEqual(messages[0]["method"], "initialize")
        self.assertNotIn("params", messages[1])
        self.assertEqual(messages[2]["method"], "account/rateLimits/read")
        self.assertIsNone(messages[2]["params"])


if __name__ == "__main__":
    unittest.main()
