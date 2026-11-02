from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


LOGGER = logging.getLogger("llm_app_monitor")
DEFAULT_CONFIG = Path(__file__).with_name("services.json")
MAX_SERVICES = 100
MAX_NAME_LENGTH = 80
MAX_URL_LENGTH = 2048
MAX_WORKERS = 16


class ConfigError(ValueError):
    """Raised when a monitoring configuration is invalid."""


@dataclass(frozen=True)
class Service:
    name: str
    url: str


@dataclass(frozen=True)
class Config:
    services: tuple[Service, ...]
    poll_interval_seconds: int = 30
    request_timeout_seconds: int = 5


def _positive_integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise ConfigError(f"{field} must be between {minimum} and {maximum}")
    return value


def _validate_url(value: Any, index: int) -> str:
    label = f"services[{index}].url"
    if not isinstance(value, str) or not value or len(value) > MAX_URL_LENGTH:
        raise ConfigError(f"{label} must be a non-empty URL no longer than {MAX_URL_LENGTH} characters")
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise ConfigError(f"{label} must not contain whitespace or control characters")

    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ConfigError(f"{label} is invalid: {exc}") from exc

    if parts.scheme not in {"http", "https"} or not hostname:
        raise ConfigError(f"{label} must be an absolute http or https URL")
    if parts.username is not None or parts.password is not None:
        raise ConfigError(f"{label} must not include credentials")
    if parts.fragment:
        raise ConfigError(f"{label} must not include a fragment")
    if port is not None and not 1 <= port <= 65535:
        raise ConfigError(f"{label} has an invalid port")
    return value


def validate_config(data: Any) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("configuration must be a JSON object")

    unknown_keys = set(data) - {
        "services",
        "poll_interval_seconds",
        "request_timeout_seconds",
    }
    if unknown_keys:
        raise ConfigError(f"unknown configuration key(s): {', '.join(sorted(unknown_keys))}")

    interval = _positive_integer(data.get("poll_interval_seconds", 30), "poll_interval_seconds", 5, 3600)
    timeout = _positive_integer(data.get("request_timeout_seconds", 5), "request_timeout_seconds", 1, 60)
    raw_services = data.get("services", [])
    if not isinstance(raw_services, list):
        raise ConfigError("services must be a list")
    if len(raw_services) > MAX_SERVICES:
        raise ConfigError(f"services must contain no more than {MAX_SERVICES} entries")

    services: list[Service] = []
    seen_names: set[str] = set()
    for index, raw_service in enumerate(raw_services):
        label = f"services[{index}]"
        if not isinstance(raw_service, dict):
            raise ConfigError(f"{label} must be an object")
        if set(raw_service) != {"name", "url"}:
            raise ConfigError(f"{label} must contain exactly the name and url fields")

        name = raw_service["name"]
        if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME_LENGTH:
            raise ConfigError(f"{label}.name must be 1 to {MAX_NAME_LENGTH} characters")
        if any(ord(character) < 32 for character in name):
            raise ConfigError(f"{label}.name must not contain control characters")
        normalized_name = name.strip().casefold()
        if normalized_name in seen_names:
            raise ConfigError(f"service names must be unique (duplicate: {name})")
        seen_names.add(normalized_name)

        services.append(Service(name=name.strip(), url=_validate_url(raw_service["url"], index)))

    return Config(
        services=tuple(services),
        poll_interval_seconds=interval,
        request_timeout_seconds=timeout,
    )


