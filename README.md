# llog

A contest logger for the Linux terminal: keyboard-driven, fast to start, and
built around the way a single operator works a contest with one radio.

llog began as a very small logger for two local VHF activity evenings. It has
grown into a complete contest tool, but the idea is unchanged: one Python
file, nothing to install or compile besides Hamlib, plain text files for all
settings, and a log you can read with any SQLite tool.

**Logging**

- Two entry rows, each with its own frequency and mode, to switch between two
  frequencies with one key
- Dupe checking as you type, with rules set per contest
- Check partial against the super check partial list of contest callsigns
- Running numbers in the sent exchange
- In CW, Enter sends the messages of the QSO through the radio (ESM)
- Every part of a logged QSO can be corrected in place
- Periodic backups, ADIF and Cabrillo export

**Scoring**

- Live score with points and multipliers, defined per contest in a small
  text file: points by mode, band and continent; multipliers from the
  exchange, callsign prefixes, DXCC countries or individual stations
- A panel of worked and missing multipliers, and a hint when the station you
  are entering is a new one
- Ready-made contest files for WNA, WSA, the DARC CW-Ausbildungscontest and
  the OK DX RTTY Contest

**Radio**

- Live frequency and mode through Hamlib's `rigctld`; typing a frequency or
  mode in the logger tunes the radio
- llog finds the radio on the USB ports, configures `rigctld` and starts it
- Tested with an Elecraft KX2; an Icom IC-705 is supported but untested

**Information while you operate**

- QRZ.com lookup with name, location and locator
- Distance and beam heading (short and long path) from your locator
- Country, continent and CQ zone from a country file
- Whether you have worked the station before, from your main logbook (ADIF)
- DX cluster with a newest-first list and a band map, stations you noted to
  come back to, and sending your own spots

**What it does not do**

llog logs and scores. In CW it can send the standard messages of a
contest QSO through the radio, but there is no free keyboard sending, and it
does not play voice messages or decode digital modes. It is made for one operator and one radio, so there is no
networking between computers and no two-radio (SO2R) operation.

## Requirements

- Linux and a terminal; about 100 columns for logging, 142 or more for the
  DX cluster and band map panel
- Python 3.9 or newer (standard library only)
- Hamlib for radio control, e.g. `sudo apt install libhamlib-utils`
- Optional: a QRZ.com XML subscription for full lookup data, a `cty.dat`
  country file for country-based scoring, a DX cluster node

## Quick start

```
git clone https://github.com/Purple-Tentacle/llog
cd llog
mkdir -p ~/.config/llog/contests
cp examples/wsa.ini ~/.config/llog/contests/     # then enter your DOK in it
python3 llog.py wsa
```

The first start asks for your callsign and QRZ login. Then connect the radio
and press Alt-M, R, D to detect it and Alt-M, R, S to start `rigctld`. Type a
callsign, Space, the exchange, Enter: that is a QSO.

## Usage

```
python3 llog.py wsa                 # log file wsa.db in the current directory
python3 llog.py wsa wsa-2026-12.db  # explicit log file, e.g. one per month
python3 llog.py                     # lists the configured contests
```

Without a radio connection the logger still works; a typed frequency is then
used for the log only.

## Radio connection (rigctld)

llog talks to the radio through Hamlib's `rigctld`. It can set that up and
run it for you: open the menu with Alt-M and choose **Radio / rigctld**.

| Entry | What it does |
| --- | --- |
| Detect radio and save settings | Looks for a radio on the USB serial ports and writes what it finds to the `[rig]` section of `config.ini` |
| Start rigctld | Starts `rigctld` with these settings |
| Stop rigctld | Stops the `rigctld` that llog started |
| Start with llog: on/off | Whether llog starts `rigctld` by itself every time |

The top of the submenu shows the radio that is set up and whether `rigctld`
is running.

**Detection.** Icom radios connected by USB (IC-705, IC-7300, ...) are
recognised by their USB name. Elecraft radios (KX2, KX3, K3) sit behind a
plain USB serial cable, so llog asks every such port for its identity at
38400, 19200, 9600 and 4800 baud. That sends a few characters to each USB
serial adapter that is plugged in; unplug other serial devices first if they
might react to that. If several radios are found, llog asks which one to
use. Detection needs the serial port for itself, so stop `rigctld` first.

