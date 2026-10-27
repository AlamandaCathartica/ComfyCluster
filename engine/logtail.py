"""Read the end of a log file. Used by the controller GUI and by the worker add-on (comfycluster_stats.py)."""
import os
import re

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
WINDOW = 256 * 1024          # never read more than this from the end of a file


def tail_log(path, lines=400, since=0):
    """Return {"lines": [...], "size": bytes}. `since` is a byte offset from an earlier call (Clear in the GUI):
    only text written after it is returned. Progress bars that redraw with \\r keep only their last frame."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return {"lines": [], "size": 0, "missing": True}
    if since > size:                     # the log was restarted
        since = 0
    start = max(since, size - WINDOW)
    with open(path, "rb") as f:
        f.seek(start)
        text = f.read(size - start).decode("utf-8", "replace")
    parts = text.split("\n")
    if start > since and start > 0:      # the window begins inside a line
        parts = parts[1:]
    out = []
    for raw in parts:
        segs = [s for s in ANSI.sub("", raw).split("\r") if s.strip()]
        if segs:
            out.append(segs[-1].rstrip())
    return {"lines": out[-lines:], "size": size}
