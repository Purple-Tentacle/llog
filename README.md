# llog

A very simple Linux terminal logger for amateur radio contests.
One Python file, standard library only, nothing to compile.

- Callsign entry with QRZ.com lookup (XML API, needs a QRZ XML subscription for full data)
- Live frequency and mode from the radio through Hamlib's `rigctld`
- Typing a frequency in the logger tunes the radio
- Two entry rows with their own frequency and mode
- DX cluster panel with the spots on the current band
- SQLite log file, ADIF and Cabrillo export

Dupe checking and scoring are configurable per contest.

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

## Two entry rows (A and B)

There are two entry rows. Each row has its own frequency and mode and keeps
its own callsign and exchange, so a half-typed QSO in row A stays there while
you work someone in row B. `\` switches to the other row. A QSO is logged
with the frequency and mode of the row it was entered in, and typing a
frequency or mode changes the current row only.

How the radio follows a row switch is set by `vfo_switch` in the `[rig]`
section of `config.ini`:

| Value | Behaviour |
| --- | --- |
| `retune` (default) | llog remembers frequency and mode of both rows and retunes the radio's active VFO when you switch. The radio's own VFO B is never touched. A row you have not used yet starts on the current frequency. Works on every radio. |
| `vfo` | Row A is the radio's VFO A and row B its VFO B; switching sends a VFO select to the radio. Only for radios where VFO B can become the active VFO. With `rigctld --vfo` both VFOs are read continuously, otherwise only the active one. |

Use `retune` for Elecraft radios (KX2, KX3, K3): there VFO A is always the
active VFO and VFO B is only the split transmit frequency, so `vfo` does not
work.

Both of your rows share one receiver; this is quick switching between two
frequencies or modes, not listening to both.

## ATU tune

F5 starts an antenna tuner cycle on the radio, the same as pressing its ATU
button. The radio transmits a carrier while it tunes, so check the frequency
first. The command is chosen by `tune_command` in the `[rig]` section:

| Value | Command sent to rigctld |
| --- | --- |
| `auto` (default) | KX2: `w SWT20;`, KX3: `w SWT44;` (the radio's ATU button), any other radio: `G TUNE` (Hamlib's generic tune operation) |
| anything else | sent to rigctld as written, e.g. `w SWT20;` |

## DX cluster

With a cluster node configured, a panel to the right of the QSO list shows
the spots on the band of the current entry row, newest first: frequency,
callsign and spot time (UTC). It follows the band when you QSY or switch
rows. Stations you have already worked on that band (by the contest's dupe
rules) are shown dimmed.

PgDn and PgUp move a highlight through the spots. Enter in an empty call
field takes the highlighted spot: the current row is tuned to its frequency
and the callsign is filled in. Esc removes the highlight.

Add the node to `config.ini`:

```
[cluster]
host = dxspider.co.uk
port = 7300
minutes = 30
call =
```

llog connects by telnet, logs in with `call` (your station callsign if left
empty; some nodes want a login like `DA2IT-1` for a second connection), asks the node for its
last 50 spots (`sh/dx 50`) and then collects new spots as they arrive. Spots
of all bands are kept in memory for `minutes`, so changing band only changes
what is displayed; nothing is fetched again. The panel needs a terminal at
least 126 columns wide; on narrower terminals it is hidden. Leave `host`
empty to switch the cluster off.

## Configuration

Station data and contest data live in separate files under `~/.config/llog/`:

| File | Content |
| --- | --- |
| `config.ini` | Callsign, e-mail, QRZ login, rigctld host and port, `vfo_switch`, `tune_command`, DX cluster node |
| `contests/<name>.ini` | Contest name, exchange you send, operator category, `SPECIFIC` header, dupe rules, scoring |

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

## Scoring

Scoring is set in the contest file as well:

| Option | Example | Meaning |
| --- | --- | --- |
| `points` | `FM:2, SSB:4, CW:6` | QSO points per mode. Keys are a mode (`CW`, `SSB`, `FM`, `AM`, `RTTY`), a mode class (`PH`, `DIG`) or `*` for everything else. Modes without an entry score 0. Empty = 1 point per QSO. |
| `multipliers` | `N[0-9][0-9], WN, DVN` | Received exchanges that count as a multiplier. Entries are exact values or patterns (`?` one character, `*` any text, `[0-9]` one digit). `*` alone makes every different exchange a multiplier. Empty = no multipliers. |
| `mult_per` | `band` (default) or `contest` | Whether a multiplier counts once per band or once in the whole contest |

The score is the sum of QSO points times the number of multipliers, or just
the points when no multipliers are configured. Dupes (according to the dupe
rules) get no points and no multiplier.

With scoring configured, llog shows the score in the header, the points of
each QSO in the list (`+M` marks a new multiplier), a green `NEW MULTIPLIER`
line while you type an exchange that is new on the band, and writes
`CLAIMED-SCORE` into the Cabrillo file. `python3 llog.py wsa wsa.db --score`
prints the score per band.

Ready-made contest files for WNA and WSA are in `examples/`. Copy them to
`~/.config/llog/contests/` and enter your DOK:

```
mkdir -p ~/.config/llog/contests
cp examples/wna.ini examples/wsa.ini ~/.config/llog/contests/
```

Multipliers are shown in a panel below the QSO list. Those named explicitly
in the contest file (`WN`, `O05`, ...) are always listed, with the ones
already worked highlighted in green. Multipliers given as a pattern such as
`N[0-9][0-9]` are not listed in advance; each one appears in the panel, in
green, once it has been worked. With `mult_per = band` the panel shows the
band the radio is on. The panel follows edits and deletions of QSOs.

The score is your claimed score; the organiser's evaluation is what counts.

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
| `\` | Switch to the other entry row (A/B) |
| PgDn / PgUp | Highlight a DX cluster spot; Enter in an empty call field takes it |
| F5 | Start an ATU tune cycle on the radio (it transmits a carrier) |
| Up / Down | Edit a logged QSO in the list (see below) |
| F8 | Delete the last QSO, or the selected one while editing |
| F10 or Ctrl-X | Quit |

### Editing a QSO

Cursor Up moves an edit cursor into the QSO list; Up and Down move through
the log, and Down past the last QSO returns to QSO entry. The selected QSO is
edited in place, cell by cell: time (UTC, `2026-12-01 18:45:00`), callsign,
frequency (kHz), mode, both reports and both exchanges. Tab and Shift-Tab
move between the cells.

Enter saves, Esc cancels, F8 deletes the selected QSO. Moving to another QSO
with Up or Down discards unsaved changes. Whatever is typed in the entry rows
is left untouched while you edit. The log is kept in time order, so a QSO
whose time was changed moves to its new place in the list and exports.

Exports also work without the UI:

```
python3 llog.py wsa wsa-2026-12.db --export-adif out.adi --export-cabrillo out.cbr
```

## Exchange

`exchange_sent` in the contest file is a fixed text (for example your DOK) or `#`
for a running serial number. The received exchange is a free-text field.

## Test

```
python3 test/run_test.py
```

Runs the real UI in a pseudo-terminal against a simulated `rigctld`.
The QRZ lookup is only tested against a sample XML response.
