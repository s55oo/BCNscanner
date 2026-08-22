import csv
import functools
import io
import os
import re
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone

from flask import Flask, jsonify, request

import rotator
import radio

app = Flask(__name__)

VERSION = "dev"
_version_file = os.path.join(os.path.dirname(__file__), "VERSION")
if os.path.exists(_version_file):
    with open(_version_file) as f:
        VERSION = f.read().strip() or "dev"

BANDS = {
    "2m": {
        "url": "https://mmmonvhf.de/beacon/download/bcn_2m.csv",
        "file": os.path.join(os.path.dirname(__file__), "data", "bcn_2m.csv"),
    },
    # 6m: DL8WX beacon list (HTML table, needs its own parser - planned)
    "6m": {
        "url": "http://dl8wx.de/baken_50.htm",
        "file": os.path.join(os.path.dirname(__file__), "data", "baken_50.htm"),
    },
}

CTY = {
    "url": "https://www.country-files.com/cty/cty.dat",
    "file": os.path.join(os.path.dirname(__file__), "data", "cty.dat"),
}

STATUS_LABEL = {"O": "Operational", "T": "Testing", "P": "Proposed", "X": "Off air", "U": "Unknown"}
CONTINENTS = {"AF": "Africa", "AN": "Antarctica", "AS": "Asia",
              "EU": "Europe", "NA": "North America", "OC": "Oceania", "SA": "South America"}
GRID_RE = re.compile(r"^[A-R]{2}[0-9]{2}([A-X]{2})?$")
WWL_RE = re.compile(r"qrb:\s*(\d+)\s*kilometers,\s*azimuth:\s*(\d+)\s*degrees", re.I)
OVERRIDE_RE = re.compile(r"<\d+/\d+>|\[\d+\]|\{\d+\}")
REFRESH_SECONDS = 12 * 3600

LISTENING_GRID = os.environ.get("LISTENING_GRID", "JN76JG").strip().upper()
beacons = []
prefix_index = []
loaded_at = None
lock = threading.Lock()


def valid_grid(grid):
    return bool(grid and GRID_RE.match(grid))


@functools.lru_cache(maxsize=None)
def qrb(dx_grid):
    try:
        out = subprocess.run(
            ["wwl", LISTENING_GRID, dx_grid.upper()],
            capture_output=True, text=True, timeout=5,
        ).stdout
        m = WWL_RE.search(out)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return None


def parse_float(s):
    try:
        return float(str(s).replace(",", ".").replace(" ", ""))
    except ValueError:
        return None


def parse_cty(text):
    index = []
    for chunk in text.split(";"):
        lines = [l for l in chunk.strip().splitlines()
                 if l.strip() and not l.strip().startswith("#")]
        if not lines:
            continue
        head = [f.strip() for f in lines[0].split(":")]
        if len(head) < 4 or not head[0]:
            continue
        entity = {
            "name": head[0],
            "continent": head[3].strip().upper(),
            "lat": parse_float(head[4]) if len(head) > 4 else None,
            "lon": parse_float(head[5]) if len(head) > 5 else None,
        }
        for line in lines[1:]:
            for raw in line.split(","):
                p = raw.strip().upper()
                exact = p.startswith("=")
                p = re.sub(OVERRIDE_RE, "", p.lstrip("="))
                if not p:
                    continue
                index.append((p, exact, entity))
    return sorted(index, key=lambda e: -len(e[0]))


def dxcc_lookup(call):
    call = call.strip().upper()
    tokens = call.split("/") if "/" in call else [call]
    candidates = [t for t in tokens if t] + [call]
    for token in candidates:
        for prefix, exact, entity in prefix_index:
            if exact:
                if token == prefix:
                    return entity
            elif token.startswith(prefix):
                return entity
    return None


