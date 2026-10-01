#!/usr/bin/env python3
"""多 Codex 账号切换工具。

登录凭据分别保存；运行时共用会话目录，切换后可恢复原会话。
"""

from __future__ import annotations

import argparse
import base64
import binascii
import datetime as datetime_module
import fcntl
import json
import os
import re
import shlex
import shutil
import selectors
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


DEFAULT_SLOTS = ("1", "2")
AUTH_FILE = "auth.json"
CONFIG_FILE = "config.toml"
STATE_FILE = "state.json"
STORE_LINE = 'cli_auth_credentials_store = "file"'
STORE_RE = re.compile(r"(?m)^\s*cli_auth_credentials_store\s*=.*(?:\n|$)")


class SwitcherError(RuntimeError):
    pass


def root_path() -> Path:
    value = os.environ.get("CODEX_SWITCHER_HOME")
    return Path(value).expanduser() if value else Path.home() / ".codex-switcher"


def slot(value: str) -> str:
    if not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise SwitcherError("账号槽位必须是正整数，例如 1、2、3。")
    return str(value)


def account_slots(root: Path) -> List[str]:
    """兼容原有槽位，并发现按需创建的账号目录。"""
    slots = set(DEFAULT_SLOTS)
    slots.add(slot(load_state(root)["active"]))
    accounts = root / "accounts"
    if accounts.is_dir():
        slots.update(path.name for path in accounts.iterdir()
                     if path.is_dir() and re.fullmatch(r"[1-9][0-9]*", path.name))
    return sorted(slots, key=lambda value: (len(value), value))


def slot_argument(value: str) -> str:
    try:
        return slot(value)
    except SwitcherError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def private_file(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def write_atomic(path: Path, text: str) -> None:
    private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".cxs-", dir=str(path.parent))
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        private_file(temp_path)
        os.replace(str(temp_path), str(path))
    finally:
        if temp_path.exists():
            temp_path.unlink()


def default_state() -> Dict[str, Any]:
    return {"active": "1", "labels": {"1": "账号 1", "2": "账号 2"}}


def load_state(root: Path) -> Dict[str, Any]:
    path = root / STATE_FILE
    if not path.exists():
        return default_state()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SwitcherError("state.json 损坏，请检查该文件。") from exc
    if not isinstance(state, dict):
        raise SwitcherError("state.json 格式不正确。")
    state.setdefault("active", "1")
    state.setdefault("labels", {"1": "账号 1", "2": "账号 2"})
    slot(str(state["active"]))
    return state


def save_state(root: Path, state: Dict[str, Any]) -> None:
    write_atomic(root / STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def account_home(root: Path, account_slot: str) -> Path:
    return root / "accounts" / slot(account_slot)


def ensure_config(home: Path, source: Optional[Path] = None) -> None:
    private_dir(home)
    target = home / CONFIG_FILE
    if source is not None and source.exists():
        content = source.read_text(encoding="utf-8")
    elif target.exists():
        content = target.read_text(encoding="utf-8")
    else:
        content = ""
    content = STORE_RE.sub("", content)
    if content and not content.endswith("\n"):
        content += "\n"
    write_atomic(target, STORE_LINE + "\n" + content)


def init_layout(root: Path) -> None:
    private_dir(root)
    private_dir(root / "accounts")
    for account_slot in account_slots(root):
        home = account_home(root, account_slot)
        private_dir(home)
        ensure_config(home)
    if not (root / STATE_FILE).exists():
        save_state(root, default_state())


def codex_binary() -> str:
    configured = os.environ.get("CODEX_BIN")
    if configured:
        return configured
    executable = shutil.which("codex")
    if not executable:
        raise SwitcherError("找不到 codex 命令，请先安装 Codex CLI，或设置 CODEX_BIN。")
    return executable


def codex_env(home: Path) -> Dict[str, str]:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(home)
    return environment


def current_slot(root: Path) -> str:
    return slot(load_state(root)["active"])


def token_claims(token: Any) -> Dict[str, Any]:
    if not isinstance(token, str):
        return {}
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    try:
        payload = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        claims = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error):
        return {}
    return claims if isinstance(claims, dict) else {}


