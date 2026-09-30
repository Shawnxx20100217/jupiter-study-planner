#!/usr/bin/env python3
"""One-time local TickTick authorization; no Codex credentials or model calls.

Personal API tokens are entered invisibly in the local terminal. OAuth is an
alternative for owners of a registered TickTick developer application.
"""
import argparse
import base64
from datetime import datetime, timedelta, timezone
import getpass
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import webbrowser

from ticktick_client import TickTickClient, TickTickError, atomic_json


API_BASE = "https://api.ticktick.com/open/v1"
AUTHORIZE_URL = "https://ticktick.com/oauth/authorize"
TOKEN_URL = "https://ticktick.com/oauth/token"
REDIRECT_URI = "http://127.0.0.1:8765/callback"
SCOPE = "tasks:read tasks:write"


class SetupError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    """Never forward a secret-bearing request through an HTTP redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def load_instance(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SetupError("无法读取本机实例配置。") from exc
    if not isinstance(value, dict):
        raise SetupError("本机实例配置格式无效。")
    nested = value.get("ticktick") if isinstance(value.get("ticktick"), dict) else {}
    base = value.get("ticktick_api_base", nested.get("api_base", API_BASE))
    if str(base).rstrip("/") != API_BASE:
        raise SetupError("此授权助手仅支持国际版 TickTick；不会把令牌发送到其他地址。")
    return value


def clean_token(token):
    if not isinstance(token, str):
        raise SetupError("令牌格式无效。")
    token = token.strip()
    if not token or len(token) > 8192 or any(c.isspace() for c in token):
        raise SetupError("令牌为空或包含空白字符，请重新复制。")
    return token


def select_project(projects, project_id=None, project_name="Jupiter 作业"):
    if not isinstance(projects, list):
        raise SetupError("TickTick 返回的清单格式无效。")
    candidates = [p for p in projects if isinstance(p, dict) and p.get("id") and not p.get("closed")]
    matches = [p for p in candidates if str(p["id"]) == str(project_id)] if project_id else [
        p for p in candidates if p.get("name") == project_name]
    if len(matches) != 1:
        raise SetupError("找不到唯一的目标清单；请先在 TickTick 创建清单，或使用 --project-id 指定已有清单。")
    return matches[0]


def validate_target(token, config, project_id=None, client=None):
    token = clean_token(token)
    client = client or TickTickClient(token, API_BASE, opener=build_opener(NoRedirect()).open)
    nested = config.get("ticktick") if isinstance(config.get("ticktick"), dict) else {}
    try:
        projects = client.list_projects()
    except TickTickError as exc:
        # API error bodies may contain request details; never print them here.
        raise SetupError("TickTick 令牌验证失败，请检查令牌、账号区域与网络。") from exc
    return select_project(projects, project_id or config.get("ticktick_project_id") or nested.get("project_id"),
                          config.get("ticktick_project_name") or nested.get("project_name") or "Jupiter 作业")


def private_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_authorization(instance_path, token, project, method, expires_in=None):
    """Called only after read-only token/project verification has succeeded."""
    from jupiter_runtime import AlreadyRunning, instance_config, run_lock
    try:
        path, directory, _report, _json = instance_config(instance_path)
        with run_lock(directory):
            return _save_authorization_locked(path, token, project, method, expires_in)
    except AlreadyRunning as exc:
        raise SetupError("正在同步，授权尚未保存；请稍后再次提交。") from exc


def _save_authorization_locked(instance_path, token, project, method, expires_in=None):
    path = Path(instance_path).expanduser().resolve()
    # Re-read immediately before saving to preserve unrelated recent settings.
    config = load_instance(path)
    token = clean_token(token)
    token_path = path.parent / "ticktick-token"
    now = datetime.now(timezone.utc)
    metadata = {"method": method, "authorized_at": now.isoformat(), "project_id": str(project["id"])}
    if isinstance(expires_in, int) and not isinstance(expires_in, bool) and expires_in > 0:
        metadata["expires_at"] = (now + timedelta(seconds=expires_in)).isoformat()
    private_text(token_path, token + "\n")
    atomic_json(path.parent / "ticktick-auth.json", metadata)
    config.update({"ticktick_enabled": True, "ticktick_api_base": API_BASE,
                   "ticktick_project_id": str(project["id"]), "ticktick_project_name": str(project.get("name", "Jupiter 作业")),
                   "ticktick_token_file": str(token_path)})
    atomic_json(path, config)
    return {"ok": True, "project_name": config["ticktick_project_name"],
            "message": "本机授权已保存，后续定时同步无需调用模型。"}


def local_form(path, nonce, message="", success=False):
    """No JavaScript, third-party resources, persisted form values, or secrets."""
    form = "" if success else f'''<form method="post" action="{escape(path, quote=True)}" autocomplete="off">
    <input type="hidden" name="csrf" value="{escape(nonce, quote=True)}">
    <label for="token">个人 API Token</label><input id="token" name="token" type="password" required
    autocomplete="off" spellcheck="false" autofocus placeholder="粘贴令牌，仅保存到本机">
    <button type="submit">连接自动同步</button></form>'''
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
    <title>连接 TickTick</title><style>
    *{{box-sizing:border-box}} body{{margin:0;background:#f5f5f7;color:#1d1d1f;font:15px -apple-system,BlinkMacSystemFont,sans-serif}}
    main{{max-width:480px;margin:10vh auto;padding:34px;background:white;border-radius:22px;box-shadow:0 12px 45px #00000009}}
    h1{{font-size:27px;letter-spacing:-.6px;margin:0 0 14px}}p{{line-height:1.7;color:#6e6e73}}a{{color:#007aff}}
    label{{display:block;font-size:13px;margin-top:26px}}input[type=password]{{width:100%;font:inherit;margin:9px 0 15px;padding:13px;border:1px solid #d2d2d7;border-radius:10px}}
    button{{width:100%;border:0;border-radius:10px;background:#007aff;color:white;font:inherit;font-weight:600;padding:14px;cursor:pointer}}
    .message{{color:{'#248a3d' if success else '#b84036'};font-weight:500}}small{{display:block;margin-top:24px;color:#86868b;line-height:1.6}}
    </style><main><h1>{'TickTick 已连接' if success else '连接 TickTick'}</h1>
    <p>{'可以关闭此页。之后由本机程序自动同步，提醒在 TickTick 中管理。' if success else '打开 TickTick 网页，进入头像 → Settings → Account → API Token，创建个人令牌。'}</p>
    {'' if success else '<p><a href="https://ticktick.com/webapp/" target="_blank" rel="noopener noreferrer">打开 TickTick ↗</a></p>'}
    <p class="message" role="status">{escape(message)}</p>{form}
    <small>令牌只保存在你的电脑，不经过聊天或模型。此页面仅在本机临时运行。</small></main></html>'''


def origin_allowed(request_origin, expected_origin):
    """Allow embedded same-host form posts, including an opaque ``null`` origin."""
    return not request_origin or request_origin == "null" or request_origin == expected_origin


def serve_local_form(instance_path, project_id=None, timeout=1200, browser_open=None, port=0):
    """The caller may open the printed one-time URL in its browser panel."""
    load_instance(instance_path)
    route = "/connect/" + secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send_page(self, status, message="", success=False):
            body = local_form(route, nonce, message, success).encode("utf-8")
            self.send_response(status)
            for key, value in {"Content-Type": "text/html; charset=utf-8", "Content-Length": str(len(body)),
                               "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                               "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                               "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"}.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def valid_route(self):
            return self.path == route and self.headers.get("Host") == authority

        def do_GET(self):
            if not self.valid_route():
                self.send_error(404)
                return
            self.send_page(200)

        def do_POST(self):
            # Some embedded browsers intentionally omit Origin for a loopback
            # form.  The per-page CSRF nonce below still protects the POST;
            # reject an explicit foreign Origin while accepting same-host
            # requests with no Origin header.
            request_origin = self.headers.get("Origin")
            if not self.valid_route() or not origin_allowed(request_origin, origin):
                self.send_error(403)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 1 or size > 16384 or self.headers.get_content_type() != "application/x-www-form-urlencoded":
                    raise SetupError("表单格式无效，请刷新页面。")
                values = parse_qs(self.rfile.read(size).decode("utf-8"), keep_blank_values=True)
                csrf = values.get("csrf", [])
                if len(csrf) != 1 or not secrets.compare_digest(csrf[0], nonce):
                    raise SetupError("页面已失效，请重新打开连接页。")
                tokens = values.get("token", [])
                if len(tokens) != 1:
                    raise SetupError("令牌格式无效。")
                token = clean_token(tokens[0])
                project = validate_target(token, load_instance(instance_path), project_id)
                connected = save_authorization(instance_path, token, project, "personal-token")
            except (SetupError, OSError, ValueError) as exc:
                message = str(exc) if isinstance(exc, SetupError) else "本机授权未保存，请稍后重试。"
                self.send_page(400, message)
                return
            self.send_page(200, "已连接清单：" + connected["project_name"], True)
            result.update(connected)

    with HTTPServer(("127.0.0.1", port), Handler) as server:
        server.timeout = 1
        authority = "127.0.0.1:" + str(server.server_address[1])
        origin = "http://" + authority
        url = origin + route
        print(json.dumps({"ok": True, "status": "waiting_for_token", "url": url}, ensure_ascii=False), flush=True)
        if browser_open:
            browser_open(url)
        deadline = time.monotonic() + timeout
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if not result:
        raise SetupError("连接页面已超时；本机配置没有启用。")
    return result


def authorization_url(client_id, state):
    return AUTHORIZE_URL + "?" + urlencode({"client_id": client_id, "scope": SCOPE, "state": state,
                                            "redirect_uri": REDIRECT_URI, "response_type": "code"})


def callback_code(target, state):
    """Validate a loopback request without logging or reflecting its code."""
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.path != "/callback":
        raise SetupError("回调地址不匹配。")
    values = parse_qs(parsed.query, keep_blank_values=True)
    states = values.get("state", [])
    if len(states) != 1 or not secrets.compare_digest(states[0], state):
        raise SetupError("授权校验失败，请重新授权。")
    if "error" in values:
        raise SetupError("TickTick 授权未完成。")
    codes = values.get("code", [])
    if len(codes) != 1 or not codes[0]:
        raise SetupError("授权回调缺少有效代码。")
    return codes[0]


def await_authorization(client_id, timeout=600, browser_open=webbrowser.open):
    state = secrets.token_urlsafe(32)
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # HTTP request lines contain the authorization code.

        def do_GET(self):
            try:
                if self.headers.get("Host") != "127.0.0.1:8765":
                    raise SetupError("回调主机不匹配。")
                code = callback_code(self.path, state)
            except SetupError:
                self.send_response(400)
                body = "Authorization could not be verified. Return to the terminal."
            else:
                result["code"] = code
                self.send_response(200)
                body = "Authorization received. Return to the terminal for verification."
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

    try:
        server = HTTPServer(("127.0.0.1", 8765), Handler)
    except OSError as exc:
        raise SetupError("本机授权端口 8765 正在使用，请关闭上次授权窗口后再试。") from exc
    with server:
        server.timeout = 1
        url = authorization_url(client_id, state)
        print("请在浏览器中完成 TickTick 授权。若浏览器未打开，请访问：\n" + url, flush=True)
        browser_open(url)
        deadline = time.monotonic() + timeout
        while "code" not in result and time.monotonic() < deadline:
            server.handle_request()
    if "code" not in result:
        raise SetupError("授权等待超时；本机配置没有启用。")
    return result["code"]


def exchange_code(client_id, client_secret, code, opener=None):
    credentials = base64.b64encode((client_id + ":" + client_secret).encode("utf-8")).decode("ascii")
    body = urlencode({"code": code, "grant_type": "authorization_code", "scope": SCOPE,
                      "redirect_uri": REDIRECT_URI}).encode("ascii")
    request = Request(TOKEN_URL, data=body, method="POST", headers={"Authorization": "Basic " + credentials,
                       "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    try:
        with (opener or build_opener(NoRedirect()).open)(request, timeout=20) as response:
            result = json.loads(response.read(65536).decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("invalid token response")
        token = clean_token(result.get("access_token"))
        if result.get("scope") and not {"tasks:read", "tasks:write"}.issubset(set(str(result["scope"]).split())):
            raise SetupError("授权缺少任务读写权限。")
        return token, result.get("expires_in")
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        raise SetupError("无法完成 TickTick 授权，请检查应用设置和网络后重试。") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--project-id", help="已有 TickTick 清单 ID；默认使用配置中的清单")
    parser.add_argument("--method", choices=("personal-token", "oauth"), default="personal-token")
    parser.add_argument("--client-id", help="OAuth 应用 Client ID；仅 OAuth 模式使用")
    parser.add_argument("--web", action="store_true", help="用一次性本机网页输入个人令牌；输出页面地址")
    args = parser.parse_args(argv)
    try:
        config = load_instance(args.instance)
        if args.web:
            if args.method != "personal-token":
                raise SetupError("本机网页用于个人令牌；OAuth 请使用终端方式。")
            result = serve_local_form(args.instance, args.project_id)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        expires_in = None
        if args.method == "oauth":
            client_id = args.client_id or input("TickTick Client ID: ").strip()
            if not client_id or ":" in client_id:
                raise SetupError("Client ID 格式无效。")
            secret = getpass.getpass("TickTick Client Secret（不会显示或保存）: ")
            if not secret:
                raise SetupError("Client Secret 不能为空。")
            code = await_authorization(client_id)
            token, expires_in = exchange_code(client_id, secret, code)
        else:
            print("在 TickTick 网页的 Settings → Account → API Token 创建个人令牌。\n请只粘贴到下方本机隐藏输入，不要发送到聊天中。")
            token = clean_token(getpass.getpass("TickTick API Token（不会显示）: "))
        project = validate_target(token, config, args.project_id)
        result = save_authorization(args.instance, token, project, args.method, expires_in)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (SetupError, OSError, EOFError, KeyboardInterrupt) as exc:
        message = str(exc) if isinstance(exc, SetupError) else "本机授权未完成；请重试。"
        print(json.dumps({"ok": False, "message": message}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