def load_cty():
    global prefix_index
    text = None
    try:
        text = fetch_url(CTY["url"])
        os.makedirs(os.path.dirname(CTY["file"]), exist_ok=True)
        with open(CTY["file"], "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        if os.path.exists(CTY["file"]):
            with open(CTY["file"], encoding="utf-8") as f:
                text = f.read()
    if text:
        parsed = parse_cty(text)
        if parsed:
            prefix_index = parsed


def load_csv(text):
    rows = []
    reader = csv.reader(io.StringIO(text), delimiter=";")
    next(reader, None)
    for row in reader:
        if len(row) < 4:
            continue
        freq = parse_float(row[0])
        call = row[1].strip()
        locator = row[2].strip().upper()
        if not freq or not call or not valid_grid(locator):
            continue
        erp = parse_float(row[3]) if len(row) > 3 else None
        antenna = row[4].strip() if len(row) > 4 else ""
        qtf = row[5].strip() if len(row) > 5 else ""
        status = row[6].strip().upper() if len(row) > 6 and row[6].strip() else "U"
        if status not in STATUS_LABEL:
            status = "U"
        comment = row[7].strip() if len(row) > 7 else ""
        b = {
            "freq_mhz": freq,
            "call": call,
            "locator": locator,
            "erp_w": erp,
            "antenna": antenna,
            "qtf": qtf,
            "status": status,
            "status_label": STATUS_LABEL.get(status, status),
            "comment": comment,
        }
        entity = dxcc_lookup(call)
        if entity:
            b["dxcc"] = entity["name"]
            b["continent"] = entity["continent"]
        if valid_grid(LISTENING_GRID):
            dist, brg = (qrb(locator) or (None, None))
            b["distance_km"] = dist
            b["bearing_deg"] = brg
        rows.append(b)
    return rows


def fetch_url(url):
    req = urllib.request.Request(url, headers={"User-Agent": "BCNscanner/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def refresh(force_download=False):
    global beacons, loaded_at
    band = BANDS["2m"]
    text = None
    source = "bundled"
    try:
        text = fetch_url(band["url"])
        source = "mmmonvhf.de"
    except Exception:
        if force_download:
            raise
    if not text and os.path.exists(band["file"]):
        with open(band["file"], encoding="utf-8") as f:
            text = f.read()
    if not text:
        return
    parsed = load_csv(text)
    if parsed:
        with lock:
            beacons = sorted(parsed, key=lambda b: b["freq_mhz"])
            loaded_at = time.time()
    if source == "mmmonvhf.de" and text:
        os.makedirs(os.path.dirname(band["file"]), exist_ok=True)
        with open(band["file"], "w", encoding="utf-8") as f:
            f.write(text)


def refresher():
    while True:
        time.sleep(REFRESH_SECONDS)
        try:
            refresh()
            load_cty()
        except Exception:
            pass


load_cty()
refresh(force_download=False)
rotator.start()
radio.start()
threading.Thread(target=refresher, daemon=True).start()


@app.get("/api/status")
def status():
    counts = {}
    continents = {}
    for b in beacons:
        counts[b["status"]] = counts.get(b["status"], 0) + 1
        cont = b.get("continent")
        if cont:
            continents[cont] = continents.get(cont, 0) + 1
    return jsonify({
        "version": VERSION,
        "band": "2m",
        "source": BANDS["2m"]["url"],
        "listening_grid": LISTENING_GRID if valid_grid(LISTENING_GRID) else None,
        "count": len(beacons),
        "status_counts": counts,
        "continent_counts": continents,
        "dxcc_entities": len({b["dxcc"] for b in beacons if "dxcc" in b}),
        "rotator": {"enabled": rotator.ENABLED, "type": rotator.TYPE},
        "radio": {"enabled": radio.ENABLED, "tune_mode": radio.TUNE_MODE},
        "updated_utc": (
            datetime.fromtimestamp(loaded_at, timezone.utc).isoformat()
            if loaded_at else None
        ),
    })


@app.get("/api/beacons")
def get_beacons():
    with lock:
        return jsonify(beacons)


@app.get("/api/rotator")
def get_rotator():
    if not rotator.ENABLED:
        return jsonify({"error": "rotator disabled"}), 400
    st = rotator.get_state()
    if st["bearing"] is None:
        return jsonify({"error": "position unknown"}), 503
    return jsonify(st)


@app.post("/api/rotator")
def post_rotator():
    if not rotator.ENABLED:
        return jsonify({"error": "rotator disabled"}), 400
    try:
        bearing = float(request.get_json(force=True).get("bearing"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid bearing"}), 400
    if not 0 <= bearing <= 360:
        return jsonify({"error": "bearing out of range"}), 400
    reply = rotator.move(bearing)
    return jsonify({"ok": True, "bearing": int(bearing) % 360, "reply": reply})


@app.get("/api/radio")
def get_radio():
    if not radio.ENABLED:
        return jsonify({"error": "radio control disabled"}), 400
    st = radio.get_state()
    if st["freq_hz"] is None:
        return jsonify({"error": "position unknown"}), 503
    st.update(host=radio.HOST, port=radio.PORT, tune_mode=radio.TUNE_MODE)
    return jsonify(st)


@app.post("/api/radio")
def post_radio():
    if not radio.ENABLED:
        return jsonify({"error": "radio control disabled"}), 400
    data = request.get_json(force=True, silent=True) or {}
    if data.get("freq_mhz") is not None:
        hz = float(data["freq_mhz"]) * 1e6
    elif data.get("freq_khz") is not None:
        hz = float(data["freq_khz"]) * 1e3
    elif data.get("freq_hz") is not None:
        hz = float(data["freq_hz"])
    else:
        return jsonify({"error": "missing freq"}), 400
    if not 1e5 <= hz <= 1e10:
        return jsonify({"error": "frequency out of range"}), 400
    try:
        radio.tune(int(hz))
    except ConnectionError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"ok": True, "freq_hz": int(hz),
                    "mode": radio.TUNE_MODE or None})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
