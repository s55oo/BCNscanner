import os
import re
import socket
import threading
import time
import xmlrpc.client

ENABLED = os.environ.get("RADIO_ENABLED", "no").strip().lower() in ("yes", "true", "1", "on")
TYPE = os.environ.get("RADIO_TYPE", "rigctld").strip().lower()
HOST = os.environ.get("RADIO_HOST", "127.0.0.1").strip()
PORT = int(os.environ.get("RADIO_PORT", "4532"))
TUNE_MODE = os.environ.get("RADIO_TUNE_MODE", "CW").strip().upper()
# Filter/passband in Hz selected together with TUNE_MODE when tuning
# (0 = rig default, e.g. TS-590SG CW default is 400 Hz).
TUNE_BW = int(os.environ.get("RADIO_TUNE_BW", "600") or 0)
# Informational rig model (e.g. TS-590SG). flrig handles the rig itself;
# relevant when using rigctld directly (hamlib model id / serial speed).
MODEL = os.environ.get("RADIO_MODEL", "").strip()
# Transverter LO offset in Hz added to the rig frequency (e.g. 130000000
# makes the TS-590SG's 14.174 MHz read as 144.174 MHz). Subtracted again
# when tuning.
FREQ_OFFSET = int(os.environ.get("RADIO_FREQ_OFFSET", "0") or 0)

RPRT_RE = re.compile(r"^RPRT\s*(-?\d+)$")
VFO_PREFIX_RE = re.compile(r"^(?:currVFO|VFO[A-Z]):\s*")

_state = {"freq_hz": None, "source": None, "ts": 0}
_lock = threading.RLock()
_sock = None
_buf = None
_xmlrpc = None


def set_state(freq_hz, source):
    with _lock:
        _state.update(freq_hz=int(freq_hz), source=source, ts=time.time())


def get_state():
    with _lock:
        return dict(_state)


# --- rigctld backend -------------------------------------------------------

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


def _rigctld_get_freq():
    r = _exchange("f")
    try:
        return int(r)
    except ValueError:
        raise ConnectionError(f"unexpected rigctld reply: {r!r}")


def _rigctld_tune(raw_hz):
    """Set TUNE_MODE with TUNE_BW passband first, then tune."""
    ok = True
    if TUNE_MODE:
        ok = set_cmd(f"M {TUNE_MODE} {TUNE_BW}")
    if ok:
        ok = set_cmd(f"F {int(raw_hz)}")
    if not ok:
        raise ConnectionError("rigctld rejected command (RPRT error)")


# --- flrig backend (XML-RPC) -----------------------------------------------

def _flrig():
    global _xmlrpc
    with _lock:
        if _xmlrpc is None:
            _xmlrpc = xmlrpc.client.ServerProxy(
                f"http://{HOST}:{PORT}/RPC2", allow_none=True)
        return _xmlrpc


def _flrig_reset():
    global _xmlrpc
    with _lock:
        _xmlrpc = None


def _flrig_get_freq():
    p = _flrig()
    try:
        v = p.rig.get_vfoA()
    except (OSError, xmlrpc.client.Error) as e:
        _flrig_reset()
        raise ConnectionError(f"flrig unreachable at {HOST}:{PORT}: {e}")
    try:
        return int(float(str(v).split()[0]))
    except (ValueError, IndexError):
        raise ConnectionError(f"unexpected flrig reply: {v!r}")


def _flrig_tune(raw_hz):
    p = _flrig()
    try:
        if TUNE_MODE:
            p.rig.set_modeA(TUNE_MODE)
            # Mode change resets the rig's stored filter -> set bw after mode
            if TUNE_BW:
                p.rig.set_verify_bandwidth(int(TUNE_BW))
        p.rig.set_frequency(float(int(raw_hz)))
    except (OSError, xmlrpc.client.Error) as e:
        _flrig_reset()
        raise ConnectionError(f"flrig unreachable at {HOST}:{PORT}: {e}")


# --- common -----------------------------------------------------------------

if TYPE == "flrig":
    _raw_get_freq = _flrig_get_freq
    _raw_tune = _flrig_tune
else:
    _raw_get_freq = _rigctld_get_freq
    _raw_tune = _rigctld_tune


def get_freq():
    """Query current VFO frequency in Hz (offset-corrected).

    Updates state as 'controller'."""
    hz = _raw_get_freq() + FREQ_OFFSET
    set_state(hz, "controller")
    return hz


def tune(freq_hz):
    """Tune to an offset-corrected RF frequency in Hz."""
    _raw_tune(int(freq_hz) - FREQ_OFFSET)
    set_state(freq_hz, "commanded")


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
