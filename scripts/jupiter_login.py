#!/usr/bin/env python3
"""Launch a visible, dedicated macOS Chrome profile for Jupiter sign-in.

This command only starts Chrome. It never reads another browser's profile,
imports cookies, receives a password, or submits a form. Quit the dedicated
Chrome instance after signing in; the collector reuses its profile later.
"""
import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

from jupiter_collector import CollectorError, LOGIN_URL, browser_lock, config_at
from jupiter_browser_session import ensure_profile_idle


DEFAULT_CHROME = "Google Chrome"


def _default_profile_paths():
    home = Path.home()
    return {
        (home / "Library" / "Application Support" / "Google" / "Chrome").resolve(),
        (home / "Library" / "Application Support" / "Google" / "Chrome Beta").resolve(),
        (home / "Library" / "Application Support" / "Google" / "Chrome Dev").resolve(),
        (home / "Library" / "Application Support" / "Google" / "Chrome Canary").resolve(),
    }


def launch_visible_login(instance_path, chrome_app=DEFAULT_CHROME, runner=None):
    """Start a visible Chrome app using only this instance's dedicated profile."""
    if platform.system() != "Darwin":
        raise CollectorError("unsupported_platform", "Visible dedicated Chrome login is currently supported on macOS only.")
    config = config_at(instance_path)
    profile = Path(config["profile_dir"]).expanduser().resolve()
    state = Path(config["state_dir"]).expanduser().resolve()
    if profile in _default_profile_paths() or profile == Path.home().resolve():
        raise CollectorError("profile_not_dedicated", "profile_dir must be a separate directory; the normal Chrome profile is not allowed.")
    if profile == state:
        raise CollectorError("profile_not_dedicated", "profile_dir must be separate from the state directory.")
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(profile, 0o700)
    opener = shutil.which("open")
    if not opener:
        raise CollectorError("open_unavailable", "macOS 'open' command was not found.")
    run = runner or subprocess.run
    command = [opener, "-na", chrome_app, "--args",
               "--user-data-dir=" + str(profile), "--new-window", LOGIN_URL]
    with browser_lock(config):
        ensure_profile_idle(profile)
        try:
            run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except (OSError, subprocess.CalledProcessError) as error:
            raise CollectorError("chrome_launch_failed", "Could not launch the dedicated Chrome login window.") from error
        # A previous snapshot must never overwrite cookies from this new login.
        # Remove it only after Chrome accepts the launch, while collection is
        # excluded by the same lock. Chrome retains the dedicated profile.
        (state / "collector-session.json").unlink(missing_ok=True)
    return {"ok": True, "login_url": LOGIN_URL, "profile_dir": str(profile),
            "dedicated_profile": True, "password_read": False, "cookies_imported": False,
            "message": "请在新窗口自行登录 Jupiter；完成后退出这个专用 Chrome（⌘Q），再同步。程序会复用该专用登录状态。"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", required=True, type=Path)
    parser.add_argument("--chrome-app", default=DEFAULT_CHROME,
                        help="macOS Chrome app name (default: Google Chrome)")
    args = parser.parse_args(argv)
    try:
        result = launch_visible_login(args.instance, args.chrome_app)
    except Exception as error:
        result = {"ok": False, "code": getattr(error, "code", "login_launch_failed"),
                  "message": str(error)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
