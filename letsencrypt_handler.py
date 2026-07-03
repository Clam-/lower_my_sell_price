#!/usr/bin/env python3.14
"""Temporary Let's Encrypt HTTP-01 handler and certificate fetcher."""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import Any


DEFAULT_CERT_ROOT = "certs"
DEFAULT_DIRECTORY_PRODUCTION = "https://acme-v02.api.letsencrypt.org/directory"
DEFAULT_DIRECTORY_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"
DEFAULT_ENV_FILE = ".env"
DEFAULT_HTTP_HOST = "0.0.0.0"
DEFAULT_HTTP_PORT = 8081
DEFAULT_KEY_BITS = 2048
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_TIMEOUT = 300.0
USER_AGENT = "lower-my-sell-price-acme/1.0"


class AcmeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    domain: str
    email: str
    agree_tos: bool
    directory_url: str
    host: str
    port: int
    cert_root: Path
    key_bits: int
    timeout: float
    poll_interval: float

    @property
    def cert_dir(self) -> Path:
        return self.cert_root / self.domain


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Obtain a Let's Encrypt certificate using a temporary HTTP-01 server on port 8081.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(DEFAULT_ENV_FILE),
        help=f"Config file. Defaults to {DEFAULT_ENV_FILE}.",
    )
    parser.add_argument("--domain", help="Domain name to certify. Defaults to LETSENCRYPT_DOMAIN.")
    parser.add_argument("--email", help="Let's Encrypt account contact email. Defaults to LETSENCRYPT_EMAIL.")
    parser.add_argument(
        "--host",
        default=None,
        help=f"Local challenge listen host. Defaults to {DEFAULT_HTTP_HOST}.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"Local challenge listen port. Defaults to {DEFAULT_HTTP_PORT}.",
    )
    parser.add_argument(
        "--cert-root",
        type=Path,
        default=None,
        help=f"Directory for certificate material. Defaults to {DEFAULT_CERT_ROOT}.",
    )
    parser.add_argument(
        "--key-bits",
        type=int,
        default=None,
        help=f"RSA key size for generated keys. Defaults to {DEFAULT_KEY_BITS}.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help=f"HTTP timeout in seconds. Defaults to {int(DEFAULT_TIMEOUT)}.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=None,
        help=f"ACME polling interval in seconds. Defaults to {int(DEFAULT_POLL_INTERVAL)}.",
    )
    parser.add_argument(
        "--directory-url",
        help="Override the ACME directory URL. Defaults to Let's Encrypt production or staging.",
    )
    parser.add_argument(
        "--agree-tos",
        action="store_true",
        help="Agree to the Let's Encrypt Subscriber Agreement.",
    )
    environment = parser.add_mutually_exclusive_group()
    environment.add_argument(
        "--staging",
        action="store_true",
        help="Use the Let's Encrypt staging directory.",
    )
    environment.add_argument(
        "--production",
        action="store_true",
        help="Use the Let's Encrypt production directory.",
    )
    return parser.parse_args()


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
        raise AcmeError(f"Could not read {path}: {exc}") from exc

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            raise AcmeError(f"{path}:{line_number}: expected KEY=VALUE")

        key, value = line.split("=", 1)
        key = key.strip()
        if not valid_env_key(key):
            raise AcmeError(f"{path}:{line_number}: invalid key {key!r}")
        values[key] = parse_dotenv_value(value)

    return values


def config_value(config: dict[str, str], key: str, default: str | None = None) -> str | None:
    value = config.get(key)
    if value is None or value == "":
        return default
    return value


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise AcmeError(f"Expected a boolean value, got {value!r}")


def parse_int_setting(value: str | None, default: int, name: str) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise AcmeError(f"{name} must be an integer") from exc


def parse_float_setting(value: str | None, default: float, name: str) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise AcmeError(f"{name} must be a number") from exc


def validate_domain(domain: str | None) -> str:
    if not domain:
        raise AcmeError("Missing LETSENCRYPT_DOMAIN. Set it in .env or pass --domain.")
    normalized = domain.strip().lower().rstrip(".")
    if not normalized:
        raise AcmeError("LETSENCRYPT_DOMAIN cannot be blank.")
    if normalized.startswith("*."):
        raise AcmeError("HTTP-01 cannot issue wildcard certificates.")
    if "://" in normalized or "/" in normalized or ":" in normalized:
        raise AcmeError("LETSENCRYPT_DOMAIN must be a domain name, not a URL.")
    return normalized


