import os
import re
import socket
import threading
import time

ENABLED = os.environ.get("RADIO_ENABLED", "no").strip().lower() in ("yes", "true", "1", "on")
HOST = os.environ.get("RADIO_HOST", "127.0.0.1").strip()
PORT = int(os.environ.get("RADIO_PORT", "4532"))
TUNE_MODE = os.environ.get("RADIO_TUNE_MODE", "USB").strip().upper()

RPRT_RE = re.compile(r"^RPRT\s*(-?\d+)$")
VFO_PREFIX_RE = re.compile(r"^(?:currVFO|VFO[A-Z]):\s*")

_state = {"freq_hz": None, "source": None, "ts": 0}
_lock = threading.RLock()
_sock = None
_buf = None


def set_state(freq_hz, source):
    with _lock:
        _state.update(freq_hz=int(freq_hz), source=source, ts=time.time())


def get_state():
    with _lock:
        return dict(_state)


def _connect():
    global _sock, _buf
    s = socket.create_connection((HOST, PORT), timeout=3)
    s.settimeout(4)
    _sock = s
    _buf = s.makefile("r", encoding="ascii", newline="\n")


def _close():
    global _sock, _buf
    try:
        if _sock:
            _sock.close()
    except OSError:
        pass
    _sock = None
    _buf = None


def _readline():
    line = _buf.readline()
    if not line:
        raise ConnectionError("rigctld closed connection")
    return VFO_PREFIX_RE.sub("", line.strip())


def _exchange(line):
    """Send one command line, return one reply line. Reconnects once on failure."""
    with _lock:
        last_err = None
        for attempt in range(2):
            try:
                if _sock is None:
                    _connect()
                _sock.sendall((line + "\n").encode("ascii"))
                return _readline()
            except (OSError, ConnectionError) as e:
                last_err = e
                _close()
                time.sleep(0.2)
        raise ConnectionError(f"rigctld unreachable at {HOST}:{PORT}: {last_err}")


def set_cmd(line):
    """Run a set command (F/M/...). Returns True when rigctld reports success."""
    r = _exchange(line)
    m = RPRT_RE.match(r)
    if not m:
        return not r
    return int(m.group(1)) == 0


def get_freq():
    """Query current VFO frequency in Hz. Updates state as 'controller'."""
    r = _exchange("f")
    try:
        hz = int(r)
    except ValueError:
        raise ConnectionError(f"unexpected rigctld reply: {r!r}")
    set_state(hz, "controller")
    return hz


def tune(freq_hz):
    """Optionally set TUNE_MODE first (passband 0 = rig default), then tune."""
    ok = True
    if TUNE_MODE:
        ok = set_cmd(f"M {TUNE_MODE} 0")
    if ok:
        ok = set_cmd(f"F {int(freq_hz)}")
    if ok:
        set_state(freq_hz, "commanded")
    else:
        raise ConnectionError("rigctld rejected command (RPRT error)")


def poll_loop():
    while True:
        try:
            if ENABLED:
                get_freq()
        except Exception:
            pass
        time.sleep(2)


def start():
    if not ENABLED:
        return
    threading.Thread(target=poll_loop, daemon=True).start()