def email_from_mapping(value: Any) -> Optional[str]:
    if not isinstance(value, dict):
        return None
    candidates = [value.get("email")]
    profile = value.get("https://api.openai.com/profile")
    if isinstance(profile, dict):
        candidates.append(profile.get("email"))
    for candidate in candidates:
        if isinstance(candidate, str) and "@" in candidate:
            return candidate
    return None


def account_email(home: Path) -> Optional[str]:
    """读取本地登录凭据中的邮箱，不返回或记录 token 内容。"""
    auth_path = home / AUTH_FILE
    try:
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(auth, dict):
        return None

    candidates: List[Any] = [auth]
    tokens = auth.get("tokens")
    if isinstance(tokens, dict):
        candidates.extend([tokens, token_claims(tokens.get("id_token"))])

    for candidate in candidates:
        email = email_from_mapping(candidate)
        if email:
            return email
    return None


def account_title(item: Dict[str, Any]) -> str:
    email = item.get("email")
    return "账号 %s（%s）" % (item["slot"], email) if email else "账号 %s" % item["slot"]


def login_status(root: Path, account_slot: str) -> Dict[str, Any]:
    home = account_home(root, account_slot)
    email = account_email(home)
    try:
        result = subprocess.run(
            [codex_binary(), "login", "status"],
            env=codex_env(home),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError as exc:
        return {"slot": account_slot, "email": email, "logged_in": False, "error": str(exc)}
    output = ((result.stdout or "") + " " + (result.stderr or "")).lower()
    mode = "chatgpt" if "chatgpt" in output else "api" if "api key" in output else None
    return {
        "slot": account_slot,
        "email": email,
        "logged_in": result.returncode == 0,
        "mode": mode,
        "error": None if result.returncode == 0 else "未登录",
    }


def login(root: Path, account_slot: str, device_auth: bool) -> int:
    home = account_home(root, account_slot)
    private_dir(home)
    ensure_config(home)
    command = [codex_binary(), "login"]
    if device_auth:
        command.append("--device-auth")
    try:
        return subprocess.run(command, env=codex_env(home), check=False).returncode
    except OSError as exc:
        raise SwitcherError("启动 codex login 失败：%s" % exc) from exc


def import_account(root: Path, account_slot: str, source: Path, confirmed: bool) -> None:
    if not confirmed:
        raise SwitcherError("导入会复制本地登录凭据，请加 --yes 确认。")
    source = source.expanduser().resolve()
    source_auth = source / AUTH_FILE
    target = account_home(root, account_slot)
    target_auth = target / AUTH_FILE
    if not source_auth.exists():
        raise SwitcherError("源目录没有 auth.json，请直接运行 login %s。" % account_slot)
    if source_auth.resolve() == target_auth.resolve():
        return
    private_dir(target)
    shutil.copyfile(str(source_auth), str(target_auth))
    private_file(target_auth)
    source_config = source / CONFIG_FILE
    ensure_config(target, source_config if source_config.exists() else None)


def json_lines(text: str) -> Iterable[Dict[str, Any]]:
    for line in text.splitlines():
        try:
            value = json.loads(line.strip())
        except ValueError:
            continue
        if isinstance(value, dict):
            yield value


def send_json(process: subprocess.Popen, message: Dict[str, Any]) -> None:
    if process.stdin is None:
        raise OSError("app-server stdin 不可用")
    process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
    process.stdin.flush()


def read_app_server(
    process: subprocess.Popen,
    response_id: int,
    timeout: float,
    accept_rate_update: bool = False,
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """读取 app-server 消息，直到目标响应或超时，不等待长驻进程退出。"""
    if process.stdout is None or process.stderr is None:
        return None, []
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    stderr_lines: List[str] = []
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            events = selector.select(remaining)
            if not events:
                break
            for key, _ in events:
                line = key.fileobj.readline()
                if not line:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stderr":
                    stderr_lines.append(line.strip())
                    continue
                try:
                    message = next(json_lines(line))
                except StopIteration:
                    continue
                if message.get("id") == response_id:
                    return message, stderr_lines
                if accept_rate_update and message.get("method") == "account/rateLimits/updated":
                    params = message.get("params")
                    if isinstance(params, dict):
                        return {"id": response_id, "result": params}, stderr_lines
    finally:
        selector.close()
    return None, stderr_lines


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        pass
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            stream.close()


def query_quota(root: Path, account_slot: str) -> Dict[str, Any]:
    """读取当前 Codex CLI app-server 的 rate-limit snapshot。"""
    home = account_home(root, account_slot)
    process = None
    try:
        process = subprocess.Popen(
            [codex_binary(), "app-server", "--stdio"],
            env=codex_env(home),
            cwd=str(home),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        send_json(process, {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "codex-account-switcher", "version": "1.0.0"},
                "capabilities": {"experimentalApi": True},
            },
        })
        initialize_response, stderr_lines = read_app_server(process, 1, 10)
        if initialize_response is None:
            detail = stderr_lines[-1] if stderr_lines else "初始化超时（10 秒）"
            return {"slot": account_slot, "error": detail[:240]}
        if initialize_response.get("error"):
            error = initialize_response["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            return {"slot": account_slot, "error": str(message)[:240]}
        send_json(process, {"method": "initialized"})
        send_json(process, {"id": 2, "method": "account/rateLimits/read", "params": None})
        response, stderr_lines = read_app_server(process, 2, 20, accept_rate_update=True)
        if response is None:
            detail = stderr_lines[-1] if stderr_lines else "读取额度超时（20 秒）"
            return {"slot": account_slot, "error": detail[:240]}
    except (OSError, ValueError) as exc:
        return {"slot": account_slot, "error": "app-server 通信失败：%s" % exc}
    finally:
        if process is not None:
            stop_process(process)
    if response.get("error"):
        error = response["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        return {"slot": account_slot, "error": str(message)[:240]}
    result = response.get("result")
    return {"slot": account_slot, "result": result} if isinstance(result, dict) else {"slot": account_slot, "error": "额度响应格式不受支持。"}


def buckets(result: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    grouped = result.get("rateLimitsByLimitId")
    if isinstance(grouped, dict):
        values = [(str(name), value) for name, value in grouped.items() if isinstance(value, dict)]
        if values:
            return values
    single = result.get("rateLimits")
    return [("codex", single)] if isinstance(single, dict) else []


def reset_at(value: Any) -> str:
    try:
        timestamp = int(value)
        return datetime_module.datetime.fromtimestamp(timestamp).strftime("%m-%d %H:%M") if timestamp > 0 else "未知"
    except (TypeError, ValueError, OverflowError, OSError):
        return "未知"


def window_line(name: str, window: Dict[str, Any]) -> str:
    duration = window.get("windowDurationMins")
    label = "5 小时" if duration == 300 else "每周" if duration == 10080 else "%s 分钟" % duration if duration else name
    try:
        used = max(0, min(100, int(window.get("usedPercent"))))
        remaining = 100 - used
        usage = "已用 %d%%，剩余 %d%%" % (used, remaining)
    except (TypeError, ValueError):
        usage = "已用未知，剩余未知"
    return "%s：%s，重置 %s" % (label, usage, reset_at(window.get("resetsAt")))


def quota_text(snapshot: Dict[str, Any], active: bool) -> List[str]:
    title = "账号 %s%s" % (snapshot["slot"], " [当前]" if active else "")
    lines = [title]
    if snapshot.get("error"):
        lines.append("  状态：%s" % snapshot["error"])
        return lines
    result = snapshot["result"]
    all_buckets = buckets(result)
    for name, item in all_buckets:
        if item.get("planType"):
            lines.append("  套餐：%s" % item["planType"])
        if isinstance(item.get("primary"), dict):
            lines.append("  " + window_line("主要窗口", item["primary"]))
        if isinstance(item.get("secondary"), dict):
            lines.append("  " + window_line("次要窗口", item["secondary"]))
        credits = item.get("credits")
        if isinstance(credits, dict):
            if credits.get("unlimited"):
                lines.append("  credits：不限")
            elif credits.get("balance") is not None:
                lines.append("  credits：%s" % credits["balance"])
    reset_credits = result.get("rateLimitResetCredits")
    if isinstance(reset_credits, dict):
        lines.append("  可用重置次数：%s" % reset_credits.get("availableCount", 0))
    if not all_buckets:
        lines.append("  状态：没有可用额度窗口")
    return lines


def show_status(root: Path, as_json: bool) -> int:
    state = load_state(root)
    result = [login_status(root, account_slot) for account_slot in account_slots(root)]
    for item in result:
        item["active"] = item["slot"] == state["active"]
    if as_json:
        print(json.dumps({"active": state["active"], "accounts": result}, ensure_ascii=False, indent=2))
    else:
        active_item = next(item for item in result if item["active"])
        print("当前账号：%s" % account_title(active_item))
        for item in result:
            marker = "*" if item["active"] else " "
            mode = "（%s）" % item["mode"] if item.get("mode") else ""
            print("%s %s：%s%s" % (marker, account_title(item), "已登录" if item["logged_in"] else "未登录", mode))
    return 0


def show_quota(root: Path, account_slot: Optional[str], as_json: bool) -> int:
    state = load_state(root)
    selected = [slot(account_slot)] if account_slot else account_slots(root)
    result = [query_quota(root, item) for item in selected]
    if as_json:
        print(json.dumps({"active": state["active"], "accounts": result}, ensure_ascii=False, indent=2))
    else:
        print("\n\n".join("\n".join(quota_text(item, item["slot"] == state["active"])) for item in result))
    return 0 if all("error" not in item for item in result) else 1


def session_home(root: Path) -> Path:
    state = load_state(root)
    return Path(state.get("session_home") or os.environ.get("CODEX_SESSION_HOME")
                or str(Path.home() / ".codex")).expanduser().resolve()


def auth_text(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
        value = json.loads(text)
    except (OSError, ValueError) as exc:
        raise SwitcherError("无法读取有效登录凭据：%s；请先 login 或 import。" % path) from exc
    if not isinstance(value, dict) or not (value.get("tokens") or value.get("OPENAI_API_KEY")
                                         or value.get("agent_identity")
                                         or value.get("personal_access_token")):
        raise SwitcherError("登录凭据为空：%s" % path)
    return text


def run_codex(root: Path, arguments: List[str]) -> int:
    selected = current_slot(root)
    credential = account_home(root, selected) / AUTH_FILE
    # Validate everything before replacing the shared runtime credentials.
    text = auth_text(credential)
    executable = codex_binary()
    shared = session_home(root)
    private_dir(shared)
    if shared in [account_home(root, item).resolve() for item in account_slots(root)]:
        raise SwitcherError("会话目录必须与账号凭据目录分开。")
    with (shared / ".switcher.lock").open("a") as lock:
        private_file(shared / ".switcher.lock")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SwitcherError("已有切换工具启动的 Codex 正在运行；请先退出该进程。") from exc
        runtime_auth = shared / AUTH_FILE
        backup = shared / "auth.before-switcher.json"
        if runtime_auth.exists() and not backup.exists():
            write_atomic(backup, runtime_auth.read_text(encoding="utf-8"))
        write_atomic(runtime_auth, text)
        command = [executable, "-c", 'cli_auth_credentials_store="file"']
        # New releases can reuse a daemon that still holds another account's auth.
        help_result = subprocess.run([executable, "--help"], stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, check=False)
        interactive = not arguments or arguments[0].startswith("-") or arguments[0] in ("resume", "fork")
        if interactive and "--no-daemon" in help_result.stdout and "--no-daemon" not in arguments:
            command.append("--no-daemon")
        command.extend(arguments)
        environment = codex_env(shared)
        environment["CODEX_ACCOUNT_SLOT"] = selected
        state = load_state(root)
        state["active"] = selected
        save_state(root, state)
        try:
            return subprocess.run(command, env=environment, check=False).returncode
        finally:
            # Keep refreshed tokens, including after an interrupted or failed run.
            if runtime_auth.exists():
                write_atomic(credential, auth_text(runtime_auth))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="codex-switcher", description="多 Codex 账号切换、状态和额度工具")
    result.add_argument("--root", type=Path, help="账号目录，默认 ~/.codex-switcher")
    commands = result.add_subparsers(dest="command")
    commands.add_parser("init", help="初始化账号目录；更多账号通过 login 或 import 添加")
    import_command = commands.add_parser("import", help="导入现有 CODEX_HOME 的文件凭据")
    import_command.add_argument("slot", type=slot_argument, metavar="账号编号")
    import_command.add_argument("--source", type=Path, help="源 CODEX_HOME，默认当前 CODEX_HOME 或 ~/.codex")
    import_command.add_argument("--yes", action="store_true")
    login_command = commands.add_parser("login", help="登录指定账号；默认使用 device-auth")
    login_command.add_argument("slot", type=slot_argument, metavar="账号编号")
    login_command.add_argument("--device-auth", action="store_true", help="显式选择设备码登录（默认）")
    login_command.add_argument("--browser-auth", action="store_true", help="改用浏览器 OAuth 登录")
    logout_command = commands.add_parser("logout", help="退出指定账号")
    logout_command.add_argument("slot", type=slot_argument, metavar="账号编号")
    status_command = commands.add_parser("status", help="显示登录状态")
    status_command.add_argument("--json", action="store_true")
    quota_command = commands.add_parser("quota", help="显示额度")
    quota_command.add_argument("slot", nargs="?", type=slot_argument, metavar="账号编号")
    quota_command.add_argument("--json", action="store_true")
    use_command = commands.add_parser("use", aliases=["switch"], help="切换默认账号")
    use_command.add_argument("slot", type=slot_argument, metavar="账号编号")
    path_command = commands.add_parser("path", help="打印账号 CODEX_HOME")
    path_command.add_argument("slot", type=slot_argument, metavar="账号编号")
    env_command = commands.add_parser("env", help="打印账号环境变量")
    env_command.add_argument("slot", type=slot_argument, metavar="账号编号")
    run_command = commands.add_parser("run", aliases=["exec"], help="用当前账号启动 codex")
    run_command.add_argument("arguments", nargs=argparse.REMAINDER)
    resume_command = commands.add_parser("resume", help="使用指定账号恢复共用目录中的会话")
    resume_command.add_argument("slot", nargs="?", type=slot_argument, metavar="账号编号")
    resume_command.add_argument("--session", help="指定会话 ID；默认恢复当前项目最近会话")
    commands.add_parser("current", help="显示默认账号")
    return result


def main(arguments: Optional[List[str]] = None) -> int:
    command_parser = parser()
    args = command_parser.parse_args(arguments)
    root = (args.root if args.root else root_path()).expanduser().resolve()
    try:
        if args.command == "init":
            init_layout(root)
            print("已初始化账号目录：%s；可通过 login <编号> 或 import <编号> 添加更多账号。" % (root / "accounts"))
            return 0
        init_layout(root)
        if args.command == "import":
            source = args.source or Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
            import_account(root, args.slot, source, args.yes)
            state = load_state(root)
            state["session_home"] = str(source.expanduser().resolve())
            save_state(root, state)
            print("已将凭据导入账号 %s；共用会话目录：%s。" % (args.slot, session_home(root)))
            return 0
        if args.command == "login":
            if args.device_auth and args.browser_auth:
                raise SwitcherError("--device-auth 和 --browser-auth 不能同时使用。")
            return login(root, args.slot, not args.browser_auth)
        if args.command == "logout":
            return subprocess.run([codex_binary(), "logout"], env=codex_env(account_home(root, args.slot)), check=False).returncode
        if args.command == "status":
            return show_status(root, args.json)
        if args.command == "quota":
            return show_quota(root, args.slot, args.json)
        if args.command in ("use", "switch"):
            state = load_state(root)
            state["active"] = slot(args.slot)
            save_state(root, state)
            print("已切换到账号 %s；新的 codex 进程将使用该账号。" % args.slot)
            return 0
        if args.command == "path":
            print(account_home(root, args.slot))
            return 0
        if args.command == "env":
            print("export CODEX_HOME=%s" % shlex.quote(str(session_home(root))))
            print("export CODEX_ACCOUNT_SLOT=%s" % shlex.quote(args.slot))
            return 0
        if args.command == "resume":
            if args.slot:
                state = load_state(root)
                state["active"] = args.slot
                save_state(root, state)
                os.environ["CODEX_ACCOUNT_SLOT"] = args.slot
            return run_codex(root, ["resume", args.session] if args.session else ["resume", "--last"])
        if args.command in ("run", "exec"):
            arguments = args.arguments
            if arguments[:1] == ["--"]:
                arguments = arguments[1:]
            return run_codex(root, arguments)
        if args.command == "current":
            print(load_state(root)["active"])
            return 0
        command_parser.print_help()
        return 0
    except KeyboardInterrupt:
        return 130
    except (SwitcherError, OSError) as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
