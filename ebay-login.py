#!/usr/bin/env python3.14
"""Run the eBay OAuth login callback handler and store a refresh token."""

from __future__ import annotations

import argparse
import base64
import http.server
import json
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


DEFAULT_CALLBACK_PATH = "/callback"
DEFAULT_ENV_FILE = ".env"
DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_LISTEN_PORT = 8765
DEFAULT_LOGIN_SCOPE = (
    "https://api.ebay.com/oauth/api_scope "
    "https://api.ebay.com/oauth/api_scope/sell.analytics.readonly"
)
DEFAULT_LOGIN_TIMEOUT = 300.0
DEFAULT_REQUEST_TIMEOUT = 30.0


class EbayApiError(RuntimeError):
    pass


class EbayLoginError(RuntimeError):
    pass


@dataclass
class OAuthCallback:
    code: str | None = None
    state: str | None = None
    error: str | None = None
    error_description: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run an eBay OAuth callback server and store EBAY_REFRESH_TOKEN in .env.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(DEFAULT_ENV_FILE),
        help=f"Credential/config file. Defaults to {DEFAULT_ENV_FILE}.",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_LISTEN_HOST,
        help=f"Local listen host. Defaults to {DEFAULT_LISTEN_HOST}.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_LISTEN_PORT,
        help=f"Local OAuth callback port. Defaults to {DEFAULT_LISTEN_PORT}.",
    )
    parser.add_argument(
        "--callback-path",
        default=DEFAULT_CALLBACK_PATH,
        help=f"OAuth callback path. Defaults to {DEFAULT_CALLBACK_PATH}.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_LOGIN_TIMEOUT,
        help=f"Seconds to wait for the OAuth redirect. Defaults to {int(DEFAULT_LOGIN_TIMEOUT)}.",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=DEFAULT_REQUEST_TIMEOUT,
        help=f"eBay API request timeout in seconds. Defaults to {int(DEFAULT_REQUEST_TIMEOUT)}.",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Listen with plain HTTP instead of HTTPS. Use only behind a TLS-terminating proxy.",
    )
    parser.add_argument(
        "--cert-file",
        type=Path,
        help="TLS certificate chain file. Defaults to certs/<LETSENCRYPT_DOMAIN>/fullchain.pem.",
    )
    parser.add_argument(
        "--key-file",
        type=Path,
        help="TLS private key file. Defaults to certs/<LETSENCRYPT_DOMAIN>/privkey.pem.",
    )
    parser.add_argument(
        "--domain",
        help="Public domain for default TLS cert lookup. Defaults to LETSENCRYPT_DOMAIN in .env.",
    )
    parser.add_argument(
        "--public-url",
        help="Auth Accepted URL configured in eBay. Defaults to EBAY_OAUTH_ACCEPTED_URL or https://<domain><path>.",
    )
    parser.add_argument(
        "--runame",
        help="eBay Redirect URL name (RuName). Defaults to EBAY_RUNAME in .env.",
    )
    parser.add_argument(
        "--scopes",
        help="Space-separated OAuth scopes. Defaults to EBAY_OAUTH_SCOPES or eBay's base OAuth scope.",
    )
    parser.add_argument(
        "--sandbox",
        action="store_true",
        help="Use eBay Sandbox OAuth endpoints.",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Print the eBay sign-in URL and prompt for the redirected URL instead of running a server.",
    )
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than 0")
    if args.request_timeout <= 0:
        parser.error("--request-timeout must be greater than 0")
    return args


def valid_env_key(key: str) -> bool:
    if not key or not (key[0].isalpha() or key[0] == "_"):
        return False
    return all(char.isalnum() or char == "_" for char in key)


def parse_dotenv_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise EbayLoginError(f"Could not read {path}: {exc}") from exc

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            raise EbayLoginError(f"{path}:{line_number}: expected KEY=VALUE")

        key, value = line.split("=", 1)
        key = key.strip()
        if not valid_env_key(key):
            raise EbayLoginError(f"{path}:{line_number}: invalid key {key!r}")
        values[key] = parse_dotenv_value(value)

    return values


