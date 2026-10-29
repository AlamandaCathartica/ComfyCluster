"""Pairing codes: a worker prints one, the controller reads it.

A code is "CW1-" + url-safe base64 of {"name": ..., "url": ..., "key": ...}.

`key` is the worker's model sync key (see modelsync.py). The controller sends it with every model it pushes, and the
worker refuses a push without it. A code from an older worker has no key: it still pairs, it just gets no model sync.
"""
import base64
import json
import re

from modelsync import KEY_RE

PREFIX = "CW1-"


def make_code(name, url, key=""):
    data = {"name": name, "url": url}
    if key:
        data["key"] = key
    raw = json.dumps(data, separators=(",", ":")).encode()
    return PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def parse_code(code):
    code = code.strip()
    if not code.startswith(PREFIX):
        raise ValueError(f"not a pairing code: {code[:14]}")
    body = code[len(PREFIX):]
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        name, url, key = str(data["name"]), str(data["url"]).rstrip("/"), str(data.get("key") or "")
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"damaged pairing code ({e})") from None
    if not re.fullmatch(r"[\w .-]{1,40}", name):
        raise ValueError("bad worker name in code")
    if not re.fullmatch(r"https://[a-z0-9-]+\.trycloudflare\.com", url):
        raise ValueError("bad worker address in code")
    if key and not KEY_RE.fullmatch(key):
        raise ValueError("bad sync key in code")
    return {"name": name, "url": url, "key": key}


def parse_codes(text):
    """Split a pasted block into codes (spaces, commas or new lines). Placeholders such as CW1-.... are skipped."""
    out = []
    for tok in re.split(r"[\s,]+", text or ""):
        if tok.startswith(PREFIX) and "...." not in tok:
            out.append(parse_code(tok))
    return out
