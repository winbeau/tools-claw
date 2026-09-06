"""Local authentication helpers; credentials never enter logs or the database."""
from __future__ import annotations

import getpass
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from beauclaw.core import API_ORIGIN, InvalidBoard, fetch, parse_board, utcnow


def load_auth(auth_file: Path, token_file: Path | None = None) -> dict:
    if token_file:
        token = token_file.read_text().strip()
        if not token:
            raise ValueError("Token 文件为空")
        return {"token": token}
    if os.environ.get("BEAUCLAW_TOKEN", "").strip():
        return {"token": os.environ["BEAUCLAW_TOKEN"].strip()}
    if not auth_file.exists():
        return {}
    try:
        auth = json.loads(auth_file.read_text())
    except ValueError:
        raise ValueError("登录凭据文件不是有效 JSON，请重新运行 login") from None
    if not isinstance(auth, dict) or any(not isinstance(auth.get(k, ""), str) for k in ("token", "cookie")):
        raise ValueError("登录凭据文件格式错误，请重新运行 login")
    return {key: auth.get(key, "") for key in ("token", "cookie")}


def save_auth(path: Path, auth: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("凭据文件不能是符号链接")
    fd, temporary = tempfile.mkstemp(prefix=".login-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump({**auth, "saved_at": utcnow()}, stream)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_auth(event_id: str, auth: dict) -> None:
    response = fetch(event_id, auth)
    if response.error:
        raise ValueError(response.error)
    if response.status != 200:
        raise ValueError(f"榜单接口未接受此凭据（HTTP {response.status}）。赛事需要网页登录会话；"
                         "gc 的个人访问令牌可能不兼容。可运行 login --browser。")
    try:
        parse_board(response.body, response.captured_at)
    except InvalidBoard as exc:
        raise ValueError(f"凭据验证未通过：{exc}") from None


def gc_auth() -> dict:
    for name in ("GC_TOKEN", "GITCODE_TOKEN"):
        if os.environ.get(name, "").strip():
            return {"token": os.environ[name].strip()}
    directory = Path(os.environ.get("GC_CONFIG_DIR", str(Path.home() / ".config" / "gc")))
    try:
        config = json.loads((directory / "auth.json").read_text())
        host = config["hosts"]["gitcode.com"]
        return {"token": host["users"][host["active_user"]]["token"]}
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError("未找到可用的 gc 登录配置；可先运行 gc auth login") from None


def login(event_id: str, auth_file: Path, *, browser: bool = False,
          from_gc: bool = False, token_file: Path | None = None, profile: Path) -> None:
    if browser:
        auth = browser_login(event_id, profile)
    else:
        if from_gc:
            auth = gc_auth()
        elif token_file:
            auth = load_auth(auth_file, token_file)
        else:
            if not sys.stdin.isatty():
                raise ValueError("请在自己的交互终端运行 login；或指定 --token-file。不要把令牌发到聊天里。")
            print("请输入赛事网页的 access_token（输入不回显）。也可用 login --browser 自动获取。")
            token = getpass.getpass("GitCode 网页登录 Token: ").strip()
            if not token:
                raise ValueError("未输入 Token")
            auth = {"token": token}
        validate_auth(event_id, auth)
    save_auth(auth_file, auth)
    print(f"登录成功，榜单访问已验证。凭据已保存到 {auth_file}（权限 600）。", flush=True)


def browser_login(event_id: str, profile: Path) -> dict:
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        raise ValueError("浏览器登录需要可选依赖：uv run --extra browser beauclaw login --browser") from None
    profile.mkdir(parents=True, mode=0o700, exist_ok=True)
    print("正在打开独立浏览器。请在网页中完成 GitCode 登录；成功后终端会自动保存会话。", flush=True)
    print("不需要复制 Token。10 分钟内有效，Ctrl+C 可取消。", flush=True)
    try:
        with sync_playwright() as playwright:
            kwargs = {"channel": "chrome"} if shutil.which("google-chrome") else {}
            with playwright.chromium.launch_persistent_context(str(profile), headless=False, **kwargs) as context:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(f"https://competition.gitcode.com/competition/{event_id}/live-ranking",
                          wait_until="domcontentloaded", timeout=60000)
                deadline, next_check = time.monotonic() + 600, 0.0
                while time.monotonic() < deadline:
                    if not context.pages:
                        raise ValueError("浏览器已关闭，登录尚未完成")
                    for tab in context.pages:
                        if urlsplit(tab.url).hostname not in ("competition.gitcode.com", "gitcode.com"):
                            continue
                        token = tab.evaluate("() => localStorage.getItem('access_token') || ''")
                        if not token or time.monotonic() < next_check:
                            continue
                        cookies = context.cookies(API_ORIGIN)
                        auth = {"token": token, "cookie": "; ".join(f"{c['name']}={c['value']}" for c in cookies)}
                        next_check = time.monotonic() + 10
                        try:
                            validate_auth(event_id, auth)
                            return auth
                        except ValueError:
                            pass
                    time.sleep(1)
    except Error:
        raise ValueError("浏览器登录未完成。请检查 Chrome；没有 Chrome 时先运行 python -m playwright install chromium。") from None
    raise ValueError("登录等待超时，请重新运行 login --browser")