**Start with llog.** With this switched on, llog starts `rigctld` with the
saved settings and does not scan. Only if that fails because the saved radio
is not there (its USB device is missing, or `rigctld` gives up at once) does
llog look for a radio, save what it finds and start `rigctld` for it. With
several radios connected it asks which one to use.

**Running.** A `rigctld` started by llog is stopped again when llog quits.
Its messages go to `~/.config/llog/rigctld.log`; if it stops right after the
start, the message line shows the last line of that file. If a `rigctld` is
already running (started by you in another terminal), llog uses it and does
not start a second one.

The settings in `config.ini`, in case you want to set them by hand:

```
[rig]
rigctld_radio = KX2
rigctld_model = 2044
rigctld_device = /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_XXXXXXXX-if00-port0
rigctld_speed = 38400
rigctld_options =
rigctld_autostart = no
```

`rigctld_options` takes extra `rigctld` options such as `--vfo`.

You can still start `rigctld` yourself instead:

```
rigctl -l | grep -i -e 705 -e kx2             # Hamlib model numbers
rigctld -m <model> -r /dev/ttyACM0 &          # Icom IC-705 over USB
rigctld -m <model> -r /dev/ttyUSB0 -s 38400 & # Elecraft KX2 with KXUSB cable
```

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

## CW messages and ESM

llog can have the radio send the CW of a contest QSO. The radio keys the text
itself at its own keyer speed; llog only hands it over through `rigctld`.

### Message keys

