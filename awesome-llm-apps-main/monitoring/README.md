# Endpoint monitor

A lightweight dashboard for checking HTTP health endpoints belonging to the apps you run. It uses only Python's standard library and does not start or configure the monitored apps for you.

## Run

From the repository root, copy the example configuration, add your own health URLs, then start the monitor:

```powershell
Copy-Item monitoring\services.example.json monitoring\services.json
# Edit monitoring\services.json
python monitoring\monitor.py
```

Open <http://127.0.0.1:8765>. The dashboard refreshes its status view every two seconds; checks run in the background at the configured interval. Use `--config`, `--host`, and `--port` to override the defaults:

```powershell
python monitoring\monitor.py --config C:\path\to\services.json --port 9000
```

The server binds to `127.0.0.1` by default. Only bind to a non-loopback address on a trusted network: this dashboard does not provide authentication.

## Configuration

```json
{
  "poll_interval_seconds": 30,
  "request_timeout_seconds": 5,
  "services": [
    {
      "name": "Travel API",
      "url": "http://127.0.0.1:8000/health"
    }
  ]
}
```

- `poll_interval_seconds`: check interval from 5 to 3600 seconds.
- `request_timeout_seconds`: per-endpoint timeout from 1 to 60 seconds.
- `services`: up to 100 endpoints. Names must be unique; URLs must be absolute HTTP or HTTPS URLs without embedded credentials or fragments.

Any HTTP 2xx response is healthy. HTTP errors, timeouts, and connection failures are shown as down with their response time and last-check timestamp. An empty `services` list is valid and displays setup instructions in the dashboard.

## Tests

Run the standard-library unit and integration tests from the repository root:

```powershell
python -m unittest discover -s monitoring -v
```
