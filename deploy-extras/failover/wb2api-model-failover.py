#!/usr/bin/env python
"""Model auto-failover proxy for WB2api Hub.

Sits between Claude Code and the gateway. When the gateway answers 429 for
the requested model (model-level cooldown), this proxy transparently retries
the same request with the next model in MODEL_CHAIN — no manual switching.
Only POSTed Anthropic / OpenAI JSON bodies are rewritten; everything else is
passed through unchanged, including SSE streams.

Listen : http://127.0.0.1:8799  ->  your WB2api gateway (configurable)

Config is read from environment variables, or from an optional .env file
placed next to this script (see .env.example):

    WB2API_KEY      gateway API key (wb-...)
    WB2API_TARGET   upstream gateway URL, e.g. http://YOUR_HOST:7863
    FAILOVER_LISTEN_HOST / FAILOVER_LISTEN_PORT  (defaults 127.0.0.1 / 8799)
"""
import http.server
import json
import os
import re
import socket
import socketserver
import sys
import time

import requests


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(SCRIPT_DIR, "wb2api-failover.log")


def log(msg):
    """Append one timestamped line to the proxy log file."""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    except OSError:
        pass


def _setup_stdio():
    """pythonw has no console: sys.stdout/stderr are None.

    A handler thread that hit an exception would then fail while printing
    the traceback and die without ever closing the socket — the client
    sees zero bytes until it times out. Redirect stdio to the log file so
    output (and tracebacks) always have somewhere to go.
    """
    if sys.stdout is None or sys.stderr is None:
        try:
            if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1_000_000:
                os.remove(LOG_PATH)
        except OSError:
            pass
        fh = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = fh
        if sys.stderr is None:
            sys.stderr = fh


_setup_stdio()


