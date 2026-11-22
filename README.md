# llog

A very simple Linux terminal logger for amateur radio contests.
One Python file, standard library only, nothing to compile.

- Callsign entry with QRZ.com lookup (XML API, needs a QRZ XML subscription for full data)
- Live frequency and mode from the radio through Hamlib's `rigctld`
- Typing a frequency in the logger tunes the radio
- SQLite log file, ADIF, Cabrillo and STF export

Dupe checking is configurable per contest. Scoring is not implemented yet.

## Requirements

- Python 3.9 or newer
- Hamlib (`rigctld`), e.g. `sudo apt install libhamlib-utils`

## Usage

Start `rigctld` for your radio, then start llog with a contest name:

```
# Icom IC-705 over USB (2 m / 70 cm, e.g. WNA and WSA)
rigctl -l | grep 705                          # find the Hamlib model number
rigctld -m <model> -r /dev/ttyACM0 &

# Elecraft KX2 with KXUSB cable (HF)
rigctl -l | grep -i kx2
rigctld -m <model> -r /dev/ttyUSB0 -s 38400 &

python3 llog.py wsa                 # log file wsa.db in the current directory
python3 llog.py wsa wsa-2026-12.db  # explicit log file, e.g. one per month
python3 llog.py                     # lists the configured contests
```

Without a running `rigctld` the logger still works; a typed frequency is then
used for the log only.

## Configuration

Station data and contest data live in separate files under `~/.config/llog/`:

| File | Content |
| --- | --- |
| `config.ini` | Callsign, e-mail, QRZ login, rigctld host and port |
| `contests/<name>.ini` | Contest name, exchange you send, operator category, `SPECIFIC` header, dupe rules |

Both are created by a few questions the first time they are needed: the
station file on the very first start, a contest file the first time you start
llog with a new contest name. Copy or edit the `.ini` files to change them.
An `email` entry in a contest file overrides the station e-mail.

The QRZ password is stored in `config.ini` in plain text (file mode 600); the
environment variables `QRZ_USERNAME` and `QRZ_PASSWORD` override the file.

## Dupe checking

While you type a callsign, llog shows a red `DUPE` line with the earlier QSO.
A dupe is not blocked: the first Enter warns, a second Enter logs it anyway.

The rules are set in the contest file:

| Option | Values | Meaning |
| --- | --- | --- |
| `dupe_check` | `band` (default) | A station counts once per band |
| | `band_mode` | Once per band and mode class |
| | `mode` | Once per mode class, on any band |
| | `once` | Once in the whole contest |
| | `off` | No dupe check |
| `dupe_ignore_suffix` | `no` / `yes` | `yes`: `DL1ABC/P` and `DL1ABC` are the same station |
| `dupe_exchange` | `no` / `yes` | `yes`: a station counts again when it sends a different exchange (rovers and mobiles that changed location) |
| `dupe_minutes` | `0` or a number | A station may be worked again after this many minutes; `0` = never |

Mode classes are CW, phone (SSB, FM and AM together) and digital.

Settings for some contests:

| Contest | Setting |
| --- | --- |
| WNA, WSA, CQ WW, CQ WPX, ARRL RTTY Roundup | `dupe_check = band` |
| IARU HF Championship | `dupe_check = band_mode` |
| ARRL November Sweepstakes | `dupe_check = once` |
| ARRL 10 GHz and Up | `dupe_check = band`, `dupe_exchange = yes` |

Always check the current rules of the contest you enter.

Example `contests/wsa.ini`:

```
[contest]
contest = WSA
exchange_sent = N99
category_operator = SINGLE-OP
specific = N99
dupe_check = band
dupe_ignore_suffix = no
dupe_exchange = no
dupe_minutes = 0
```

## Keys

| Key | Action |
| --- | --- |
| Enter | Log the QSO |
| Tab / Shift-Tab | Next / previous field |
| Space | Jump from callsign to exchange |
| Esc | Clear the entry line |
| `<frequency>` Enter | QSY (`7030.5` = kHz, below 1000 = MHz, e.g. `144.3`) |
| `CW` / `SSB` / `FM` / `AM` / `RTTY` Enter | Set mode |
| F2 | Export ADIF (`<logfile>.adi`) |
| F3 | Export Cabrillo (`<CALL>-<date>-1.cbr`) |
| F4 | Export STF (`<CALL>_<MM>_<YYYY>_<contest>.stf`) |
| F8 | Delete last QSO |
| F10 or Ctrl-X | Quit |

Exports also work without the UI:

```
python3 llog.py wsa wsa-2026-12.db --export-adif out.adi --export-cabrillo out.cbr --export-stf out.stf
```

## STF export

The STF export was written from a published example file, not from the
format specification, and has not been checked by a contest organiser.
Bands are written in metres (`2`) and 70 cm as `70`; empty fields as `-`.
Send a test log to the organiser before relying on it, or use Cabrillo.

## Exchange

`exchange_sent` in the contest file is a fixed text (for example your DOK) or `#`
for a running serial number. The received exchange is a free-text field.

## Test

```
python3 test/run_test.py
```

Runs the real UI in a pseudo-terminal against a simulated `rigctld`.
The QRZ lookup is only tested against a sample XML response.