def build_settings(config: dict[str, str], args: argparse.Namespace) -> Settings:
    domain = validate_domain(args.domain or config_value(config, "LETSENCRYPT_DOMAIN"))
    email = args.email or config_value(config, "LETSENCRYPT_EMAIL")
    if not email:
        raise AcmeError("Missing LETSENCRYPT_EMAIL. Set it in .env or pass --email.")

    if args.production:
        staging = False
    elif args.staging:
        staging = True
    else:
        staging = parse_bool(config_value(config, "LETSENCRYPT_STAGING"), default=False)

    directory_url = (
        args.directory_url
        or config_value(config, "LETSENCRYPT_DIRECTORY_URL")
        or (DEFAULT_DIRECTORY_STAGING if staging else DEFAULT_DIRECTORY_PRODUCTION)
    )
    agree_tos = args.agree_tos or parse_bool(config_value(config, "LETSENCRYPT_AGREE_TOS"), default=False)
    if not agree_tos:
        raise AcmeError("Set LETSENCRYPT_AGREE_TOS=true or pass --agree-tos after reading Let's Encrypt's terms.")

    port = args.port
    if port is None:
        port = parse_int_setting(config_value(config, "LETSENCRYPT_HTTP_PORT"), DEFAULT_HTTP_PORT, "LETSENCRYPT_HTTP_PORT")
    if not 1 <= port <= 65535:
        raise AcmeError("LETSENCRYPT_HTTP_PORT must be between 1 and 65535.")

    key_bits = args.key_bits
    if key_bits is None:
        key_bits = parse_int_setting(config_value(config, "LETSENCRYPT_KEY_BITS"), DEFAULT_KEY_BITS, "LETSENCRYPT_KEY_BITS")
    if key_bits < 2048:
        raise AcmeError("LETSENCRYPT_KEY_BITS must be at least 2048.")

    timeout = args.timeout
    if timeout is None:
        timeout = parse_float_setting(config_value(config, "LETSENCRYPT_TIMEOUT"), DEFAULT_TIMEOUT, "LETSENCRYPT_TIMEOUT")
    if timeout <= 0:
        raise AcmeError("LETSENCRYPT_TIMEOUT must be greater than 0.")

    poll_interval = args.poll_interval
    if poll_interval is None:
        poll_interval = parse_float_setting(
            config_value(config, "LETSENCRYPT_POLL_INTERVAL"),
            DEFAULT_POLL_INTERVAL,
            "LETSENCRYPT_POLL_INTERVAL",
        )
    if poll_interval <= 0:
        raise AcmeError("LETSENCRYPT_POLL_INTERVAL must be greater than 0.")

    return Settings(
        domain=domain,
        email=email,
        agree_tos=agree_tos,
        directory_url=directory_url,
        host=args.host or config_value(config, "LETSENCRYPT_HTTP_HOST", DEFAULT_HTTP_HOST) or DEFAULT_HTTP_HOST,
        port=port,
        cert_root=args.cert_root or Path(config_value(config, "LETSENCRYPT_CERT_ROOT", DEFAULT_CERT_ROOT) or DEFAULT_CERT_ROOT),
        key_bits=key_bits,
        timeout=timeout,
        poll_interval=poll_interval,
    )


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def run_openssl_text(args: list[str]) -> str:
    try:
        completed = subprocess.run(
            ["openssl", *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise AcmeError("openssl was not found on PATH.") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise AcmeError(f"openssl {' '.join(args)} failed: {detail}") from exc
    return completed.stdout


def run_openssl_bytes(args: list[str], input_data: bytes | None = None) -> bytes:
    try:
        completed = subprocess.run(
            ["openssl", *args],
            input=input_data,
            check=True,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise AcmeError("openssl was not found on PATH.") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or b"").decode("utf-8", errors="replace").strip()
        raise AcmeError(f"openssl {' '.join(args)} failed: {detail}") from exc
    return completed.stdout


def ensure_rsa_key(path: Path, bits: int) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    run_openssl_text(["genrsa", "-out", str(path), str(bits)])
    path.chmod(0o600)


def rsa_public_jwk(private_key: Path) -> dict[str, str]:
    modulus_output = run_openssl_text(["rsa", "-in", str(private_key), "-noout", "-modulus"])
    try:
        _label, modulus_hex = modulus_output.strip().split("=", 1)
    except ValueError as exc:
        raise AcmeError("Could not read RSA modulus from account key.") from exc

    key_text = run_openssl_text(["rsa", "-in", str(private_key), "-noout", "-text"])
    exponent_match = re.search(r"publicExponent:\s*(\d+)", key_text)
    if not exponent_match:
        raise AcmeError("Could not read RSA exponent from account key.")

    modulus_hex = modulus_hex.strip()
    while len(modulus_hex) > 2 and modulus_hex.startswith("00"):
        modulus_hex = modulus_hex[2:]
    exponent = int(exponent_match.group(1))
    exponent_bytes = exponent.to_bytes((exponent.bit_length() + 7) // 8, "big")
    return {
        "e": b64url(exponent_bytes),
        "kty": "RSA",
        "n": b64url(bytes.fromhex(modulus_hex)),
    }


def jwk_thumbprint(jwk: dict[str, str]) -> str:
    payload = json_bytes({"e": jwk["e"], "kty": "RSA", "n": jwk["n"]})
    return b64url(hashlib.sha256(payload).digest())


def sign_rs256(private_key: Path, message: bytes) -> str:
    signature = run_openssl_bytes(["dgst", "-sha256", "-sign", str(private_key)], input_data=message)
    return b64url(signature)


def acme_error_message(status: int, body: bytes) -> str:
    if not body:
        return f"ACME request failed with HTTP {status}."
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        return f"ACME request failed with HTTP {status}: {body[:1000].decode('utf-8', errors='replace')}"
    detail = payload.get("detail") or payload.get("title") or payload
    problem_type = payload.get("type")
    if problem_type:
        return f"ACME request failed with HTTP {status}: {problem_type}: {detail}"
    return f"ACME request failed with HTTP {status}: {detail}"


class AcmeClient:
    def __init__(self, directory_url: str, account_key: Path, timeout: float) -> None:
        self.directory_url = directory_url
        self.account_key = account_key
        self.timeout = timeout
        self.directory: dict[str, str] = {}
        self.jwk = rsa_public_jwk(account_key)
        self.kid: str | None = None
        self.nonce: str | None = None

    def load_directory(self) -> None:
        request = urllib.request.Request(
            self.directory_url,
            headers={"User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                self.directory = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read()
            raise AcmeError(acme_error_message(exc.code, body)) from exc
        except urllib.error.URLError as exc:
            raise AcmeError(f"Could not load ACME directory: {exc.reason}") from exc

    def remember_nonce(self, headers: Message) -> None:
        nonce = headers.get("Replay-Nonce")
        if nonce:
            self.nonce = nonce

    def get_nonce(self) -> str:
        if self.nonce:
            nonce = self.nonce
            self.nonce = None
            return nonce

        new_nonce_url = self.directory.get("newNonce")
        if not new_nonce_url:
            raise AcmeError("ACME directory did not include newNonce.")

        request = urllib.request.Request(
            new_nonce_url,
            method="HEAD",
            headers={"User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                nonce = response.headers.get("Replay-Nonce")
        except urllib.error.HTTPError as exc:
            body = exc.read()
            self.remember_nonce(exc.headers)
            raise AcmeError(acme_error_message(exc.code, body)) from exc
        except urllib.error.URLError as exc:
            raise AcmeError(f"Could not fetch ACME nonce: {exc.reason}") from exc
        if not nonce:
            raise AcmeError("ACME server did not return a nonce.")
        return nonce

    def jws_body(self, url: str, payload: dict[str, Any] | None) -> bytes:
        protected: dict[str, Any] = {
            "alg": "RS256",
            "nonce": self.get_nonce(),
            "url": url,
        }
        if self.kid:
            protected["kid"] = self.kid
        else:
            protected["jwk"] = self.jwk

        protected_b64 = b64url(json_bytes(protected))
        payload_b64 = "" if payload is None else b64url(json_bytes(payload))
        signing_input = f"{protected_b64}.{payload_b64}".encode("ascii")
        return json_bytes(
            {
                "protected": protected_b64,
                "payload": payload_b64,
                "signature": sign_rs256(self.account_key, signing_input),
            }
        )

    def post(self, url: str, payload: dict[str, Any] | None) -> tuple[int, Message, bytes]:
        request = urllib.request.Request(
            url,
            data=self.jws_body(url, payload),
            method="POST",
            headers={
                "Content-Type": "application/jose+json",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                self.remember_nonce(response.headers)
                return response.status, response.headers, body
        except urllib.error.HTTPError as exc:
            body = exc.read()
            self.remember_nonce(exc.headers)
            raise AcmeError(acme_error_message(exc.code, body)) from exc
        except urllib.error.URLError as exc:
            raise AcmeError(f"ACME request failed: {exc.reason}") from exc

    def post_json(self, url: str, payload: dict[str, Any] | None) -> tuple[dict[str, Any], Message]:
        _status, headers, body = self.post(url, payload)
        if not body:
            return {}, headers
        try:
            return json.loads(body.decode("utf-8")), headers
        except json.JSONDecodeError as exc:
            raise AcmeError(f"ACME server returned invalid JSON from {url}.") from exc

    def new_account(self, email: str, agree_tos: bool) -> None:
        new_account_url = self.directory.get("newAccount")
        if not new_account_url:
            raise AcmeError("ACME directory did not include newAccount.")

        payload: dict[str, Any] = {"termsOfServiceAgreed": agree_tos}
        if email:
            payload["contact"] = [f"mailto:{email}"]

        _body, headers = self.post_json(new_account_url, payload)
        location = headers.get("Location")
        if not location:
            raise AcmeError("ACME account response did not include an account URL.")
        self.kid = location

    def new_order(self, domain: str) -> tuple[dict[str, Any], str]:
        new_order_url = self.directory.get("newOrder")
        if not new_order_url:
            raise AcmeError("ACME directory did not include newOrder.")
        body, headers = self.post_json(
            new_order_url,
            {"identifiers": [{"type": "dns", "value": domain}]},
        )
        location = headers.get("Location")
        if not location:
            raise AcmeError("ACME order response did not include an order URL.")
        return body, location


def make_challenge_handler(challenges: dict[str, str]) -> type[http.server.BaseHTTPRequestHandler]:
    class ChallengeHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            prefix = "/.well-known/acme-challenge/"
            path = self.path.split("?", 1)[0]
            if path in {"/", "/healthz"}:
                self.send_text(200, "ACME HTTP-01 handler is running.\n")
                return
            if not path.startswith(prefix):
                self.send_text(404, "not found\n")
                return

            token = path[len(prefix) :]
            key_authorization = challenges.get(token)
            if not key_authorization:
                self.send_text(404, "unknown challenge token\n")
                return
            self.send_text(200, key_authorization)

        def send_text(self, status: int, body_text: str) -> None:
            body = body_text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            print(f"{self.client_address[0]} - {format % args}", file=sys.stderr)

    return ChallengeHandler


def start_challenge_server(
    host: str,
    port: int,
    challenges: dict[str, str],
) -> tuple[http.server.ThreadingHTTPServer, threading.Thread]:
    server = http.server.ThreadingHTTPServer((host, port), make_challenge_handler(challenges))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def challenge_error(challenge: dict[str, Any]) -> str:
    error = challenge.get("error")
    if isinstance(error, dict):
        return str(error.get("detail") or error.get("type") or error)
    return str(error or challenge)


def poll_challenge(
    client: AcmeClient,
    challenge_url: str,
    deadline: float,
    poll_interval: float,
) -> None:
    while time.monotonic() < deadline:
        challenge, _headers = client.post_json(challenge_url, None)
        status = challenge.get("status")
        if status == "valid":
            return
        if status == "invalid":
            raise AcmeError(f"ACME challenge failed: {challenge_error(challenge)}")
        time.sleep(poll_interval)
    raise AcmeError("Timed out waiting for ACME challenge validation.")


def poll_order(
    client: AcmeClient,
    order_url: str,
    deadline: float,
    poll_interval: float,
) -> dict[str, Any]:
    while time.monotonic() < deadline:
        order, _headers = client.post_json(order_url, None)
        status = order.get("status")
        if status == "valid" and order.get("certificate"):
            return order
        if status == "invalid":
            raise AcmeError(f"ACME order failed: {order}")
        time.sleep(poll_interval)
    raise AcmeError("Timed out waiting for ACME order finalization.")


def csr_config(domain: str) -> str:
    return f"""[req]
prompt = no
distinguished_name = req_distinguished_name
req_extensions = v3_req

[req_distinguished_name]
CN = {domain}

[v3_req]
subjectAltName = @alt_names

[alt_names]
DNS.1 = {domain}
"""


def create_csr_der(domain: str, private_key: Path, cert_dir: Path) -> bytes:
    config_path = cert_dir / "csr.conf"
    csr_path = cert_dir / "request.csr"
    config_path.write_text(csr_config(domain), encoding="utf-8")
    run_openssl_text(
        [
            "req",
            "-new",
            "-sha256",
            "-key",
            str(private_key),
            "-out",
            str(csr_path),
            "-config",
            str(config_path),
        ]
    )
    return run_openssl_bytes(["req", "-in", str(csr_path), "-outform", "DER"])


def write_certificate_files(cert_dir: Path, certificate_pem: bytes) -> None:
    fullchain_path = cert_dir / "fullchain.pem"
    cert_path = cert_dir / "cert.pem"
    chain_path = cert_dir / "chain.pem"
    fullchain_path.write_bytes(certificate_pem)

    marker = b"-----END CERTIFICATE-----"
    first_end = certificate_pem.find(marker)
    if first_end == -1:
        raise AcmeError("Downloaded certificate did not look like PEM.")
    first_end += len(marker)
    cert_path.write_bytes(certificate_pem[:first_end] + b"\n")
    chain_path.write_bytes(certificate_pem[first_end:].lstrip())


def select_http01_challenge(authorization: dict[str, Any]) -> dict[str, Any] | None:
    for challenge in authorization.get("challenges", []):
        if challenge.get("type") == "http-01":
            return challenge
    return None


def issue_certificate(settings: Settings) -> None:
    settings.cert_dir.mkdir(parents=True, exist_ok=True)
    account_key = settings.cert_dir / "account.key"
    private_key = settings.cert_dir / "privkey.pem"
    ensure_rsa_key(account_key, settings.key_bits)
    ensure_rsa_key(private_key, settings.key_bits)

    print(f"Using ACME directory: {settings.directory_url}")
    print(f"Writing certificate files under {settings.cert_dir}")

    client = AcmeClient(settings.directory_url, account_key, settings.timeout)
    client.load_directory()
    client.new_account(settings.email, settings.agree_tos)

    order, order_url = client.new_order(settings.domain)
    authorization_urls = order.get("authorizations")
    if not isinstance(authorization_urls, list) or not authorization_urls:
        raise AcmeError("ACME order did not include authorization URLs.")

    thumbprint = jwk_thumbprint(client.jwk)
    challenge_tokens: dict[str, str] = {}
    pending_challenge_urls: list[str] = []

    for authorization_url in authorization_urls:
        authorization, _headers = client.post_json(str(authorization_url), None)
        if authorization.get("status") == "valid":
            continue

        challenge = select_http01_challenge(authorization)
        if not challenge:
            raise AcmeError("ACME authorization did not include an http-01 challenge.")
        token = challenge.get("token")
        challenge_url = challenge.get("url")
        if not isinstance(token, str) or not isinstance(challenge_url, str):
            raise AcmeError("ACME http-01 challenge was missing token or URL.")
        challenge_tokens[token] = f"{token}.{thumbprint}"
        pending_challenge_urls.append(challenge_url)

    server: http.server.ThreadingHTTPServer | None = None
    thread: threading.Thread | None = None
    try:
        if challenge_tokens:
            server, thread = start_challenge_server(settings.host, settings.port, challenge_tokens)
            print(f"Serving HTTP-01 challenges on http://{settings.host}:{settings.port}")
            print("External port 80 must forward to this host and port while validation runs.")

            for challenge_url in pending_challenge_urls:
                client.post_json(challenge_url, {})
            deadline = time.monotonic() + settings.timeout
            for challenge_url in pending_challenge_urls:
                poll_challenge(client, challenge_url, deadline, settings.poll_interval)
        else:
            print("ACME authorization was already valid; no HTTP-01 request was needed.")
    finally:
        if server:
            server.shutdown()
            server.server_close()
        if thread:
            thread.join(timeout=5)

    csr_der = create_csr_der(settings.domain, private_key, settings.cert_dir)
    finalize_url = order.get("finalize")
    if not isinstance(finalize_url, str):
        raise AcmeError("ACME order did not include a finalize URL.")

    finalized_order, _headers = client.post_json(finalize_url, {"csr": b64url(csr_der)})
    if finalized_order.get("status") == "valid" and finalized_order.get("certificate"):
        final_order = finalized_order
    else:
        final_order = poll_order(
            client,
            order_url,
            time.monotonic() + settings.timeout,
            settings.poll_interval,
        )

    certificate_url = final_order.get("certificate")
    if not isinstance(certificate_url, str):
        raise AcmeError("ACME order completed without a certificate URL.")

    _status, _headers, certificate_pem = client.post(certificate_url, None)
    write_certificate_files(settings.cert_dir, certificate_pem)
    print(f"Saved certificate chain: {settings.cert_dir / 'fullchain.pem'}")
    print(f"Saved private key: {settings.cert_dir / 'privkey.pem'}")


def main() -> int:
    args = parse_args()
    try:
        config = load_dotenv(args.env_file)
        settings = build_settings(config, args)
        issue_certificate(settings)
    except (AcmeError, OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
