"""Local authentication helpers; credentials never enter logs or the database."""
from __future__ import annotations

import getpass
import base64
from datetime import datetime, timezone
import json
import os
import shlex
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from beauclaw.core import API_ORIGIN, InvalidBoard, fetch, parse_board, utcnow
from beauclaw.ui import activity


def credential_stamp(auth_file: Path, token_file: Path | None = None):
    """Watch only the credential source that load_auth actually uses."""
    if token_file is None and os.environ.get("BEAUCLAW_TOKEN", "").strip():
        return None
    try:
        stat = (token_file or auth_file).stat()
        return stat.st_ino, stat.st_mtime_ns, stat.st_size
    except OSError:
        return ()


def credential_metadata(auth: dict, token_file: Path | None = None) -> dict:
    source = "token_file" if token_file else "environment" if os.environ.get("BEAUCLAW_TOKEN", "").strip() else "saved_session"
    result = {"credential_source": source}
    # JWT timestamps are diagnostic hints only; API validation decides validity.
    token = auth.get("token", "").removeprefix("Bearer ")
    try:
        encoded = token.split(".")[1]
        if len(encoded) <= 20000:
            claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            if type(claims.get("exp")) in (int, float):
                expiry = datetime.fromtimestamp(claims["exp"], timezone.utc)
                result.update(expires_at=expiry.isoformat(), expired=expiry <= datetime.now(timezone.utc))
    except (ValueError, IndexError, TypeError, AttributeError, OverflowError, OSError):
        pass
    return result


def load_auth(auth_file: Path, token_file: Path | None = None) -> dict:
    if token_file:
        token = token_file.read_text().strip()
        if not token:
            raise ValueError("The token file is empty")
        return {"token": token}
    if os.environ.get("BEAUCLAW_TOKEN", "").strip():
        return {"token": os.environ["BEAUCLAW_TOKEN"].strip()}
    if not auth_file.exists():
        return {}
    try:
        auth = json.loads(auth_file.read_text())
    except ValueError:
        raise ValueError("Invalid credentials JSON; run beauclaw login again") from None
    if not isinstance(auth, dict) or any(not isinstance(auth.get(k, ""), str) for k in ("token", "cookie")):
        raise ValueError("Invalid credentials format; run beauclaw login again")
    return {key: auth.get(key, "") for key in ("token", "cookie")}