def _load_dotenv():
    """Load KEY=VALUE pairs from a .env file next to this script.

    Zero-dependency, minimal parser; real environment variables win.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()

LISTEN_HOST = os.environ.get("FAILOVER_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("FAILOVER_LISTEN_PORT", "8799"))

# Upstream gateway. Set WB2API_TARGET / WB2API_KEY in your environment;
# the values are intentionally NOT hardcoded so this file can be shared.
TARGET = os.environ.get("WB2API_TARGET", "http://127.0.0.1:7863").rstrip("/")
GATEWAY_KEY = os.environ.get("WB2API_KEY", "")

# Virtual model name: when the client asks for "auto" the proxy walks the
# whole chain and uses the first model that isn't cooled down.
AUTO_MODEL = "auto"

# Priority order: models listed here are tried first (most capable first).
# At startup the proxy pulls the gateway's full /v1/models list and appends
# every other available model after these, so the whole catalog is in the pool
# and upstream additions join automatically.
PRIORITY_MODELS = [
    "glm-5.3",
    "deepseek-v4-pro",
    "kimi-k3-1",
    "deepseek-v4.1-flash",
    "glm-5.3-flash",
    "minimax-m3",
    "hy4-preview-f",
    "hy3",
]

# Fallback chain used only if the gateway model list can't be fetched.
FALLBACK_CHAIN = list(PRIORITY_MODELS) + [
    "glm-5.2", "glm-5.1", "kimi-k2.8-preview", "kimi-k2.7", "kimi-k2.6",
]

MODEL_CHAIN = list(FALLBACK_CHAIN)


def auth_headers():
    return {"Authorization": "Bearer " + GATEWAY_KEY} if GATEWAY_KEY else {}


def build_chain():
    """Fetch every model from the gateway, priority ones first."""
    try:
        resp = requests.get(
            TARGET + "/v1/models",
            headers=auth_headers(),
            timeout=15,
        )
        ids = [m.get("id") for m in resp.json().get("data", []) if m.get("id")]
    except Exception as exc:
        log("model list fetch failed (%s); using fallback chain" % exc)
        return FALLBACK_CHAIN
    ids = list(dict.fromkeys(ids))  # de-dup, preserve order
    ordered = [m for m in PRIORITY_MODELS if m in ids]
    ordered += [m for m in ids if m not in ordered]
    log("pool (%d models): %s" % (len(ordered), ", ".join(ordered)))
    return ordered

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    "content-length",
}
JSON_MODEL_PATHS = (
    "/v1/messages", "/messages",
    "/v1/chat/completions", "/chat/completions",
    "/v1/responses", "/responses",
)


def chain_from(requested):
    requested = (requested or "").strip()
    if requested == AUTO_MODEL:
        # auto: schedule over the whole chain, never send "auto" upstream
        return list(MODEL_CHAIN)
    if requested in MODEL_CHAIN:
        i = MODEL_CHAIN.index(requested)
        return MODEL_CHAIN[i:]
    # unrecognised model: try it first, then fall through the whole chain
    return [requested] + MODEL_CHAIN if requested else list(MODEL_CHAIN)


# model -> epoch seconds until which the model is known to be cooled down
MODEL_COOLDOWNS = {}
DEFAULT_COOLDOWN_S = 300


def is_model_unavailable(status, detail):
    """A 429 (cooldown) or a 400/404 that names the model -> try next model."""
    if status == 429:
        return True
    if status in (400, 404):
        text = (detail or b"").lower()
        return b"model" in text
    return False


def is_error_envelope(detail):
    """Gateway wraps known cooldowns as HTTP 200 + {"type":"error",...}.

    Returns True for a rate-limit / model error envelope so the caller can
    rotate to the next model instead of relaying the error to the client.
    """
    try:
        obj = json.loads((detail or b"").decode("utf-8"))
    except Exception:
        return False
    if not isinstance(obj, dict) or obj.get("type") != "error":
        return False
    err = obj.get("error") or {}
    etype = str(err.get("type") or "")
    msg = str(err.get("message") or "").lower()
    if etype in ("rate_limit_error", "not_found_error"):
        return True
    return "frequency limit" in msg or ("model" in msg and "not" in msg)


def remember_cooldown(model, detail):
    """Cache how long `model` stays throttled so auto skips it next time."""
    wait = DEFAULT_COOLDOWN_S
    m = re.search(rb"retry in (\d+)\s*s", detail or b"")
    if m:
        wait = int(m.group(1))
    MODEL_COOLDOWNS[model] = time.time() + max(5, wait)


def cooling(model):
    until = MODEL_COOLDOWNS.get(model)
    return bool(until and until > time.time())


def live_chain(chain):
    """Drop models still in cooldown; fall back to the full chain if all cool."""
    ready = [m for m in chain if not cooling(m)]
    return ready or chain


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _base_headers(self):
        headers = {}
        for k, v in self.headers.items():
            if k.lower() in HOP_BY_HOP:
                continue
            headers[k] = v
        return headers

    def _passthrough(self, body):
        url = TARGET + self.path
        upstream = requests.request(
            self.command, url, headers=self._base_headers(), data=body,
            stream=True, timeout=(30, 600), allow_redirects=False,
        )
        self.send_response(upstream.status_code)
        for k, v in upstream.headers.items():
            if k.lower() in HOP_BY_HOP:
                continue
            self.send_header(k, v)
        self.end_headers()
        if self.command == "HEAD":
            upstream.close()
            return
        try:
            for chunk in upstream.iter_content(chunk_size=None):
                if chunk:
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            upstream.close()

    def _proxy_with_failover(self, raw_body):
        try:
            parsed = json.loads(raw_body.decode("utf-8"))
        except Exception:
            return self._passthrough(raw_body)

        chain = live_chain(chain_from(parsed.get("model")))
        headers = self._base_headers()
        url = TARGET + self.path
        last_detail = b""
        log("POST %s requested=%r trying: %s" %
            (self.path, parsed.get("model"), ", ".join(chain[:4])))

        for model in chain:
            body = dict(parsed)
            body["model"] = model
            data = json.dumps(body).encode("utf-8")
            t0 = time.time()
            try:
                upstream = requests.request(
                    self.command, url, headers=headers, data=data,
                    # Connect must be quick; a stalled model must not pin the
                    # whole auto chain — rotate if no data flows within 60s.
                    stream=True, timeout=(10, 60), allow_redirects=False,
                )
            except requests.RequestException as exc:
                # Transient/stalled upstream for this model: try the next one.
                log("model %s upstream error after %.1fs: %s" %
                    (model, time.time() - t0, exc))
                continue

            status = upstream.status_code
            ctype = upstream.headers.get("Content-Type", "").lower()
            log("model %s -> HTTP %d %s (%.1fs)" %
                (model, status, ctype, time.time() - t0))

            # Real SSE stream: relay chunks live. (A cooled model comes back as
            # application/json, not event-stream, so it never reaches here.)
            if status not in (429,) and "text/event-stream" in ctype:
                self._relay_headers(upstream)
                self._stream_back(upstream)
                log("model %s stream relayed, done" % model)
                return

            # Non-stream body: buffer once and inspect it.
            detail = upstream.content or b""
            if is_model_unavailable(status, detail) or is_error_envelope(detail):
                # cooldown / model error -> cache expiry, rotate to next model
                remember_cooldown(model, detail)
                last_detail = detail
                upstream.close()
                log("model %s unavailable, rotating" % model)
                continue

            # success: relay the buffered JSON straight back
            self._relay_headers(upstream, body_len=len(detail))
            if self.command == "HEAD":
                upstream.close()
                return
            self.wfile.write(detail)
            self.wfile.flush()
            upstream.close()
            return

        # every model in the chain was throttled: surface the last error body
        if last_detail:
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(last_detail)))
            self.end_headers()
            self.wfile.write(last_detail)
        else:
            payload = b'{"type":"error","error":{"type":"api_error",' \
                      b'"message":"no models available"}}'
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    def _relay_headers(self, upstream, body_len=None):
        self.send_response(upstream.status_code)
        for k, v in upstream.headers.items():
            if k.lower() in HOP_BY_HOP:
                continue
            self.send_header(k, v)
        # We stripped Content-Length as hop-by-hop; for a buffered body we
        # must restore it so HTTP/1.1 clients know where the body ends.
        if body_len is not None:
            self.send_header("Content-Length", str(body_len))
        self.end_headers()

    def _stream_back(self, upstream):
        if self.command == "HEAD":
            upstream.close()
            return
        try:
            for chunk in upstream.iter_content(chunk_size=None):
                if chunk:
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            upstream.close()

    def _handle(self):
        # Each upstream call is one proxied request; close after responding
        # so streamed bodies (no Content-Length / chunked) end cleanly.
        self.close_connection = True
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None
        path = self.path.split("?", 1)[0]
        if self.command == "POST" and path in JSON_MODEL_PATHS and body:
            return self._proxy_with_failover(body)
        return self._passthrough(body)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle
    do_PATCH = _handle

    def log_message(self, fmt, *args):
        pass


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    # Windows SO_REUSEADDR lets a second copy bind the "same" port while the
    # first is listening (connections then land on either process). Binding
    # without it fails atomically, which is the single-instance guarantee.
    allow_reuse_address = False


if __name__ == "__main__":
    # Fast path: if something already answers on the port, exit quietly.
    try:
        probe = socket.create_connection((LISTEN_HOST, LISTEN_PORT), timeout=1)
        probe.close()
        log("already running on %s:%d, exiting" % (LISTEN_HOST, LISTEN_PORT))
        sys.exit(0)
    except OSError:
        pass

    MODEL_CHAIN = build_chain()
    # Bind failure (WSAEADDRINUSE) means another copy won the race at boot
    # (startup VBS and the watchdog can fire within the same second).
    try:
        server = ThreadingServer((LISTEN_HOST, LISTEN_PORT), Handler)
    except OSError:
        log("port %d already bound by another instance, exiting" % LISTEN_PORT)
        sys.exit(0)
    log("model failover proxy on http://%s:%d -> %s" %
        (LISTEN_HOST, LISTEN_PORT, TARGET))
    server.serve_forever()