def load_config(path: Path) -> Config:
    try:
        with path.open(encoding="utf-8") as config_file:
            data = json.load(config_file)
    except OSError as exc:
        raise ConfigError(f"cannot read configuration file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {path} at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
    return validate_config(data)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _check_service(service: Service, timeout: int) -> dict[str, Any]:
    checked_at = _timestamp()
    started_at = time.perf_counter()
    request = Request(service.url, headers={"Accept": "*/*", "User-Agent": "AwesomeLLMAppsMonitor/1.0"})
    status_code: int | None = None
    error: str | None = None

    try:
        with urlopen(request, timeout=timeout) as response:
            status_code = response.status
            if not 200 <= status_code < 300:
                error = f"HTTP {status_code}"
    except HTTPError as exc:
        status_code = exc.code
        error = f"HTTP {exc.code}"
        exc.close()
    except (TimeoutError, URLError, OSError) as exc:
        reason = exc.reason if isinstance(exc, URLError) else exc
        error = f"Request failed: {reason}"

    return {
        "name": service.name,
        "url": service.url,
        "status": "healthy" if error is None else "down",
        "checked_at": checked_at,
        "response_time_ms": round((time.perf_counter() - started_at) * 1000, 1),
        "status_code": status_code,
        "error": error,
    }


class Monitor:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._started_at = time.monotonic()
        self._last_checked_at: str | None = None
        self._services = [
            {
                "name": service.name,
                "url": service.url,
                "status": "unknown",
                "checked_at": None,
                "response_time_ms": None,
                "status_code": None,
                "error": None,
            }
            for service in config.services
        ]

    def check_once(self) -> None:
        if self.config.services:
            workers = min(len(self.config.services), MAX_WORKERS)
            with ThreadPoolExecutor(max_workers=workers) as executor:
                results = list(
                    executor.map(
                        lambda service: _check_service(service, self.config.request_timeout_seconds),
                        self.config.services,
                    )
                )
        else:
            results = []

        with self._lock:
            self._services = results
            self._last_checked_at = _timestamp()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            services = [dict(service) for service in self._services]
            last_checked_at = self._last_checked_at

        healthy = sum(service["status"] == "healthy" for service in services)
        down = sum(service["status"] == "down" for service in services)
        if services and healthy == len(services):
            overall = "healthy"
        elif services and down == len(services):
            overall = "down"
        elif healthy and down:
            overall = "degraded"
        else:
            overall = "unknown"

        return {
            "status": overall,
            "last_checked_at": last_checked_at,
            "uptime_seconds": int(time.monotonic() - self._started_at),
            "summary": {
                "total": len(services),
                "healthy": healthy,
                "down": down,
                "unknown": len(services) - healthy - down,
            },
            "services": services,
        }


class PollingThread:
    def __init__(self, monitor: Monitor) -> None:
        self.monitor = monitor
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name="endpoint-poller", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        batches = (len(self.monitor.config.services) + MAX_WORKERS - 1) // MAX_WORKERS
        timeout = max(1, batches) * self.monitor.config.request_timeout_seconds + 2
        self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.monitor.check_once()
            except Exception:
                LOGGER.exception("Unexpected error while checking configured endpoints")
            if self._stop_event.wait(self.monitor.config.poll_interval_seconds):
                break


class MonitorRequestHandler(BaseHTTPRequestHandler):
    monitor: Monitor

    def _write(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; connect-src 'self'; style-src 'self'; script-src 'self'; "
            "img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; object-src 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/status":
            body = json.dumps(self.monitor.snapshot(), separators=(",", ":")).encode("utf-8")
            self._write(body, "application/json; charset=utf-8")
            return
        if path == "/api/health":
            body = json.dumps(
                {"status": "ok", "uptime_seconds": int(time.monotonic() - self.monitor._started_at)},
                separators=(",", ":"),
            ).encode("utf-8")
            self._write(body, "application/json; charset=utf-8")
            return

        assets = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/app.css": ("app.css", "text/css; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
        }
        asset = assets.get(path)
        if asset is None:
            self._write(b'{"error":"Not found"}', "application/json; charset=utf-8", 404)
            return

        filename, content_type = asset
        body = (Path(__file__).parent / "static" / filename).read_bytes()
        self._write(body, content_type)

    def do_POST(self) -> None:
        self.send_response(405)
        self.send_header("Allow", "GET")
        self.send_header("Content-Length", "0")
        self.end_headers()


def create_server(host: str, port: int, monitor: Monitor) -> ThreadingHTTPServer:
    class Handler(MonitorRequestHandler):
        pass

    Handler.monitor = monitor
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer between 1 and 65535") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor configured HTTP health endpoints.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="path to the JSON services file")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=_port, default=8765, help="dashboard port (default: 8765)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        parser.error(str(exc))

    monitor = Monitor(config)
    poller = PollingThread(monitor)
    try:
        server = create_server(args.host, args.port, monitor)
    except OSError as exc:
        parser.error(f"cannot bind dashboard to {args.host}:{args.port}: {exc}")

    poller.start()
    LOGGER.info("Dashboard listening at http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Shutting down dashboard")
    finally:
        server.shutdown()
        server.server_close()
        poller.stop()


if __name__ == "__main__":
    main()