def config_value(config: dict[str, str], key: str, default: str | None = None) -> str | None:
    value = config.get(key)
    if value is None or value == "":
        return default
    return value


def authorization_endpoint(sandbox: bool) -> str:
    return (
        "https://auth.sandbox.ebay.com/oauth2/authorize"
        if sandbox
        else "https://auth.ebay.com/oauth2/authorize"
    )


def token_endpoint(sandbox: bool) -> str:
    return (
        "https://api.sandbox.ebay.com/identity/v1/oauth2/token"
        if sandbox
        else "https://api.ebay.com/identity/v1/oauth2/token"
    )


def normalize_callback_path(path: str) -> str:
    if not path:
        return DEFAULT_CALLBACK_PATH
    return path if path.startswith("/") else f"/{path}"


def build_authorization_url(
    client_id: str,
    runame: str,
    scopes: str,
    state: str,
    sandbox: bool,
) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": runame,
        "response_type": "code",
        "scope": scopes,
        "state": state,
        "prompt": "login",
    }
    return authorization_endpoint(sandbox) + "?" + urllib.parse.urlencode(
        params,
        quote_via=urllib.parse.quote,
    )


def public_callback_url(
    config: dict[str, str],
    args: argparse.Namespace,
    callback_path: str,
) -> str:
    configured = args.public_url or config_value(config, "EBAY_OAUTH_ACCEPTED_URL")
    if configured:
        return configured

    domain = args.domain or config_value(config, "LETSENCRYPT_DOMAIN")
    if domain:
        return f"https://{domain}{callback_path}"
    return f"https://<your-domain>{callback_path}"


def listen_url(args: argparse.Namespace, callback_path: str) -> str:
    scheme = "http" if args.http else "https"
    host = "localhost" if args.host in {"", "0.0.0.0", "::"} else args.host
    return f"{scheme}://{host}:{args.port}{callback_path}"


def default_cert_paths(
    config: dict[str, str],
    args: argparse.Namespace,
) -> tuple[Path, Path]:
    cert_file = args.cert_file
    key_file = args.key_file
    if cert_file and key_file:
        return cert_file, key_file

    domain = args.domain or config_value(config, "LETSENCRYPT_DOMAIN")
    if not domain:
        raise EbayLoginError(
            "Missing LETSENCRYPT_DOMAIN. Set it in .env, pass --domain, or pass --cert-file and --key-file."
        )

    cert_root = Path(config_value(config, "LETSENCRYPT_CERT_ROOT", "certs") or "certs")
    default_cert_file = cert_root / domain / "fullchain.pem"
    default_key_file = cert_root / domain / "privkey.pem"
    return cert_file or default_cert_file, key_file or default_key_file


