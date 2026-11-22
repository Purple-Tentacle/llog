# llog

A very simple Linux terminal logger for amateur radio contests.
One Python file, standard library only, nothing to compile.

- Callsign entry with QRZ.com lookup (XML API, needs a QRZ XML subscription for full data)
- Live frequency and mode from the radio through Hamlib's `rigctld`
- Typing a frequency in the logger tunes the radio
- SQLite log file, ADIF, Cabrillo and STF export

Dupe checking and scoring are not implemented yet.

## Requirements

- Python 3.9 or newer
- Hamlib (`rigctld`), e.g. `sudo apt install libhamlib-utils`

## Usage

```
# Icom IC-705 over USB (2 m / 70 cm, e.g. WNA and WSA)
rigctl -l | grep 705                          # find the Hamlib model number
rigctld -m <model> -r /dev/ttyACM0 &

# Elecraft KX2 with KXUSB cable (HF)
rigctl -l | grep -i kx2
rigctld -m <model> -r /dev/ttyUSB0 -s 38400 &

python3 llog.py wsa-2026-12.db          # one log file per contest
```

The first start asks for your callsign, the exchange you send, your QRZ login
and the log header data, and stores them in
`~/.config/llog/config.ini` (mode 600). The QRZ password is stored there
in plain text; the environment variables `QRZ_USERNAME` and `QRZ_PASSWORD`
override the file.

Without a running `rigctld` the logger still works; a typed frequency is then
used for the log only.

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
python3 llog.py mylog.db --export-adif out.adi --export-cabrillo out.cbr --export-stf out.stf
```

## STF export

The STF export was written from a published example file, not from the
format specification, and has not been checked by a contest organiser.
Bands are written in metres (`2`) and 70 cm as `70`; empty fields as `-`.
Send a test log to the organiser before relying on it, or use Cabrillo.

## Exchange

`exchange_sent` in the config is a fixed text (for example your DOK) or `#`
for a running serial number. The received exchange is a free-text field.

## Test

```
python3 test/run_test.py
```

Runs the real UI in a pseudo-terminal against a simulated `rigctld`.
The QRZ lookup is only tested against a sample XML response.
