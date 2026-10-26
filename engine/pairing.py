"""Pairing codes: a worker prints one, the controller reads it.

A code is "CW1-" + url-safe base64 of {"name": ..., "url": ...}.
"""
import base64
import json
import re

PREFIX = "CW1-"


def make_code(name, url):
    raw = json.dumps({"name": name, "url": url}, separators=(",", ":")).encode()
    return PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def parse_code(code):
    code = code.strip()
    if not code.startswith(PREFIX):
        raise ValueError(f"not a pairing code: {code[:14]}")
    body = code[len(PREFIX):]
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        name, url = str(data["name"]), str(data["url"]).rstrip("/")
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"damaged pairing code ({e})") from None
    if not re.fullmatch(r"[\w .-]{1,40}", name):
        raise ValueError("bad worker name in code")
    if not re.fullmatch(r"https://[a-z0-9-]+\.trycloudflare\.com", url):
        raise ValueError("bad worker address in code")
    return {"name": name, "url": url}


def parse_codes(text):
    """Split a pasted block into codes (spaces, commas or new lines). Placeholders such as CW1-.... are skipped."""
    out = []
    for tok in re.split(r"[\s,]+", text or ""):
        if tok.startswith(PREFIX) and "...." not in tok:
            out.append(parse_code(tok))
    return out