def save_auth(path: Path, auth: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("The credentials file must not be a symbolic link")
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
        raise ValueError(f"Leaderboard credentials were rejected (HTTP {response.status}). "
                         "A GitCode web session is required; gc personal tokens may not work. "
                         "Run beauclaw login --browser.")
    try:
        parse_board(response.body, response.captured_at)
    except InvalidBoard as exc:
        raise ValueError(f"Leaderboard validation failed: {exc}") from None


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
        raise ValueError("No usable gc credentials found; run gc auth login first") from None


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
                raise ValueError("Run login in an interactive terminal, or use --token-file")
            print("Enter the competition website access_token (hidden input), or use login --browser.")
            token = getpass.getpass("GitCode web token: ").strip()
            if not token:
                raise ValueError("No token was entered")
            auth = {"token": token}
        with activity("Verifying leaderboard access..."):
            validate_auth(event_id, auth)
    save_auth(auth_file, auth)
    print(f"Signed in. Leaderboard access verified. Credentials: {auth_file} (mode 600).", flush=True)


LOGIN_ORIGINS = {"https://competition.gitcode.com", "https://gitcode.com"}


def wait_for_browser_auth(event_id: str, context, browser_error, progress, timeout: float = 600) -> dict:
    deadline, next_check = time.monotonic() + timeout, 0.0
    attempts, captured = {}, {}
    page = context.pages[0] if context.pages else context.new_page()
    try:
        # Login redirects and slow subresources must not terminate the session.
        page.goto(f"https://competition.gitcode.com/competition/{event_id}/live-ranking",
                  wait_until="commit", timeout=15000)
    except browser_error:
        progress.update("Page navigation is still settling; waiting for sign-in...")
    while time.monotonic() < deadline:
        pages = list(context.pages)
        tokens = []
        for tab in pages:
            try:
                location = urlsplit(tab.url)
                if f"{location.scheme}://{location.netloc}" not in LOGIN_ORIGINS:
                    continue
                token = tab.evaluate("() => localStorage.getItem('access_token') || ''")
                if isinstance(token, str) and token.strip():
                    tokens.append(token)
            except browser_error:
                continue  # A redirect can destroy the JavaScript execution context.
        if not tokens and pages:
            try:
                for origin in context.storage_state().get("origins", []):
                    if origin.get("origin") in LOGIN_ORIGINS:
                        tokens.extend(item["value"] for item in origin.get("localStorage", [])
                                      if item.get("name") == "access_token" and isinstance(item.get("value"), str)
                                      and item["value"].strip())
            except browser_error:
                pass
        if tokens:
            try:
                cookie = "; ".join(f"{c['name']}={c['value']}" for c in context.cookies(API_ORIGIN))
            except browser_error:
                cookie = ""
            for token in tokens:
                captured[token] = {"token": token, "cookie": cookie}
        if captured and (time.monotonic() >= next_check or not pages):
            token = min(captured, key=lambda value: attempts.get(value, 0))
            progress.update("Session found; verifying leaderboard access...")
            attempts[token] = time.monotonic()
            next_check = time.monotonic() + 10
            try:
                validate_auth(event_id, captured[token])
                return captured[token]
            except ValueError:
                progress.update("Waiting for the leaderboard to accept the session; retrying...")
        if not pages:
            raise ValueError("The browser closed before leaderboard access was verified. "
                             "Run beauclaw login --browser again to reuse the saved browser session.")
        time.sleep(0.5)
    raise ValueError("Sign-in timed out after 10 minutes without verified leaderboard access. "
                     "Your browser profile was kept; run beauclaw login --browser to retry.")


def browser_login(event_id: str, profile: Path) -> dict:
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        raise ValueError("Browser sign-in requires Playwright. Reinstall BeauClaw, "
                         "or run uv sync --extra browser in the source project.") from None
    profile.mkdir(parents=True, mode=0o700, exist_ok=True)
    print("Opening a dedicated browser. Complete GitCode sign-in there; credentials will be saved automatically.", flush=True)
    print("Keep the window open until access is verified. You have 10 minutes; press Ctrl+C to cancel.", flush=True)
    verified = None
    try:
        with sync_playwright() as playwright, activity("Opening the sign-in browser...") as progress:
            kwargs = {"channel": "chrome"} if (shutil.which("google-chrome") or
                      Path("/Applications/Google Chrome.app").exists()) else {}
            try:
                context = playwright.chromium.launch_persistent_context(str(profile), headless=False, **kwargs)
            except Error as exc:
                detail = str(exc).lower()
                if "executable doesn't exist" in detail or "not found" in detail:
                    command = shlex.join([sys.executable, "-m", "playwright", "install", "chromium"])
                    raise ValueError(f"Browser executable missing. Install Chromium in BeauClaw's uv environment:\n  {command}") from None
                if "xserver" in detail or "display" in detail:
                    raise ValueError("Browser sign-in needs a graphical desktop. Use beauclaw login "
                                     "to enter a web token on a headless machine.") from None
                raise ValueError("Could not start the sign-in browser. Close any window using this "
                                 "BeauClaw profile and retry, or choose a separate --profile directory.") from None
            try:
                context.set_default_timeout(3000)
                progress.update("Waiting for GitCode sign-in...")
                verified = wait_for_browser_auth(event_id, context, Error, progress)
            finally:
                try:
                    context.close()
                except Error:
                    pass  # Cleanup must not overwrite a verified sign-in result.
    except Error:
        if verified is None:
            raise ValueError("Lost the browser connection before access was verified. Your browser "
                             "profile was kept; run beauclaw login --browser again.") from None
    return verified
