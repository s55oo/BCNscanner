import json
import os
import re
import socket
import threading
import time
import urllib.request

ENABLED = os.environ.get("ROTATOR_ENABLED", "no").strip().lower() in ("yes", "true", "1", "on")
TYPE = os.environ.get("ROTATOR_TYPE", "pstrotator-http").strip().lower() or "pstrotator-http"
HOST = os.environ.get("ROTATOR_HOST", "").strip()
PORT = int(os.environ.get("ROTATOR_PORT", "80"))

AZ_RE = re.compile(r"AZ\s*=\s*([+-]?\d+)|[+-]\d{3,4}")
PST_AZ_RE = re.compile(r"AZ\s*[:=]\s*([+-]?\d+(?:\.\d+)?)")
PST_WEB_AZ_RE = re.compile(r"(?:Bearing|AZ)\s*=\s*([+-]?\d+(?:\.\d+)?)")

_state = {"bearing": None, "source": None, "ts": 0}
_lock = threading.Lock()


def _cfg(key, default):
    return os.environ.get(key, default).strip()


def set_state(bearing, source):
    with _lock:
        _state.update(bearing=int(round(float(bearing))) % 360,
                      source=source, ts=time.time())


def get_state():
    with _lock:
        return dict(_state)


def _http_get(path):
    url = f"http://{HOST}:{PORT}{path}"
    with urllib.request.urlopen(url, timeout=4) as r:
        return r.read().decode("utf-8", "replace")


def move(bearing, el=None):
    """Point the antenna. PstRotator Web backend mirrors CTESTKST:
    command /PstRotator.htm?az=NNN&el=NNN (el omitted = unchanged)."""
    az = "%03d" % int(round(float(bearing)))
    if TYPE == "pstrotator-http":
        path = "/PstRotator.htm?az=" + az
        if el is not None:
            path += "&el=" + ("%03d" % int(round(float(el))))
        _http_get(path)
        set_state(az, "commanded")
        return ""
    if TYPE == "pstrotator":
        cmd_port = int(_cfg("ROTATOR_CMD_PORT", str(PORT)))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(2)
            s.sendto(f"<COMMANDS><AZIMUTH>{az}</AZIMUTH></COMMANDS>".encode(),
                     (HOST, cmd_port))
        set_state(az, "commanded")
        return ""
    cmd = _cfg("ROTATOR_CMD", r"M{bearing}\r").format(bearing=az)
    cmd = cmd.replace("\\r", "\r").replace("\\n", "\n").replace("\\t", "\t")
    reply = talk(cmd.encode("ascii", "replace"))
    set_state(az, "commanded")
    return reply.strip()


def talk(payload, window=0.8, stop_re=None):
    port = int(_cfg("ROTATOR_TCP_PORT", str(PORT)))
    reply = ""
    with socket.create_connection((HOST, port), timeout=2.0) as sock:
        sock.settimeout(0.3)
        if payload:
            sock.sendall(payload)
        end = time.time() + window
        while time.time() < end:
            try:
                chunk = sock.recv(512)
                if not chunk:
                    break
                reply += chunk.decode("utf-8", "replace")
                if stop_re and stop_re.search(reply):
                    break
            except socket.timeout:
                pass
    return reply


def read():
    """Query current azimuth from the controller. Returns int or None."""
    if TYPE == "pstrotator-http":
        page = _http_get("/PstRotator.htm?")
        m = PST_WEB_AZ_RE.search(page)
        if not m:
            return None
        az = int(float(m.group(1))) % 360
        set_state(az, "controller")
        return az
    if TYPE == "pstrotator":
        cmd_port = int(_cfg("ROTATOR_CMD_PORT", str(PORT)))
        rep_port = cmd_port + 1
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(1.6)
            try:
                s.bind(("0.0.0.0", rep_port))
            except OSError:
                pass
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as c:
                c.sendto(b"<COMMANDS><AZ?>1</AZ?></COMMANDS>", (HOST, cmd_port))
            end = time.time() + 1.6
            while time.time() < end:
                try:
                    data = s.recv(512).decode("utf-8", "replace")
                except socket.timeout:
                    continue
                except OSError:
                    break
                m = PST_AZ_RE.search(data)
                if m:
                    az = int(float(m.group(1))) % 360
                    set_state(az, "controller")
                    return az
        return None
    query = _cfg("ROTATOR_QUERY_CMD", r"C\r\n")
    query = query.replace("\\r", "\r").replace("\\n", "\n").replace("\\t", "\t")
    reply = talk(query.encode("ascii", "replace"), window=1.5, stop_re=AZ_RE)
    m = AZ_RE.search(reply)
    if not m:
        return None
    num = m.group(0).split("=")[-1].lstrip("+")
    az = int(num) % 360
    set_state(az, "controller")
    return az


def poll_loop():
    while True:
        try:
            if ENABLED:
                read()
        except Exception:
            pass
        time.sleep(2)


def start():
    if not ENABLED:
        return
    threading.Thread(target=poll_loop, daemon=True).start()
