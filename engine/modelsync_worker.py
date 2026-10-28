"""Worker side of the model sync protocol (CMS1): receives models from the controller.

Used by the worker add-on (comfycluster_stats.py). Has no dependency on ComfyUI or aiohttp, so it can be tested alone.

A file arrives in slices of at most CHUNK bytes. Slices are written to models/.cc-sync/ and hashed as they come in, so
when the last slice lands the SHA-256 is already known: if it matches what the controller announced, the file is moved
into place in one atomic step, otherwise it is thrown away. A model is therefore never visible half-written.
"""
import hmac
import os
import shutil
import threading
import hashlib

try:
    from . import modelsync as ms
except ImportError:      # run outside the custom_nodes package (tests)
    import modelsync as ms


class Receiver:
    def __init__(self, models_dir, key, log=print):
        self.models_dir, self.key, self.log = models_dir, key, log
        self.stage = os.path.join(models_dir, ms.STAGE_DIR)
        self.index = ms.HashIndex(os.path.join(self.stage, "hashes.json"))
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._st = {}                      # rel -> {"h": hasher, "have": bytes written, "sha": ..., "total": ...}
        self._clean_parts()

    def _clean_parts(self):
        """Slices left behind by an earlier run cannot be resumed (the hash state is gone), so they are removed."""
        try:
            for n in os.listdir(self.stage):
                if n.endswith(".part"):
                    os.remove(os.path.join(self.stage, n))
        except OSError:
            pass

    # ------------------------------------------------------------------ access
    def authed(self, header_value):
        return bool(self.key) and hmac.compare_digest(str(header_value or ""), self.key)

    # ------------------------------------------------------------------ what is here
    def start(self):
        """Hash the files that have no hash yet, in the background (a multi-gigabyte model takes a while)."""
        threading.Thread(target=self._hash_loop, daemon=True, name="cc-model-hasher").start()

    def _hash_loop(self):
        while True:
            try:
                waiting = ms.hash_missing(self.models_dir, self.index, settle=ms.SETTLE_SECONDS)
            except Exception as e:  # noqa: BLE001  (never let the thread die)
                self.log(f"[ComfyCluster] model hashing: {e}")
                waiting = 0
            self._wake.wait(4 if waiting else 20)
            self._wake.clear()

    def inventory(self):
        files, skipped = ms.inventory(self.models_dir, self.index)
        try:
            free = shutil.disk_usage(self.models_dir).free
        except OSError:
            free = 0
        return {"protocol": ms.PROTOCOL, "files": files, "skipped": skipped[:20], "free": free}

    # ------------------------------------------------------------------ receiving
    def receive(self, rel, offset, total, sha, data):
        """Take one slice. Returns (http status, json body)."""
        try:
            rel = ms.check_path(rel)
        except ValueError as e:
            return 400, {"error": str(e)}
        sha = str(sha or "").lower()
        if not ms.SHA_RE.fullmatch(sha):
            return 400, {"error": "bad checksum"}
        if total <= 0 or offset < 0 or offset >= total or not data or offset + len(data) > total or len(data) > ms.CHUNK:
            return 400, {"error": "bad offset or size"}

        part = os.path.join(self.stage, ms.stage_key(rel) + ".part")
        with self._lock:
            st = self._st.get(rel)
            if offset == 0:                                           # (re)start this file
                self._drop(rel)
                try:
                    os.makedirs(self.stage, exist_ok=True)
                    free = shutil.disk_usage(self.models_dir).free
                except OSError as e:
                    return 500, {"error": f"disk: {e}"}
                if free < total + ms.RESERVE:
                    return 507, {"error": "not enough free disk space on the worker", "free": free}
                st = self._st[rel] = {"h": hashlib.sha256(), "have": 0, "sha": sha, "total": total}
            elif not st or st["have"] != offset or st["sha"] != sha or st["total"] != total:
                same = st and st["sha"] == sha and st["total"] == total
                return 409, {"error": "out of sync", "have": st["have"] if same else 0}

            try:
                with open(part, "ab" if offset else "wb") as f:
                    f.write(data)
            except OSError as e:
                self._drop(rel)
                return 500, {"error": f"could not write: {e}"}
            st["h"].update(data)
            st["have"] += len(data)
            if st["have"] < total:
                return 200, {"ok": True, "have": st["have"], "done": False}

            digest = st["h"].hexdigest()
            self._st.pop(rel, None)
            if digest != sha:                                         # damaged in transit, or it changed while being sent
                self._remove(part)
                return 422, {"error": "checksum mismatch: the file was damaged or changed while it was sent"}
            try:
                dest = ms.abs_path(self.models_dir, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                os.replace(part, dest)                                # same disk (the stage is inside models/): atomic
                stt = os.stat(dest)
            except OSError as e:
                self._remove(part)
                return 500, {"error": f"could not put the file in place: {e}"}
            self.index.put(rel, stt.st_size, stt.st_mtime_ns, digest)
            self.index.save()
            self.log(f"[ComfyCluster] model received: {rel}")
            return 200, {"ok": True, "have": total, "done": True}

    def _drop(self, rel):
        self._st.pop(rel, None)
        self._remove(os.path.join(self.stage, ms.stage_key(rel) + ".part"))

    @staticmethod
    def _remove(path):
        try:
            os.remove(path)
        except OSError:
            pass
