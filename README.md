# llog

A very simple Linux terminal logger for amateur radio contests.
One Python file, standard library only, nothing to compile.

- Callsign entry with QRZ.com lookup (XML API, needs a QRZ XML subscription for full data)
- Live frequency and mode from the radio through Hamlib's `rigctld`
- Typing a frequency in the logger tunes the radio
- Two entry rows with their own frequency and mode
- DX cluster panel and band map for the current band, with stations stored for later
- Locator field with distance to each station
- "Worked before" information from an ADIF logbook file
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

"ATU tune" in the menu (Alt-M, then T) starts an antenna tuner cycle on the
radio, the same as pressing its ATU button. The radio transmits a carrier while it tunes, so check the frequency
first. The command is chosen by `tune_command` in the `[rig]` section:

| Value | Command sent to rigctld |
| --- | --- |
| `auto` (default) | KX2: `w SWT20;`, KX3: `w SWT44;` (the radio's ATU button), any other radio: `G TUNE` (Hamlib's generic tune operation) |
| anything else | sent to rigctld as written, e.g. `w SWT20;` |

## DX cluster and band map

A panel to the right of the QSO list shows what is on the band of the current
entry row. It follows the band when you QSY or switch rows, and F7 switches
between two views:

- **Newest first:** cluster spots and stations you stored for later, newest
  on top: frequency, callsign, time (UTC) and the spotter's comment, as far
  as the width allows (a full comment needs a terminal about 170 columns
  wide).
- **Band map:** the same entries sorted by frequency, plus every station in
  your log on that band at the frequency you worked it. A `>` marks where
  the current row is tuned: in front of the stations on that frequency
  (within 200 Hz), or on a line of its own between its neighbours when no
  station is there.

Stations you have already worked on that band (by the contest's dupe rules)
are shown dimmed; stored stations are marked with `*`.

**Storing a station for later.** Type the callsign and press F9 (or Ctrl-J):
the station is noted on the current frequency and mode and the entry row is
cleared. Ctrl-Enter does the same in terminals that send it as a key of its
own; many send a plain Enter instead, so check what yours does before relying
on it. Stored stations are kept in the log file, so they survive a restart.
They disappear when you log the station on that band, or when you highlight
one and press Del.

**Going to a station.** PgDn and PgUp move a highlight through the panel. In
the band map the first press starts from your frequency: PgDn goes to the
next station above, PgUp to the next below. Enter in an empty call field
takes the highlighted entry: the current row is tuned to its frequency (and
mode, for a stored station) and the callsign is filled in. Esc removes the
highlight.

F6 spots a station on the cluster: the callsign in the call field on the
current row's frequency, or, if the call field is empty, the last logged QSO
on the frequency it was logged with. llog asks for an optional comment; Enter
sends the spot, Esc cancels. A spot is public and cannot be withdrawn. The
message line then shows the node's answer: that the spot was accepted (it
came back from the node), or the node's own text if it refused it.

The panel needs a terminal at least 142 columns wide; on narrower terminals
it is hidden. It works without a cluster node too, with stored and worked
stations only.

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
what is displayed; nothing is fetched again. Leave `host` empty to switch the
cluster off.

### Testing the cluster functions without a real node

Do not send made-up spots to a real cluster: they are passed on to nodes and
users worldwide. `tools/fake_cluster.py` is a small local cluster for this:

```
python3 tools/fake_cluster.py
```

Then set `host = 127.0.0.1` and `port = 7300` in the `[cluster]` section. It
sends a made-up spot every 20 seconds, answers the startup `sh/dx`, and
echoes the spots you send with F6. With `call = REFUSE` it refuses your
spots, so you can see how llog shows that.

## Configuration

Station data and contest data live in separate files under `~/.config/llog/`:

| File | Content |
| --- | --- |
| `config.ini` | Callsign, e-mail, QRZ login, rigctld host and port, `vfo_switch`, `tune_command`, DX cluster node |
| `contests/<name>.ini` | Contest name, exchange you send, operator category, `SPECIFIC` header, your locator, dupe rules, scoring |

Both are created by a few questions the first time they are needed: the
station file on the very first start, a contest file the first time you start
llog with a new contest name. Copy or edit the `.ini` files to change them.
An `email` entry in a contest file overrides the station e-mail.

