"""ComfyCluster model sync protocol (CMS1): the pieces the controller and the worker add-on share.

The rule of the protocol: a model enters the cluster in ONE place, the controller (a local upload in the GUI, a link the
controller downloads itself, or models.json). From there the controller sends it to every worker. A worker never
downloads a model from the internet and never talks to another worker.

    local PC --upload--> controller --push--> worker1 .. workerN
    web link --download-> controller

This file has no dependency on ComfyUI, Flask or requests, so both sides can import it.
"""
import hashlib
import json
import os
import re
import threading
import time

PROTOCOL = "CMS1"
KEY_HEADER = "X-ComfyCluster-Key"
CHUNK = 32 * 1024 * 1024                 # one request carries at most this much (Cloudflare tunnels allow 100 MB)
MODEL_EXT = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft", ".onnx")
SKIP_FOLDERS = {"diffusers", "configs"}  # whole-repository folders and config files are not single model files
STAGE_DIR = ".cc-sync"                   # inside models/: partial files wait here (same disk, so the final move is atomic)
SETTLE_SECONDS = 3                       # a file written in the last seconds may still be growing: do not send it yet
RESERVE = 1024 ** 3                      # keep this much disk free on a worker

NAME_RE = re.compile(r"[\w][\w .,+()\[\]'&@#=~\-]{0,200}")
FOLDER_RE = re.compile(r"[\w][\w.\-]{0,60}")
SHA_RE = re.compile(r"[0-9a-f]{64}")
KEY_RE = re.compile(r"[A-Za-z0-9_\-]{16,64}")
MAX_DEPTH = 6                            # folder + up to five levels


def check_path(rel):
    """Validate a model path like 'checkpoints/sdxl/base.safetensors'. Returns it normalised, or raises ValueError.

    Everything that comes from the network goes through here before it touches the disk."""
    parts = str(rel or "").replace("\\", "/").split("/")
    if not 2 <= len(parts) <= MAX_DEPTH or "" in parts:
        raise ValueError("bad model path")
    if not FOLDER_RE.fullmatch(parts[0]) or parts[0] in SKIP_FOLDERS or ".." in parts[0]:
        raise ValueError("bad model folder")
    for p in parts[1:]:
        if not NAME_RE.fullmatch(p) or ".." in p:
            raise ValueError("bad model name")
    if not parts[-1].lower().endswith(MODEL_EXT):
        raise ValueError("this file type is not a model")
    return "/".join(parts)


def abs_path(models_dir, rel):
    return os.path.join(models_dir, *check_path(rel).split("/"))


def stage_key(rel):
    return hashlib.sha1(rel.encode("utf-8")).hexdigest()[:24]


# ---------------------------------------------------------------------------------------------- hashes
def sha256_file(path, stop=None):
    """SHA-256 of a file. `stop()` returning True aborts with InterruptedError."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            blk = f.read(4 << 20)
            if not blk:
                return h.hexdigest()
            if stop and stop():
                raise InterruptedError
            h.update(blk)


class HashIndex:
    """Remembers the SHA-256 of files, keyed by path and checked against size and modification time, so a
    multi-gigabyte model is hashed once and not on every sync."""

    def __init__(self, path=None):
        self.path, self._d, self._lock, self._dirty = path, {}, threading.Lock(), False
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    raw = json.load(f)
                self._d = {k: v for k, v in raw.items() if isinstance(v, list) and len(v) == 3}
            except (OSError, ValueError, AttributeError):
                self._d = {}

    def get(self, rel, size, mtime_ns):
        with self._lock:
            e = self._d.get(rel)
        return e[2] if e and e[0] == size and e[1] == mtime_ns else None

    def put(self, rel, size, mtime_ns, sha):
        with self._lock:
            self._d[rel] = [size, mtime_ns, sha]
            self._dirty = True

    def forget_missing(self, present):
        with self._lock:
            for k in [k for k in self._d if k not in present]:
                del self._d[k]
                self._dirty = True

    def save(self):
        if not self.path:
            return
        with self._lock:
            if not self._dirty:
                return
            data, self._dirty = json.dumps(self._d), False
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            tmp = self.path + ".part"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, self.path)
        except OSError:
            pass


# ---------------------------------------------------------------------------------------------- inventory
def scan(models_dir):
    """Every model file under models_dir/<folder>/... -> ([{path, size, mtime_ns}], [paths that were skipped]).

    Skips hidden files and folders, symlinked files, the staging folder, and names the protocol does not accept."""
    out, skipped = [], []
    try:
        folders = sorted(os.listdir(models_dir))
    except OSError:
        return out, skipped
    for folder in folders:
        top = os.path.join(models_dir, folder)
        if folder.startswith(".") or folder in SKIP_FOLDERS or not os.path.isdir(top):
            continue
        for d, dirs, names in os.walk(top):
            dirs[:] = sorted(x for x in dirs if not x.startswith("."))
            for n in sorted(names):
                if n.startswith(".") or not n.lower().endswith(MODEL_EXT):
                    continue
                full = os.path.join(d, n)
                rel = folder + "/" + os.path.relpath(full, top).replace(os.sep, "/")
                if os.path.islink(full):
                    continue
                try:
                    check_path(rel)
                    st = os.stat(full)
                except ValueError:
                    skipped.append(rel)
                    continue
                except OSError:
                    continue
                if st.st_size > 0:
                    out.append(dict(path=rel, size=st.st_size, mtime_ns=st.st_mtime_ns))
    return out, skipped


def inventory(models_dir, index):
    """scan() plus the cached hash of each file (None where it has not been hashed yet)."""
    files, skipped = scan(models_dir)
    for f in files:
        f["sha256"] = index.get(f["path"], f["size"], f["mtime_ns"])
    return files, skipped


def hash_missing(models_dir, index, settle=0, stop=None):
    """Hash every file that has no valid cached hash. Files modified less than `settle` seconds ago are left alone
    (they may still be growing). Returns how many files still have no hash."""
    files, _ = scan(models_dir)
    index.forget_missing({f["path"] for f in files})
    waiting = 0
    for f in files:
        if index.get(f["path"], f["size"], f["mtime_ns"]):
            continue
        if settle and time.time() - f["mtime_ns"] / 1e9 < settle:
            waiting += 1
            continue
        full = abs_path(models_dir, f["path"])
        try:
            sha = sha256_file(full, stop)
            st = os.stat(full)
        except OSError:
            continue
        if (st.st_size, st.st_mtime_ns) != (f["size"], f["mtime_ns"]):      # it changed while it was read
            waiting += 1
            continue
        index.put(f["path"], f["size"], f["mtime_ns"], sha)
        index.save()
    return waiting
