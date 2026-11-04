"""Shared Provider preparation and HTTP probes; independent of the Harness wire format."""
import http.client
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request


_ensured = {}


def ensure_provider(provider):
    """Optional trusted local preparation; never log its command or output."""
    cache_key = (provider.name, provider.base_url, provider.ensure)
    if not provider.ensure or time.time() - _ensured.get(cache_key, 0) < 60:
        return
    try:
        done = subprocess.run(os.path.expanduser(provider.ensure).split(), stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("provider preparation failed") from None
    if done.returncode:
        raise ValueError("provider preparation failed")
    _ensured[cache_key] = time.time()


def shallow_probe(provider, timeout=5):
    """Does the gateway answer at all? A connection that hangs is the failure Claude Code stays silent about."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        opener.open(provider.base_url + "/", timeout=timeout).read(16)
        return True
    except urllib.error.HTTPError:
        return True
    except (OSError, http.client.HTTPException):   # an endpoint that does not speak HTTP raises HTTPException
        return False


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        # Credentials must never follow an endpoint's redirect to another service.
        return None


def deep_probe(provider, model, timeout=30):
    """One real, tiny model call, for when the gateway answers but the model behind it may not."""
    return deep_check(provider, model, timeout)[0]


ERROR_TYPE = re.compile(r"[a-z_]{1,60}")


def _error_type(payload):
    """None unless payload is an Anthropic error object; then its type, if that is a plain identifier, else "unknown".
    Never the message: it may echo a secret."""
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("type") != "error":
        return None
    kind = data["error"].get("type") if isinstance(data.get("error"), dict) else None
    return kind if isinstance(kind, str) and ERROR_TYPE.fullmatch(kind) else "unknown"


def deep_check(provider, model, timeout=30):
    """deep_probe with a reason: (ok, reason). The reason holds only status codes, error types and exception class names,
    never the key, the URL or an upstream message.

    The call streams. A gateway in front of a thinking model (for example a proxy for a non-Anthropic model) ignores max_tokens, and
    without a stream it answers only once the model has thought it through, which can outlast a short timeout;
    the stream's first event comes at once."""
    body = json.dumps({"model": model.replace("[1m]", ""), "max_tokens": 1, "stream": True,
                       "messages": [{"role": "user", "content": "hi"}]}).encode()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        request = urllib.request.Request(provider.base_url + "/v1/messages", body, method="POST", headers={
            "content-type": "application/json", "anthropic-version": "2023-06-01", **provider.auth_headers()})
        with opener.open(request, timeout=timeout) as response:
            if "text/event-stream" not in response.headers.get("content-type", ""):
                response.read(16)   # a gateway that does not stream answered with the whole message
                return True, ""
            for _ in range(20):
                line = response.readline(65536)
                if not line:
                    break
                if line.startswith(b"data:"):
                    kind = _error_type(line[5:])
                    return (False, "the model answered with an error event (%s)" % kind) if kind else (True, "")
            return False, "the model's stream ended before its first event"
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            return False, "the endpoint redirected (HTTP %d); credentials are never sent on" % exc.code
        try:
            kind = _error_type(exc.read(65536))
        except OSError:
            kind = None
        return False, "HTTP %d%s" % (exc.code, " " + kind if kind else "")
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            return False, "no answer within %g seconds" % timeout
        return False, "connection failed (%s)" % type(exc.reason).__name__
    except TimeoutError:
        return False, "no answer within %g seconds" % timeout
    except (OSError, ValueError, http.client.HTTPException) as exc:
        return False, "request failed (%s)" % type(exc).__name__


