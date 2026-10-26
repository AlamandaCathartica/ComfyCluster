"""Cloudflare quick tunnel (trycloudflare.com, free, no account) used by the controller and the workers."""
import os
import platform
import re
import shutil
import stat
import subprocess
import threading
import urllib.request

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
NOT_A_TUNNEL = {"https://api.trycloudflare.com"}  # cloudflared mentions its API host in log lines


def ensure_binary(cache_dir, log=print):
    found = shutil.which("cloudflared")
    if found:
        return found
    path = os.path.join(cache_dir, "cloudflared")
    if os.path.isfile(path) and os.access(path, os.X_OK):
        return path
    arch = "arm64" if platform.machine().lower() in ("aarch64", "arm64") else "amd64"
    url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{arch}"
    os.makedirs(cache_dir, exist_ok=True)
    log(f"Downloading cloudflared ({arch}) once; it is cached in {cache_dir}")
    tmp = path + ".part"
    urllib.request.urlretrieve(url, tmp)
    os.chmod(tmp, os.stat(tmp).st_mode | stat.S_IEXEC)
    os.replace(tmp, path)
    return path


def start(port, cache_dir, on_url, on_error=print, on_line=None):
    """Start the tunnel in the background. Calls on_url(public_url) once it is up and
    on_line(text) for every line cloudflared prints (so it can be shown in the GUI log)."""

    def run():
        try:
            exe = ensure_binary(cache_dir, on_error)
            proc = subprocess.Popen(
                [exe, "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True,
            )
            announced = False
            for line in proc.stdout:
                if on_line:
                    on_line(line.rstrip("\n"))
                if not announced:
                    for url in URL_RE.findall(line):
                        if url not in NOT_A_TUNNEL:
                            announced = True
                            on_url(url)
                            break
            on_error("cloudflared exited.")
        except Exception as e:  # noqa: BLE001
            on_error(f"Tunnel failed: {e}")

    threading.Thread(target=run, daemon=True).start()