def send_callback_response(
    handler: http.server.BaseHTTPRequestHandler,
    status: int,
    title: str,
    message: str,
) -> None:
    body = (
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>"
        + title
        + "</title></head><body><h1>"
        + title
        + "</h1><p>"
        + message
        + "</p></body></html>"
    ).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def first_query_value(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    value = values[0]
    return value or None


def wait_for_oauth_callback(
    config: dict[str, str],
    args: argparse.Namespace,
    callback_path: str,
) -> OAuthCallback:
    result = OAuthCallback()

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path != callback_path:
                send_callback_response(
                    self,
                    404,
                    "Unknown OAuth callback",
                    "This server is only listening for the configured eBay callback path.",
                )
                return

            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            result.code = first_query_value(query, "code")
            result.state = first_query_value(query, "state")
            result.error = first_query_value(query, "error")
            result.error_description = first_query_value(query, "error_description")

            if result.error:
                send_callback_response(
                    self,
                    400,
                    "eBay login declined",
                    "The OAuth flow returned an error. You can close this tab and return to the terminal.",
                )
                return

            send_callback_response(
                self,
                200,
                "eBay login complete",
                "The authorization code was received. You can close this tab and return to the terminal.",
            )

        def log_message(self, format: str, *args: object) -> None:
            return

    try:
        server = http.server.ThreadingHTTPServer((args.host, args.port), CallbackHandler)
    except OSError as exc:
        raise EbayLoginError(f"Could not start callback server on {listen_url(args, callback_path)}: {exc}") from exc

    server.daemon_threads = True
    with server:
        if not args.http:
            cert_file, key_file = default_cert_paths(config, args)
            if not cert_file.exists():
                raise EbayLoginError(f"TLS certificate file not found: {cert_file}")
            if not key_file.exists():
                raise EbayLoginError(f"TLS private key file not found: {key_file}")
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
            server.socket = context.wrap_socket(server.socket, server_side=True)

        server.timeout = 1.0
        deadline = time.monotonic() + args.timeout
        print(f"Listening on {listen_url(args, callback_path)}")
        while not any((result.code, result.error)) and time.monotonic() < deadline:
            server.handle_request()

    if not any((result.code, result.error)):
        raise EbayLoginError(f"Timed out waiting for eBay to redirect to {public_callback_url(config, args, callback_path)}.")
    return result


def parse_oauth_callback_value(value: str) -> OAuthCallback:
    value = value.strip()
    if not value:
        raise EbayLoginError("No OAuth callback URL or authorization code was entered.")

    if "://" not in value and "?" not in value:
        return OAuthCallback(code=value)

    parsed = urllib.parse.urlsplit(value)
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    callback = OAuthCallback(
        code=first_query_value(query, "code"),
        state=first_query_value(query, "state"),
        error=first_query_value(query, "error"),
        error_description=first_query_value(query, "error_description"),
    )
    if not any((callback.code, callback.error)):
        raise EbayLoginError("The pasted OAuth callback URL did not include a code or error.")
    return callback


def wait_for_manual_oauth_callback(expected_state: str) -> OAuthCallback:
    print("After eBay redirects, paste the full redirected URL here.")
    print("Pasting only the code also works, but the state check will be skipped.")
    try:
        value = input("Redirected URL or code: ")
    except EOFError as exc:
        raise EbayLoginError("No OAuth callback URL or authorization code was entered.") from exc
    callback = parse_oauth_callback_value(value)
    if callback.state is None and callback.code:
        print("Warning: no state was pasted, so the OAuth state check was skipped.", file=sys.stderr)
    elif callback.state != expected_state:
        raise EbayLoginError("OAuth callback state did not match. The refresh token was not stored.")
    return callback


def oauth_basic_auth_header(client_id: str, client_secret: str) -> str:
    credentials = f"{client_id}:{client_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(credentials).decode("ascii")


def request_oauth_token(
    data: dict[str, str],
    client_id: str,
    client_secret: str,
    sandbox: bool,
    timeout: float,
    action: str,
) -> dict[str, object]:
    request = urllib.request.Request(
        token_endpoint(sandbox),
        data=urllib.parse.urlencode(data).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": oauth_basic_auth_header(client_id, client_secret),
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise EbayApiError(f"{action} failed with HTTP {exc.code}: {response_body}") from exc
    except urllib.error.URLError as exc:
        raise EbayApiError(f"{action} failed: {exc.reason}") from exc


def exchange_authorization_code(
    code: str,
    runame: str,
    client_id: str,
    client_secret: str,
    sandbox: bool,
    timeout: float,
) -> dict[str, object]:
    return request_oauth_token(
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": runame,
        },
        client_id=client_id,
        client_secret=client_secret,
        sandbox=sandbox,
        timeout=timeout,
        action="OAuth authorization-code exchange",
    )


def dotenv_value(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("dotenv values cannot contain newlines")
    return value


def update_dotenv(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    updated_lines: list[str] = []

    for raw_line in lines:
        stripped = raw_line.strip()
        line = stripped
        export_prefix = ""
        if not line or line.startswith("#"):
            updated_lines.append(raw_line)
            continue
        if line.startswith("export "):
            export_prefix = "export "
            line = line[len("export ") :].strip()
        if "=" not in line:
            updated_lines.append(raw_line)
            continue

        key, _value = line.split("=", 1)
        key = key.strip()
        if key not in updates:
            updated_lines.append(raw_line)
            continue

        seen.add(key)
        updated_lines.append(f"{export_prefix}{key}={dotenv_value(updates[key])}")

    missing = [key for key in updates if key not in seen]
    if missing and updated_lines and updated_lines[-1] != "":
        updated_lines.append("")
    for key in missing:
        updated_lines.append(f"{key}={dotenv_value(updates[key])}")

    path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")


def required_value(
    config: dict[str, str],
    key: str,
    description: str,
    override: str | None = None,
) -> str:
    value = override or config_value(config, key)
    if not value:
        raise EbayLoginError(f"Missing {description}. Set {key} in .env and rerun this script.")
    return value


def run_login(config: dict[str, str], config_path: Path, args: argparse.Namespace) -> None:
    callback_path = normalize_callback_path(args.callback_path)
    sandbox = args.sandbox or config_value(config, "EBAY_ENV", "").lower() == "sandbox"
    client_id = required_value(config, "EBAY_CLIENT_ID", "eBay Client ID")
    client_secret = required_value(config, "EBAY_CLIENT_SECRET", "eBay Client Secret")
    runame = required_value(config, "EBAY_RUNAME", "eBay RuName", args.runame)
    scopes = args.scopes or config_value(config, "EBAY_OAUTH_SCOPES", DEFAULT_LOGIN_SCOPE)
    if not scopes:
        scopes = DEFAULT_LOGIN_SCOPE

    state = secrets.token_urlsafe(32)
    login_url = build_authorization_url(
        client_id=client_id,
        runame=runame,
        scopes=scopes,
        state=state,
        sandbox=sandbox,
    )

    print("Configure eBay's Auth Accepted URL to this HTTPS URL:")
    print(public_callback_url(config, args, callback_path))
    if not args.http and not args.manual:
        print(f"Forward external port 443 to this host's port {args.port} while this script is running.")
    print()
    print("Open this eBay login URL, sign in as the seller account, and approve access:")
    print(login_url)
    print()

    if args.manual:
        callback = wait_for_manual_oauth_callback(state)
    else:
        callback = wait_for_oauth_callback(config, args, callback_path)

    if callback.error:
        detail = f": {callback.error_description}" if callback.error_description else ""
        raise EbayLoginError(f"eBay login failed: {callback.error}{detail}")
    if callback.state is not None and callback.state != state:
        raise EbayLoginError("OAuth callback state did not match. The refresh token was not stored.")
    if not callback.code:
        raise EbayLoginError("OAuth callback did not include an authorization code.")

    payload = exchange_authorization_code(
        code=callback.code,
        runame=runame,
        client_id=client_id,
        client_secret=client_secret,
        sandbox=sandbox,
        timeout=args.request_timeout,
    )
    refresh_token = payload.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise EbayApiError(f"OAuth token response did not include refresh_token: {payload}")

    updates = {
        "EBAY_REFRESH_TOKEN": refresh_token,
        "EBAY_OAUTH_ACCESS_TOKEN": "",
    }
    if args.runame:
        updates["EBAY_RUNAME"] = runame
    if args.scopes:
        updates["EBAY_OAUTH_SCOPES"] = scopes
    update_dotenv(config_path, updates)
    print(f"Stored EBAY_REFRESH_TOKEN in {config_path}.")


def main() -> int:
    args = parse_args()
    try:
        config = load_dotenv(args.env_file)
        run_login(config, args.env_file, args)
    except (EbayApiError, EbayLoginError, OSError, ValueError, ssl.SSLError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
