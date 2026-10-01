"""Coordinate this program's dedicated Chrome profile without reading cookies."""
import os
from pathlib import Path
import re
import subprocess


class BrowserSessionError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def ensure_profile_idle(profile, runner=None):
    """Do not open a second browser on a profile still used by visible login.

    Chrome's own singleton remains the final race protection. The process check
    also handles macOS builds that do not expose a SingletonLock file. It only
    inspects process arguments; no browser profile or cookie data is read.
    """
    profile = Path(profile).expanduser().resolve()
    lock = profile / "SingletonLock"
    if lock.is_symlink():
        try:
            pid = int(os.readlink(lock).rsplit("-", 1)[1])
            if pid > 0:
                os.kill(pid, 0)
                raise BrowserSessionError("browser_busy", "The dedicated Jupiter login browser is still running; quit it before syncing.")
        except ProcessLookupError:
            pass  # Chrome handles its own stale singleton files.
        except PermissionError as error:
            raise BrowserSessionError("browser_busy", "The dedicated Jupiter browser profile is still in use.") from error
        except (ValueError, IndexError, OSError):
            pass
    run = runner or subprocess.run
    try:
        result = run(["ps", "-axo", "command="], check=True, text=True,
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (OSError, subprocess.CalledProcessError) as error:
        # A restricted runner cannot prove this profile is idle. Do not attempt
        # a browser launch from an environment whose process check was denied.
        raise BrowserSessionError("browser_state_unavailable", "Could not verify that the dedicated Jupiter browser is closed.") from error
    escaped = re.escape(str(profile))
    profile_arg = re.compile(r'(?:^|\s)--user-data-dir(?:=|\s)(?:' + escaped +
                             r'|"' + escaped + r'"|\x27' + escaped + r'\x27)(?=\s--|$)')
    chrome_process = re.compile(r'/Contents/MacOS/(?:Google Chrome(?: Beta| Dev| Canary)?|Chromium)(?=\s|$)')
    for command in result.stdout.splitlines():
        if chrome_process.search(command) and profile_arg.search(command):
            raise BrowserSessionError("browser_busy", "The dedicated Jupiter login browser is still running; quit it before syncing.")


def native_profile_launch_options(default_args, harmful_args, locale="en-US"):
    """Keep collector cookie encryption compatible with normal visible Chrome.

    Scrapling and Playwright otherwise add different password/keychain switches
    from Chrome launched via macOS. Never change the visible browser to a mock
    keychain just to accommodate automation.
    """
    storage_flags = {"--password-store=basic", "--use-mock-keychain"}
    args = [arg for arg in default_args if arg not in storage_flags]
    if locale:
        base = locale.split("-")[0].lower()
        languages = f"{locale},{base}" if base != locale.lower() else locale
        args += [f"--lang={locale}", f"--accept-lang={languages}"]
    ignored = list(dict.fromkeys([*harmful_args, "--password-store=basic", "--use-mock-keychain"]))
    return {"args": args, "ignore_default_args": ignored}
