"""Controller side of the model sync protocol (CMS1): sends every model the controller has to every worker.

    controller models/ --scan + hash--> compare with each worker's inventory --> push what is missing or different

The controller is the source of truth: a worker file with the same path but different content is replaced, a file that
only exists on a worker is left alone (nothing is ever deleted). Workers are served in parallel; each worker gets its
files one at a time, in slices of CHUNK bytes, and a dropped connection resumes where it stopped.
"""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

import modelsync as ms

RETRIES = 6


class PushError(Exception):
    """A file could not be sent and retrying will not help (the worker refused it)."""


class Stopped(Exception):
    pass


class SyncManager:
    def __init__(self, models_dir, index_path, workers_fn, log=print, interval=20, auto=True):
        self.models_dir, self.workers_fn, self.log, self.interval = models_dir, workers_fn, log, interval
        self.index = ms.HashIndex(index_path)
        self.auto = auto
        self._wake, self._now, self._stop = threading.Event(), False, threading.Event()
        self._lock = threading.Lock()
        self._busy = set()
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="cc-push")
        self.ctl = {"state": "idle", "models": 0, "bytes": 0, "skipped": [], "waiting": 0}
        self.workers = {}                       # name -> status dict (see _set)
        self.last_cycle = 0.0

    # ------------------------------------------------------------------ control
    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="cc-sync").start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def wake(self):
        """Look again soon (a model was just uploaded or downloaded)."""
        self._wake.set()

    def run_now(self):
        self._now = True
        self._wake.set()

    def set_auto(self, on):
        self.auto = bool(on)
        if on:
            self._wake.set()

    def forget(self, name):
        with self._lock:
            self.workers.pop(name, None)

    def snapshot(self):
        with self._lock:
            return {"auto": self.auto, "controller": dict(self.ctl),
                    "workers": {k: dict(v) for k, v in self.workers.items()}}

    # ------------------------------------------------------------------ status bookkeeping
    def _set(self, name, **kw):
        with self._lock:
            self.workers.setdefault(name, {"state": "idle"}).update(kw, updated=time.time())

    # ------------------------------------------------------------------ the loop
    def _loop(self):
        delay = 2
        while not self._stop.is_set():
            self._wake.wait(delay)
            self._wake.clear()
            if self._stop.is_set():
                return
            if not (self.auto or self._now):
                delay = self.interval
                continue
            self._now = False
            try:
                waiting = self._cycle()
            except Exception as e:  # noqa: BLE001  (the loop must survive anything)
                self.log(f"Model sync: {type(e).__name__}: {e}")
                waiting = 0
            delay = 5 if waiting else self.interval

    def _cycle(self):
        """One pass: hash what is new, then start a push for every worker that is not already being served.
        Returns True when something is still settling and a quick second look is worthwhile."""
        self._set_ctl(state="hashing")
        waiting = ms.hash_missing(self.models_dir, self.index, settle=ms.SETTLE_SECONDS, stop=self._stop.is_set)
        files, skipped = ms.inventory(self.models_dir, self.index)
        ready = [f for f in files if f["sha256"]]
        self._set_ctl(state="idle", models=len(files), bytes=sum(f["size"] for f in files),
                      skipped=skipped[:5], waiting=waiting)
        self.last_cycle = time.time()

        known = set()
        for w in self.workers_fn():
            known.add(w["name"])
            if not (w.get("online") and w.get("enabled", True)):
                self._set(w["name"], state="idle", note="not connected")
                continue
            if not w.get("key"):
                self._set(w["name"], state="nokey",
                          note="Run Worker.ipynb again and paste its new code to turn on model sync.")
                continue
            with self._lock:
                if w["name"] in self._busy:
                    continue
                self._busy.add(w["name"])
            self._pool.submit(self._serve, dict(w), ready, len(files), waiting)
        with self._lock:
            for gone in [n for n in self.workers if n not in known]:
                del self.workers[gone]
        return bool(waiting)

    def _set_ctl(self, **kw):
        with self._lock:
            self.ctl.update(kw)

    # ------------------------------------------------------------------ one worker
    def _serve(self, w, ready, total_models, ctl_waiting):
        name = w["name"]
        try:
            self._sync_worker(w, ready, total_models, ctl_waiting)
        except Stopped:
            self._set(name, state="idle")
        except PushError as e:
            self.log(f"Model sync to {name}: {e}")
            self._set(name, state="error", error=str(e), file="", done=0, total=0)
        except requests.RequestException as e:
            self._set(name, state="error", error=f"Cannot reach the worker ({type(e).__name__}).", file="", done=0, total=0)
        except Exception as e:  # noqa: BLE001
            self.log(f"Model sync to {name}: {type(e).__name__}: {e}")
            self._set(name, state="error", error=f"{type(e).__name__}: {e}"[:200], file="", done=0, total=0)
        finally:
            with self._lock:
                self._busy.discard(name)

    @staticmethod
    def _headers(key):
        return {ms.KEY_HEADER: key}

    def _remote_inventory(self, w):
        r = requests.get(w["url"].rstrip("/") + "/comfycluster/sync/inventory", headers=self._headers(w["key"]), timeout=45)
        if r.status_code == 404:
            raise PushError("This worker has no model sync yet. Run Worker.ipynb again and paste its new code.")
        if r.status_code in (401, 403):
            raise PushError("The worker refused the sync key. Paste its latest pairing code again.")
        if not r.ok:
            raise PushError(f"The worker answered with error {r.status_code}.")
        d = r.json()
        if d.get("protocol") != ms.PROTOCOL:
            raise PushError("The worker speaks a different sync version. Run Worker.ipynb again.")
        return d

    def diff(self, ready, remote):
        """-> (files to send, how many are the same on both, how many wait for the worker to finish hashing)"""
        have = {f["path"]: f for f in remote.get("files", [])}
        todo, same, waiting = [], 0, 0
        for f in ready:
            rf = have.get(f["path"])
            if rf and rf.get("size") == f["size"]:
                if not rf.get("sha256"):
                    waiting += 1                           # same size, hash not known yet: wait, do not guess
                    continue
                if rf["sha256"] == f["sha256"]:
                    same += 1
                    continue
            todo.append(f)
        todo.sort(key=lambda f: f["size"])                 # small ones first: they are usable sooner
        return todo, same, waiting

    def _sync_worker(self, w, ready, total_models, ctl_waiting):
        name = w["name"]
        self._set(name, state="checking", error="", note="")
        remote = self._remote_inventory(w)
        todo, same, waiting = self.diff(ready, remote)
        free, errors, sent = remote.get("free") or 0, [], 0
        queue_total = len(todo)
        self._set(name, models=total_models, synced=same, queue=queue_total, file="", done=0, total=0)
        if todo:
            self.log(f"Model sync: {name} needs {queue_total} file(s)")
        for i, f in enumerate(todo):
            if self._stop.is_set():
                raise Stopped
            if f["size"] + ms.RESERVE > free:
                errors.append(f"No space on {name} for {f['path']}")
                continue
            self._set(name, state="syncing", file=f["path"], done=0, total=f["size"], queue=queue_total - i)
            try:
                self._push_file(w, f)
            except PushError as e:
                errors.append(f"{f['path']}: {e}")
                continue
            except FileNotFoundError:
                continue                                    # deleted while queued
            free -= f["size"]
            sent += 1
            self._set(name, synced=same + sent)
            self.log(f"Model sync: {f['path']} -> {name}")
        left = queue_total - sent
        if errors:
            self._set(name, state="error", error=errors[0] + (f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""),
                      file="", done=0, total=0, queue=left)
        else:
            pending = waiting + ctl_waiting
            self._set(name, state="waiting" if pending else "synced", error="", file="", done=0, total=0, queue=0,
                      synced=same + sent, note="Waiting for new files to finish writing." if pending else "")
            if sent:
                self.log(f"Model sync: {name} is up to date")

    # ------------------------------------------------------------------ one file
    def _push_file(self, w, f):
        rel, size, name = f["path"], f["size"], w["name"]
        full = ms.abs_path(self.models_dir, rel)
        st = os.stat(full)
        if (st.st_size, st.st_mtime_ns) != (size, f["mtime_ns"]):
            raise PushError("it changed while waiting to be sent; it will be sent again")
        url = w["url"].rstrip("/") + "/comfycluster/sync/chunk"
        headers = dict(self._headers(w["key"]), **{"Content-Type": "application/octet-stream"})
        offset, fails, resyncs = 0, 0, 0
        with open(full, "rb") as fh:
            while offset < size:
                if self._stop.is_set():
                    raise Stopped
                fh.seek(offset)
                data = fh.read(min(ms.CHUNK, size - offset))
                if not data:
                    raise PushError("the file got shorter while it was being sent")
                try:
                    r = requests.post(url, params={"path": rel, "offset": offset, "total": size, "sha256": f["sha256"]},
                                      data=data, headers=headers, timeout=(15, 300))
                except requests.RequestException:
                    fails += 1
                    if fails > RETRIES:
                        raise
                    time.sleep(min(30, 2 ** fails))
                    continue
                if r.status_code == 200:
                    fails = 0
                    offset += len(data)
                    self._set(name, done=offset)
                elif r.status_code == 409:                  # the worker is somewhere else (a slice arrived but the reply was lost)
                    resyncs += 1
                    if resyncs > 10:
                        raise PushError("the worker keeps losing its place")
                    try:
                        offset = max(0, min(size - 1, int(r.json().get("have", 0))))
                    except (ValueError, TypeError):
                        offset = 0
                    self._set(name, done=offset)
                elif r.status_code in (400, 401, 403, 404, 413, 422, 507):
                    try:
                        msg = r.json().get("error", "")
                    except ValueError:
                        msg = ""
                    raise PushError(msg or f"the worker refused it ({r.status_code})")
                else:                                       # 5xx, tunnel hiccups: try again
                    fails += 1
                    if fails > RETRIES:
                        raise PushError(f"the worker answered with error {r.status_code}")
                    time.sleep(min(30, 2 ** fails))
        # If the file had changed while it was being read, the checksum announced at the start would not match and the
        # worker would have refused the last slice (422), so a file that arrives is always the one that was hashed.