The QRZ password is stored in `config.ini` in plain text (file mode 600); the
environment variables `QRZ_USERNAME` and `QRZ_PASSWORD` override the file.

## Locator and distance

Each entry row has a locator field. After the QRZ lookup (Tab or Space after
the callsign) it is filled with the locator from QRZ; type over it if the
station gives a different one, for example when portable. A locator you typed
or emptied by hand is not overwritten for that callsign.

Set your own locator in the contest file:

```
locator = JO31AA
```

The QSO list then shows the distance in km for every QSO that has a locator,
and the QRZ line shows it for the station you are entering. The distance is
the great-circle distance between the centres of the two locators (4, 6 or 8
characters). The locator of a logged QSO can be changed in the list like any
other cell. The ADIF export contains `GRIDSQUARE`, `MY_GRIDSQUARE` and
`DISTANCE`.

## Worked before

llog can tell you whether you have worked a station before, based on an ADIF
file, for example an export of your main logbook. Set the file in the
`[station]` section of `config.ini`:

```
[station]
worked_before = ~/logbook.adi
```

A contest file may name its own file with the same key, which then replaces
the one from `config.ini` for that contest.

As soon as the call field holds a callsign, a line above the QRZ line shows
what the file knows:

```
Worked before: 2 QSOs, last 2026-11-03 2m FM  |  bands 70cm 2m  |  N13  |  Horst  |  JO31SW
```

That is the number of QSOs, date, band and mode of the last one, all bands
worked, and DOK (or received exchange), name and locator as far as the file
has them. `DL1ABC/P` is also matched against `DL1ABC`. A station that is not
in the file shows `Worked before: no`.

This is information only: nothing is filled into the entry fields and it has
no effect on dupe checking or scoring, which look at the contest log alone.
The file is read once at startup, so export it again and restart llog to
bring it up to date.

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
| `multipliers` | `N[0-9][0-9], WN, DVN` | Received exchanges that count as a multiplier. If the exchange has several words (`001 N13`), each word is checked. Entries are exact values or patterns (`?` one character, `*` any text, `[0-9]` one digit). `*` alone makes every different exchange a multiplier. Empty = no multipliers. |
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
| Alt-M | Menu: export ADIF (`<logfile>.adi`), export Cabrillo (`<CALL>-<date>-1.cbr`), ATU tune |
| `\` | Switch to the other entry row (A/B) |
| PgDn / PgUp | Highlight a station in the panel; Enter in an empty call field takes it |
| F7 | Switch the panel between newest first and band map |
| F9 or Ctrl-J | Store the station in the call field for later |
| Del | Remove the highlighted stored station |
| F6 | Spot a station on the DX cluster (asks for a comment first) |
| Up / Down | Edit a logged QSO in the list (see below) |
| F8 | Delete the last QSO, or the selected one while editing |
| F10 or Ctrl-X | Quit |

### Menu

Alt-M opens a small menu in the middle of the screen. Up and Down move, Enter
selects, Esc closes it; the letter in front of an entry selects it directly
(`A` for ADIF, `C` for Cabrillo, `T` for ATU tune). The exports are written
next to the log file and the message line shows the file name.

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

## Exchange and running number

`exchange_sent` in the contest file is the text you send. A `#` in it is
replaced by a running number with at least three digits:

| `exchange_sent` | You send |
| --- | --- |
| `N99` | `N99` for every QSO |
| `#` | `001`, `002`, `003`, ... |
| `# N99` | `001 N99`, `002 N99`, ... |

The next number is shown under "Exch sent" and is stored with the QSO when
you log it. It is always one more than the highest number already in the log,
so deleting a QSO never makes a number repeat, and a log continued on another
day carries on counting. `serial_start = 100` in the contest file makes a new
log start at another number. The sent exchange of a logged QSO can be
corrected in the QSO list like any other cell.

The received exchange is a free-text field, so a received number is typed as
the station sends it.

## Test

```
python3 test/run_test.py
```

Runs the real UI in a pseudo-terminal against a simulated `rigctld`.
The QRZ lookup is only tested against a sample XML response.
