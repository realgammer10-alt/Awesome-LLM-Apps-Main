import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import urlopen

from monitor import ConfigError, Monitor, create_server, load_config, validate_config


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        status = 503 if self.path == "/unhealthy" else 200
        self.send_response(status)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format: str, *args: object) -> None:
        return


class ConfigTests(unittest.TestCase):
    def test_valid_config_applies_defaults(self) -> None:
        config = validate_config({"services": [{"name": "API", "url": "https://example.com/health"}]})
        self.assertEqual(config.poll_interval_seconds, 30)
        self.assertEqual(config.request_timeout_seconds, 5)
        self.assertEqual(config.services[0].name, "API")

    def test_rejects_bad_intervals_and_boolean_values(self) -> None:
        for value in (True, 4, 3601, "30"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                validate_config({"poll_interval_seconds": value})

    def test_rejects_invalid_and_credentialed_urls(self) -> None:
        for url in ("file:///etc/passwd", "https://user:secret@example.com", "https://example.com/#fragment"):
            with self.subTest(url=url), self.assertRaises(ConfigError):
                validate_config({"services": [{"name": "API", "url": url}]})

    def test_rejects_duplicate_names_and_unknown_keys(self) -> None:
        with self.assertRaisesRegex(ConfigError, "unique"):
            validate_config(
                {
                    "services": [
                        {"name": "API", "url": "http://example.com"},
                        {"name": " api ", "url": "http://example.org"},
                    ]
                }
            )
        with self.assertRaisesRegex(ConfigError, "unknown"):
            validate_config({"poll_intervl_seconds": 10})

    def test_reports_invalid_json_and_missing_files(self) -> None:
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "services.json"
            config_path.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "invalid JSON"):
                load_config(config_path)
            with self.assertRaisesRegex(ConfigError, "cannot read"):
                load_config(Path(directory) / "missing.json")


class MonitorIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.health_server = ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
        cls.health_thread = threading.Thread(target=cls.health_server.serve_forever, daemon=True)
        cls.health_thread.start()
        base_url = f"http://127.0.0.1:{cls.health_server.server_port}"
        cls.config = validate_config(
            {
                "services": [
                    {"name": "Healthy API", "url": f"{base_url}/healthy"},
                    {"name": "Unavailable API", "url": f"{base_url}/unhealthy"},
                    {"name": "Missing API", "url": "http://127.0.0.1:1/health"},
                ]
            }
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.health_server.shutdown()
        cls.health_server.server_close()
        cls.health_thread.join(timeout=2)

    def setUp(self) -> None:
        self.monitor = Monitor(self.config)
        self.monitor.check_once()

    def test_check_reports_healthy_http_error_and_connection_failure(self) -> None:
        status = self.monitor.snapshot()
        self.assertEqual(status["status"], "degraded")
        self.assertEqual(status["summary"], {"total": 3, "healthy": 1, "down": 2, "unknown": 0})

        healthy, unavailable, missing = status["services"]
        self.assertEqual(healthy["status"], "healthy")
        self.assertEqual(healthy["status_code"], 200)
        self.assertGreaterEqual(healthy["response_time_ms"], 0)
        self.assertIsNotNone(healthy["checked_at"])
        self.assertEqual(unavailable["status"], "down")
        self.assertEqual(unavailable["status_code"], 503)
        self.assertEqual(unavailable["error"], "HTTP 503")
        self.assertEqual(missing["status"], "down")
        self.assertIn("Request failed", missing["error"])

    def test_status_and_health_apis_and_dashboard_assets(self) -> None:
        server = create_server("127.0.0.1", 0, self.monitor)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(f"{base_url}/api/status", timeout=2) as response:
                payload = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(payload["summary"]["total"], 3)

            with urlopen(f"{base_url}/api/health", timeout=2) as response:
                health = json.loads(response.read())
                self.assertEqual(health["status"], "ok")

            with urlopen(base_url, timeout=2) as response:
                self.assertIn(b"Endpoint monitor", response.read())
                self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])

            with self.assertRaises(HTTPError) as response:
                urlopen(f"{base_url}/not-found", timeout=2)
            self.assertEqual(response.exception.code, 404)
            response.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_empty_configuration_has_no_false_healthy_status(self) -> None:
        empty_monitor = Monitor(validate_config({"services": []}))
        empty_monitor.check_once()
        status = empty_monitor.snapshot()
        self.assertEqual(status["status"], "unknown")
        self.assertEqual(status["summary"], {"total": 0, "healthy": 0, "down": 0, "unknown": 0})


if __name__ == "__main__":
    unittest.main()