F1 to F10 send messages. The keys and their meaning follow N1MM Logger, and
there is one set for **running** (you call CQ and stations answer you) and
one for **search and pounce** (you answer someone's CQ):

| Key | Running | Search and pounce |
| --- | --- | --- |
| F1 | CQ: `CQ TEST DA2IT DA2IT K` | QRL?: `QRL? DE DA2IT` |
| F2 | Exch: `5NN N13` | Exch: `TU 5NN N13` |
| F3 | TU: `TU DA2IT` | TU: `TU` |
| F4 | MyCall: `DA2IT` | MyCall: `DA2IT` |
| F5 | HisCall: the callsign in the call field | the same |
| F6 | B4: `DL1ABC QSO B4` | B4: `QSO B4` |
| F7 | QRZ: `DA2IT QRZ` | (none) |
| F8 | Agn: `AGN` | Agn: `AGN` |
| F9 | Nr?: `NR?` | Nr?: `NR?` |
| F10 | Call?: `CL?` | Call?: `CL?` |
| F11 | Auto CQ: repeats the F1 message with a pause | (none) |
| F12 | Wipe: clears the entry row | the same |

Switch between the two sets in the menu: Alt-M, then S ("Operating"). The
header shows `RUN` or `S&P`.

The second line from the bottom of the screen shows the message keys of the
current set with their labels. Keys without a message are left out. While
the radio is offline or in a mode without messages (RTTY, for example), the
message keys are hidden and the line says why.

### Repeated CQ (F11)

F11 in Run sends the F1 message again and again, with a pause after each
transmission. The key is highlighted while this is on. Any key ends it: F11
again, Esc (which also cuts a CQ that is being sent), or simply typing the
callsign of the station that answers. It also ends when the radio leaves CW.

The pause is set in the menu (Alt-M, then P) in seconds; decimals such as
`2.5` are allowed, the default is 3. It is kept in `config.ini` (`cq_pause`
in the `[cw]` section) and is shown next to the key.

llog cannot see when the radio has finished sending. It asks the radio for
its keyer speed and works out from it how long the message takes, and the
pause starts after that time. If the radio does not tell its speed, llog
assumes 20 WPM; at a slower speed the pause is then shorter than set.

Many terminals use F11 for full screen. If the key does not reach llog,
remove that shortcut in the terminal's settings.

### Voice messages in SSB

In SSB, AM and FM llog has no texts to send. It starts the voice memories
that are recorded in the radio instead, over the same connection as
everything else; no audio cable is needed. Two keys have a memory, the same
on every radio:

| Key | Memory of the radio | Record there |
| --- | --- | --- |
| F1 (Run only) | 1 | your CQ call |
| F4 | 2 | your callsign |

The other message keys have no function in these modes and are not shown.
F11 repeats memory 1 with the pause from the menu. llog watches the radio's
transmit state and starts the pause when the radio is back on receive, so
the length of the recording does not matter. If the radio does not transmit
after the command (for example because the memory is empty), llog says so
and ends the repeat. Esc stops a running message.

ESM uses the two memories as far as they go. When running, Enter in an empty
call field sends the CQ (F1); with a callsign it logs. In search and pounce,
Enter with a callsign sends your call (F4) and moves to the exchange, as
long as a field the contest requires is still empty; then it logs. The key
Enter would send is highlighted. With ESM off, Enter only logs.

Recording is done on the radio:

- **KX2 / KX3:** hold REC, tap 1 or 2, then XMIT to start and to stop
  (up to 15 seconds each). llog plays them with the commands for a tap on
  MSG and on 1 or 2 (`SWT11;SWT19;` and `SWT11;SWT27;`) and stops with Hamlib's "PTT off",
  which sends `RX;`.
- **IC-705:** VOICE > REC/SET > REC, memories T1 and T2; a microSD card is
  needed. llog uses Hamlib's `send_voice_mem` and, if the installed Hamlib
  refuses it, the CI-V command `28 00` directly (radio address A4, the
  default).
- **Other radios:** work if Hamlib supports `send_voice_mem` for them.

### ESM ("Enter sends message")

With ESM on, the Enter key sends whatever comes next in the QSO, depending
on what is in the entry row, and logs the QSO when it is complete. You type
the callsign, press Enter, type what you receive, press Enter.

| Entry row | Running: Enter sends | Search and pounce: Enter sends |
| --- | --- | --- |
| call field empty | F1 | F4 |
| callsign typed | F5 + F2 as one transmission, `DL1ABC 5NN N13` | F4, again on every Enter |
| Enter again, received exchange still missing | F8 | F4 |
| received exchange typed | F3, and the QSO is logged | F2, and the QSO is logged |

On the message key line, the keys that Enter would send now are highlighted,
and the right end of the line says it in words: `Enter: F5+F2`,
`Enter: F3 + log`, `Enter: dupe warning`. It follows what you type.

Details:

- If you correct the callsign after your exchange went out, the closing
  message repeats it: `DL1ABD TU DA2IT`.
- If you typed callsign and exchange before the first Enter, that Enter still
  sends your exchange, and the next one closes and logs.
- Sending F2 by hand counts: after F5 and F2, Enter continues with F8 or F3.
- A running number is fixed when it is first sent and stays with that QSO
  until it is logged; the other entry row gets the next number meanwhile.
- Dupes: by default a station you have already worked is handled like any
  other caller, and you get the usual warning on the Enter that would log
  it; press Enter again to send the closing message and log it anyway. With
  `esm_work_dupes = no` in the contest file, the first Enter on a dupe sends
  F6 and no exchange when you are running, and sends nothing when you search
  and pounce. A second Enter works the station anyway.
- `esm_required` names the fields that must be filled before Enter logs:
  `exch` (default), `loc`, both, or nothing. With nothing required, the
  second Enter always logs.
- `esm_empty` is the key Enter sends in an empty call field when running:
  `f1` (CQ, default) or for example `f7` (QRZ).

ESM is switched on and off in the menu (Alt-M, then E). It is on by default,
and the choice is kept in `config.ini` (`enter_sends` in the `[cw]` section).
With ESM off, Enter only logs. The message keys work either way.

### Settings in the contest file

Each key is a line `run_f<n>` or `sp_f<n>` with a label for the key line, a
comma, and the text. These are the defaults; a contest file only needs the
lines it changes.

```
run_f1 = CQ, CQ TEST {MYCALL} {MYCALL} K
run_f2 = Exch, {RST} {EXCH}
run_f3 = TU, TU {MYCALL}
run_f4 = MyCall, {MYCALL}
run_f5 = HisCall, {CALL}
run_f6 = B4, {CALL} QSO B4
run_f7 = QRZ, {MYCALL} QRZ
run_f8 = Agn, AGN
run_f9 = Nr?, NR?
run_f10 = Call?, CL?

sp_f1 = QRL?, QRL? DE {MYCALL}
sp_f2 = Exch, TU {RST} {EXCH}
sp_f3 = TU, TU
sp_f4 = MyCall, {MYCALL}
sp_f5 = HisCall, {CALL}
sp_f6 = B4, QSO B4
sp_f7 =
sp_f8 = Agn, AGN
sp_f9 = Nr?, NR?
sp_f10 = Call?, CL?

esm_empty = f1
esm_work_dupes = yes
esm_required = exch
esm_start = run
```

| Placeholder | Replaced by |
| --- | --- |
| `{MYCALL}` | your callsign |
| `{CALL}` | the callsign in the call field |
| `{RST}` | the report you send, with N for 9: `5NN` |
| `{EXCH}` | the exchange you send, including a running number |

A line with nothing after the `=` removes that key. ESM then skips that
message but still does the step, for example logging without a TU.

All of this only happens while the radio is in CW; in any other mode, or
without a radio connection, nothing is transmitted and Enter logs as usual.
Esc shortly after a message stops it: Elecraft radios are sent their own
back-to-receive command, other radios Hamlib's stop command.

## ATU tune

"ATU tune" in the menu (Alt-M, then T) starts an antenna tuner cycle on the
radio, the same as pressing its ATU button. The radio transmits a carrier while it tunes, so check the frequency
first. The command is chosen by `tune_command` in the `[rig]` section:

| Value | Command sent to rigctld |
| --- | --- |
| `auto` (default) | KX2: `w SWT20;`, KX3: `w SWT44;` (the radio's ATU button), any other radio: `G TUNE` (Hamlib's generic tune operation) |
| anything else | sent to rigctld as written, e.g. `w SWT20;` |

## DX cluster and band map

A panel on the right side of the screen, from the top down to the message
line, shows what is on the band of the current entry row. It follows the band when you QSY or switch rows, and Alt-B switches
between two views:

- **Newest first:** cluster spots and stations you stored for later, newest
  on top: frequency, callsign, age in minutes (`12m`) and the spotter's comment, as far
  as the width allows (a full comment needs a terminal about 170 columns
  wide).
- **Band map:** the same entries sorted by frequency, plus every station in
  your log on that band at the frequency you worked it. A `>` marks where
  the current row is tuned: in front of the stations on that frequency
  (within 200 Hz), or on a line of its own between its neighbours when no
  station is there.

The age is how long ago the spot arrived, the station was stored, or, for a
station from your log, the QSO was made; anything older than 999 minutes
shows `>999`.

Stations you have already worked on that band (by the contest's dupe rules)
are shown dimmed; stored stations are marked with `*`.

**Storing a station for later.** Type the callsign and press Alt-L (or Ctrl-J):
the station is noted on the current frequency and mode, together with the
received exchange and the locator if you had already entered them, and the
entry row is cleared. When you take the station from the panel later, the
exchange and locator are filled in again. Ctrl-Enter does the same in terminals that send it as a key of its
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

Alt-S spots a station on the cluster: the callsign in the call field on the
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
echoes the spots you send with Alt-S. With `call = REFUSE` it refuses your
spots, so you can see how llog shows that.

## Files and settings

llog keeps its settings in one folder and writes each contest log into the
folder you start it from.

### Overview

| File | Where | Created by | What it is for |
| --- | --- | --- | --- |
| `config.ini` | `~/.config/llog/` | llog, on the very first start | Settings for llog itself: your station, QRZ login, radio, DX cluster, backup, CW options |
| `<name>.ini` | `~/.config/llog/contests/` | llog, the first time you start a contest with that name | Settings for one contest: rules, exchange, messages |
| `<name>.db` | the folder you start llog in | llog, at the first start of the contest | The contest log (SQLite) |
| `backups/<name>-<date>-<time>.db` | next to the log file | llog, every few minutes and on quit | Copies of the log |
| `<name>.adi` | next to the log file | menu, "Export ADIF" | ADIF export |
| `<CALL>-<date>-1.cbr` | next to the log file | menu, "Export Cabrillo" | Cabrillo export |
| `cty.dat` | `~/.config/llog/` | you (download) | Country file, for country scoring and the country shown for a callsign |
| `MASTER.SCP` | `~/.config/llog/` | you (download) | Callsign list for check partial |
| an ADIF file of earlier QSOs | anywhere, named in a setting | you | "Worked before" information |
| `rigctld.log` | `~/.config/llog/` | llog, when it starts `rigctld` | Output of `rigctld`, for troubleshooting |

### Settings for llog: `config.ini`

Everything that stays the same from contest to contest is in
`~/.config/llog/config.ini`. llog asks a few questions on the very first
start and writes the file; after that you edit it with a text editor. A few
settings are also written by the menu (radio detection, start `rigctld` with
llog, ESM on or off, auto CQ pause).

| Section | Settings |
| --- | --- |
| `[station]` | `call`, `email`; optional paths `country_file`, `scp_file`, `worked_before` |
| `[qrz]` | `username`, `password` for the callsign lookup |
| `[rig]` | `host` and `port` of `rigctld`, `vfo_switch`, `tune_command`, and the `rigctld_*` settings the radio menu writes |
| `[cluster]` | `host`, `port`, `call`, `minutes` |
| `[backup]` | `minutes`, `keep`, `folder` |
| `[cw]` | `enter_sends` (ESM on or off), `cq_pause` (seconds between repeated CQs) |

The QRZ password is stored in `config.ini` in plain text (file mode 600); the
environment variables `QRZ_USERNAME` and `QRZ_PASSWORD` override the file.

### Settings for a contest: `contests/<name>.ini`

Everything that belongs to one contest is in
`~/.config/llog/contests/<name>.ini`, in a single `[contest]` section. The
name is the one you start llog with: `python3 llog.py wna` uses `contests/wna.ini`.

There are two ways to get such a file:

- **Let llog ask.** Start llog with a new name. It asks for the contest
  name, your exchange, the category, the `SPECIFIC` header, your locator,
  the dupe rule, points and multipliers, and writes the file.
- **Copy an example.** The `examples` folder has ready-made files (`wna.ini`,
  `wsa.ini`, `ok-dx-rtty.ini`, `darc-cwa.ini`). Copy one to
  `~/.config/llog/contests/` and fill in your exchange and locator.

The file holds the contest name for the log header, the exchange you send
(with `#` for a running number), `call` if you use a different callsign in
this contest, `locator`, `mode`, the categories, the dupe rules, the scoring
(`points`, `multipliers` and the `mult_*` options), the message keys
(`run_f1` to `run_f10`, `sp_f1` to `sp_f10`), the ESM options and
`worked_before`. The sections "Dupe checking", "Scoring", "CW messages and
ESM" and "Exchange and running number" describe them.

Two settings can be given in both files; the contest file wins: `email`, and
`worked_before`.

A contest file can also live elsewhere: give llog the path instead of a name,
for example `python3 llog.py ~/contests/wna-2026.ini`.

### The contest log

The log of a contest is one SQLite file. llog creates it in **the folder you
are in when you start llog**, named after the contest: `python3 llog.py wna`
creates or opens `wna.db` there. Starting the same contest again from the
same folder continues that log; starting it from another folder begins a new,
empty one.

To choose the file yourself, name it as the second argument:

```
python3 llog.py wna ~/logs/wna-2026-12.db
```

This is also how you keep several runs of the same contest apart, since the
contest file stays the same and only the log file changes. The exports and
the `backups` folder are always written next to the log file in use (the
backup folder can be changed with `folder` in `[backup]`).

### Files you provide

- **`cty.dat`** (country file from www.country-files.com): put it in
  `~/.config/llog/`, or next to `llog.py`, or name another place with
  `country_file` in `[station]`. Needed for contests that score by country or
  continent; it also provides the country shown for a callsign.
- **`MASTER.SCP`** (from www.supercheckpartial.com): the same places, or
  `scp_file` in `[station]`. Without it check partial only knows
  the callsigns in your log and in the worked-before file.
- **Worked-before ADIF**: any ADIF file, named with `worked_before` in the
  contest file or in `[station]`.

### Another settings folder

`--config-dir <folder>` makes llog use that folder instead of
`~/.config/llog/` for `config.ini`, `contests/`, `cty.dat` and `MASTER.SCP`.

## Backup

Every QSO is written to the log file at once. In addition llog copies the
log file every 5 minutes, and once more when you quit, if anything has
changed since the last copy. The copies go to a `backups` folder next to the
log file, named `<logname>-<UTC date>-<time>.db`, and the 50 newest are
kept.

```
[backup]
minutes = 5
keep = 50
folder =
```

"Backup now" in the menu (Alt-M, then B) writes such a copy immediately,
with the same name pattern and in the same folder.

`minutes = 0` switches the periodic backup off. `folder` can point somewhere else, for
example a USB stick. A backup is a complete log file: to go back to one, copy
it to the place of the log file, or start llog with it directly
(`python3 llog.py wsa backups/wsa-20261201-190500.db`).

## Locator and distance

Each entry row has a locator field. After the QRZ lookup (Tab or Space after
the callsign) it is filled with the locator from QRZ; type over it if the
station gives a different one, for example when portable. A locator you typed
or emptied by hand is not overwritten for that callsign.

Set your own locator in the contest file:

```
locator = JO31AA
```

The QSO list then shows the distance in km for every QSO that has a locator.
For the station you are entering, the QRZ line shows distance and bearing,
e.g. `[JO40AB: 163 km, SP 105° / LP 285°]`. The distance is along the short
great-circle path between the centres of the two locators (4, 6 or 8
characters). The bearing is the direction to point an antenna, with 0° north
and 90° east: `SP` for the short path and `LP` for the long path, which is
the opposite direction. If a station has no locator but a country file is present, the
QRZ line shows a rough distance and bearing to the middle of its country. The locator of a logged QSO can be changed in the list like any
other cell. The ADIF export contains `GRIDSQUARE`, `MY_GRIDSQUARE` and
`DISTANCE`.

## Check partial

While you type a callsign, llog shows known callsigns that contain what you
have typed so far (from three characters on), to the right of the entry rows.
It helps to complete a callsign you only copied in part and to catch typing
errors: a callsign nobody has ever used in a contest is worth a second look.
If what you typed is itself a known callsign, it comes first and is
underlined; then callsigns starting with it, then the others. The number in
the title is the total number of matches, of which as many are shown as fit.

The callsigns come from three places: this contest's log, the worked-before
file, and a `MASTER.SCP` file, the "super check partial" list of active
contest callsigns that other contest loggers use as well. Download it from
www.supercheckpartial.com and save it as `~/.config/llog/MASTER.SCP` (or set
`scp_file = /path/to/MASTER.SCP` in the `[station]` section of `config.ini`).
Without that file the check still works with your own log and the
worked-before file.

Check partial needs a terminal wide enough for it to fit between the entry
rows and the panel on the right, which is the case from 142 columns on, and
between about 115 and 141 columns, where no panel is shown.

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
| `points` | `FM:2, SSB:4, CW:6` | QSO points, see below. Empty = 1 point per QSO. |
| `multipliers` | `N[0-9][0-9], WN, DVN` | Received exchanges that count as a multiplier. If the exchange has several words (`001 N13`), each word is checked. Entries are exact values or patterns (`?` one character, `*` any text, `[0-9]` one digit). `*` alone makes every different exchange a multiplier. |
| `mult_exclude` | `NM, #` | Received exchanges that never count as a multiplier, even if `multipliers` matches them. Same patterns; `#` stands for any plain number, such as a serial number. |
| `mult_prefix` | `yes` | Every callsign prefix counts as a multiplier (`DL1`, `DK5`, `OK1`; the usual WPX definition, so `DL1ABC/7` is `DL7` and `EA8/DL1ABC` is `EA8`) |
| `mult_country` | `yes` | Every DXCC country counts as a multiplier |
| `mult_calls` | `OK` | Every station from the listed countries (by their main prefix) counts as a multiplier of its own. Ignored when you operate from one of these countries yourself. |
| `mult_per` | `band` (default) or `contest` | Whether a multiplier counts once per band or once in the whole contest |

**Points.** Each entry is `condition:points`, and conditions can be combined
with `+`:

| Condition | Matches |
| --- | --- |
| `CW`, `SSB`, `FM`, `AM`, `RTTY` | the mode |
| `PH`, `DIG` | any phone mode, any digital mode |
| `80M`, `40M`, `2M`, `70CM`, ... | the band |
| `SAME` | station on your own continent |
| `DX` | station on another continent |
| `OWN` | station in your own country |
| `*` | everything |

For each QSO the entry with the most matching conditions wins, so
`SAME:1, DX:2, 40M+SAME:3, 40M+DX:6` gives 1 or 2 points in general and 3 or
6 on 40 m. A QSO that matches no entry scores 0.

The score is the sum of QSO points times the number of multipliers, or just
the points when no multipliers are configured. Dupes (according to the dupe
rules) get no points and no multiplier.

With scoring configured, llog shows the score in the header, the points of
each QSO in the list (`+M` marks a new multiplier, `+2M` two of them), a green `NEW MULTIPLIER`
line while you enter a station that brings one, and writes `CLAIMED-SCORE`
into the Cabrillo file. `python3 llog.py wsa wsa.db --score` prints the score
per band.

Multipliers are shown in a panel below the QSO list. Those named explicitly
in the contest file (`WN`, `O05`, ...) are always listed, with the ones
already worked highlighted in green. All other multipliers (patterns such as
`N[0-9][0-9]`, countries, stations) appear in the panel, in green, once they
have been worked. With `mult_per = band` the panel shows the band the radio
is on. The panel follows edits and deletions of QSOs.

**Country file.** `SAME`, `DX`, `OWN`, `mult_country` and `mult_calls` need to
know the country and continent of a callsign. llog takes that from a
`cty.dat` country file, the same file other contest loggers use. Download it
from www.country-files.com and save it as `~/.config/llog/cty.dat` (or set
`country_file = /path/to/cty.dat` in the `[station]` section of `config.ini`).
With a country file present, the QRZ line also shows country, continent and
CQ zone of the callsign being entered. Portable calls are resolved by their
prefix (`EA8/DL1ABC` is Canary Islands); `/MM` has no country.

**Other contest file options**

| Option | Example | Meaning |
| --- | --- | --- |
| `call` | `OK/DA2IT` | Callsign for this contest, if it differs from the one in `config.ini` |
| `run_f1` ... `sp_f10`, `esm_...` | | CW message keys and ESM settings, see "CW messages and ESM" |
| `mode` | `RTTY` | Log every QSO with this mode, whatever the radio reports (useful when the radio is in a generic data mode) |
| `category_band`, `category_mode`, `category_power` | `ALL`, `RTTY`, `LOW` | Written as `CATEGORY-BAND` etc. into the Cabrillo header |

Ready-made contest files are in `examples/`: `wna.ini`, `wsa.ini`,
`ok-dx-rtty.ini` and `darc-cwa.ini` (DARC CW-Ausbildungscontest). Copy them to `~/.config/llog/contests/` and adjust the
marked lines:

```
mkdir -p ~/.config/llog/contests
cp examples/*.ini ~/.config/llog/contests/
```

The score is your claimed score; the organiser's evaluation is what counts.

## Keys

The bottom line of the screen lists the keys for logging and the panel, the
line above it the CW message keys.

| Key | Action |
| --- | --- |
| Enter | Log the QSO. In CW with ESM on: send the messages of the QSO first (see "CW messages and ESM") |
| F1 to F10 | Send a CW message; F11 repeats the CQ with a pause; F12 clears the entry row |
| Tab / Shift-Tab | Next / previous field |
| Space | Jump from callsign to exchange |
| Esc | Clear the entry line, and stop a CW message that is being sent |
| `\` | Switch to the other entry row (A/B) |
| `<frequency>` Enter | QSY (`7030.5` = kHz, below 1000 = MHz, e.g. `144.3`) |
| `CW` / `SSB` / `FM` / `AM` / `RTTY` Enter | Set mode |
| Up / Down | Edit a logged QSO in the list (see below) |
| PgDn / PgUp | Highlight a station in the panel; Enter in an empty call field takes it |
| Alt-B | Switch the panel between newest first and band map |
| Alt-L or Ctrl-J | Store the station in the call field for later |
| Del | Remove the highlighted stored station |
| Alt-S | Spot a station on the DX cluster (asks for a comment first) |
| Alt-D | Delete the QSO entered last, or the selected one while editing |
| Alt-M | Menu: export ADIF (`<logfile>.adi`), export Cabrillo (`<CALL>-<date>-1.cbr`), backup now, radio setup, ESM on/off, Run or S&P, auto CQ pause, ATU tune, quit |
| Ctrl-X | Quit directly, without the menu |

Alt and a letter needs a terminal that sends Alt as an Escape prefix, which
is the usual setting on Linux.

### Menu

Alt-M opens a small menu in the middle of the screen. Up and Down move, Enter
selects, Esc goes back one level or closes it; the letter in front of an entry selects it directly
(`A` for ADIF, `C` for Cabrillo, `B` for a backup, `R` for the radio menu,
`E` for the ESM switch, `S` for Run or search and pounce, `P` for the auto CQ pause, `T` for ATU tune, `Q` to quit). The exports are written
next to the log file and the message line shows the file name.

### Editing a QSO

Cursor Up moves an edit cursor into the QSO list; Up and Down move through
the log, and Down past the last QSO returns to QSO entry. The selected QSO is
edited in place, cell by cell: time (UTC, `2026-12-01 18:45:00`), callsign,
frequency (kHz), mode, both reports and both exchanges. Tab and Shift-Tab
move between the cells.

Enter saves, Esc cancels, Alt-D deletes the selected QSO. Moving to another QSO
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
