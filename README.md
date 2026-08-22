# BCNscanner

Dockerized web application for monitoring the **MMMonVHF 2m beacon list** with DXCC country lookup, distance/bearing calculation from your own QRA locator, antenna rotator control and rigctld radio tuning — all from a single light-themed dashboard.

The top header shows the live rig frequency and antenna azimuth next to the title; before hardware connects it displays demo values (**144.400 MHz / 180°**).

Live beacon data is pulled from [mmmonvhf.de](https://www.mmmonvhf.de/bcn.php), DXCC entities come from [country-files.com](https://www.country-files.com/) (`cty.dat`), both auto-refreshed every 12h with bundled fallback copies.

## Features

- **Beacon table** — frequency, callsign, DXCC country, continent, locator, distance, bearing, ERP, status badge, comment; sortable columns, live search
- **Two-column layout** — radar scope and beacon table sit side by side, each with its own panel header ("Radar" / "Beacon list"); stacks vertically below 900px
- **Header readouts** — rig frequency and antenna azimuth beside the BCNscanner title (same monospace style), tab-separated; amber italic when manually commanded
- **Continent filter** — clickable pills (EU default view) with per-continent counts
- **Status switches** — `noP` / `noU` / `noX` / `noT` hide Proposed / Unknown / Off-air / Testing beacons; all filters persist in the browser
- **Distance limiter** — `dist` button caps the list/radar to a max range (100–2000 km); left-click widens, right-click narrows
- **Radar scope** — beacons plotted around your grid by bearing/distance, range rings, antenna beam cone (±30°), tuned-beacon highlight
- **Rotator control** — click any bearing (table cell or radar dot) to turn the antenna; live azimuth display polled from the controller
- **Radio tuning** — click any frequency to tune the radio via Hamlib `rigctld` (mode fixed to USB by default)
- **DXCC engine** — longest-prefix matching against full `cty.dat` incl. `=FULLCALL` exceptions and zone-override tags

## Requirements

- Docker + Docker Compose
- Outbound internet (beacon/DXCC refresh); everything else is local
- Optional hardware: PstRotator or GS-232 rotator interface, radio with `rigctld`

## Quick Start

```bash
cd BCNscanner
docker compose up -d --build
```

Open **http://localhost:8083**.

Hardware control is disabled until you opt in via `.env` (see below).

## Configuration

Create a `.env` next to `docker-compose.yml`:

```env
# Home locator for distance/bearing (wwl)
LISTENING_GRID=JN76JG

# Rotator (same backends as CTESTKST)
ROTATOR_ENABLED=yes
ROTATOR_TYPE=pstrotator-http   # pstrotator-http | pstrotator | gs232
ROTATOR_HOST=10.147.17.32
ROTATOR_PORT=80                # http port | UDP cmd port | TCP port
ROTATOR_CMD_PORT=12000         # pstrotator UDP command port (reports on +1)
ROTATOR_TCP_PORT=2000          # gs232 raw TCP port

# Radio via rigctld
RADIO_ENABLED=yes
RADIO_HOST=10.147.17.32         # host running rigctld (shack box)
RADIO_PORT=4532                 # rigctld default port
RADIO_TUNE_MODE=USB            # mode set before each tune; empty = don't touch mode
```

| Variable | Default | Description |
|----------|---------|-------------|
| `LISTENING_GRID` | `JN76JG` | Your QRA locator, drives QRB/azimuth |
| `ROTATOR_ENABLED` | `no` | Master switch for /api/rotator |
| `ROTATOR_TYPE` | `pstrotator-http` | `pstrotator-http`, `pstrotator` (UDP), `gs232` (TCP) |
| `ROTATOR_HOST` | `10.147.17.32` | Controller address |
| `ROTATOR_PORT` | `80` | Per-type port (see above) |
| `RADIO_ENABLED` | `no` | Master switch for /api/radio |
| `RADIO_HOST` / `RADIO_PORT` | `127.0.0.1` / `4532` | rigctld endpoint |
| `RADIO_TUNE_MODE` | `USB` | Mode applied before every tune |

## Services

| Service | Container | Port | Description |
|---------|-----------|------|-------------|
| web | `bcnscanner-web` | **8083** → 80 | nginx: static UI + `/api/` proxy |
| scanner | `bcnscanner-api` | internal :5000 | Flask/gunicorn API, wwl, rotator + rigctld clients |

## Architecture

```
Browser ──:8083──> nginx ──> Flask API (:5000)
                               │
                 app.py        │  mmmonvhf.de CSV ──> beacon list (12h refresh)
                  │            │  country-files cty.dat ──> DXCC match
                  ├── wwl ──> qrb/azimuth from LISTENING_GRID
                  │
                  ├── rotator.py ──> PstRotator HTTP/UDP or GS-232 TCP
                  └── radio.py    ──> rigctld TCP ──> radio
```

- Bundled fallback copies of `bcn_2m.csv` and `cty.dat` live in `scanner/data/`; fresh downloads overwrite them on successful refresh.
- Rotator/radio positions are cached in-process (`controller` = reported by hardware, `commanded` = last sent only), UI polls every 2s.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/beacons` | Full filtered-source beacon list with DXCC, continent, qrb |
| GET | `/api/status` | Counts, continents, DXCC entities, grid, update time, subsystem states |
| GET | `/api/rotator` | Antenna azimuth + source (`controller`/`commanded`) |
| POST | `/api/rotator` | Turn antenna: `{"bearing": 180}` |
| GET | `/api/radio` | VFO frequency + source, rigctld endpoint info |
| POST | `/api/radio` | Tune: `{"freq_mhz": 144.405}` (`freq_khz`/`freq_hz` also accepted) |

Hardware endpoints return `400 {"error":"... disabled"}` unless enabled in config.

## UI Usage

- **Header readouts** show frequency (from rigctld) and antenna bearing (from the rotator controller) next to the title; demo values `144.400 MHz` / `180°` are displayed until hardware reports
- **roto** toggle arms antenna control (off by default): click a bearing cell or radar dot to turn; type azimuth in header box + Enter for manual turns; amber italic = commanded but not yet confirmed
- **tune** toggle arms radio control: click a MHz cell to tune; tuned row highlighted blue
- **dist** button limits beacons to a maximum distance: left-click steps the range up (`all → 100 → … → 2000 km`, wraps), right-click steps it down; label shows the active cap and the setting survives reloads
- Radar dots show call/DXCC/km/bearing on hover; beam cone follows reported azimuth
- All filter/toggle states survive reloads (browser localStorage)

## Beacon Status Codes

From MMMonVHF: `O` Operational · `T` Testing · `P` Proposed · `X` Not operational · `U` Unknown

## Commands

```bash
docker compose up -d --build   # start / rebuild after changes
docker compose down            # stop
docker logs -f bcnscanner-api  # API logs
```

## Security Notes

- No authentication on the web UI — keep it on LAN/ZeroTier or add nginx basic auth before exposing it
- Hardware endpoints are off by default and must be enabled explicitly; arming toggles in the UI are convenience only, not access control

## Credits

- Beacons: [MMMonVHF beacon project](https://www.mmmonvhf.de/bcn.php)
- DXCC data: [country-files.com](https://www.country-files.com/) (K1EA's cty.dat)
- QRB/azimuth: [`wwl`](https://packages.debian.org/stable/wwl)
- Radio control: [Hamlib rigctld](https://hamlib.github.io/)
