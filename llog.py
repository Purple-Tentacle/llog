#!/usr/bin/env python3
"""llog - a very simple terminal contest logger.

Needs only Python 3.9+ (standard library). Radio control goes through
Hamlib's rigctld, callsign lookup through the QRZ.com XML API.

    rigctld -m <model> -r /dev/ttyUSB0 -s 38400 &
    python3 llog.py <contest> [logfile.db]
"""
import argparse
import configparser
import curses
import fnmatch
import getpass
import math
import os
import queue
import re
import socket
import sqlite3
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

DEFAULT_CONFIG_DIR = os.path.expanduser("~/.config/llog")
AGENT = "llog-0.3"

BANDS = [  # (low Hz, high Hz, ADIF band, Cabrillo frequency or None = kHz)
    (1_800_000, 2_000_000, "160m", None), (3_500_000, 4_000_000, "80m", None),
    (5_250_000, 5_450_000, "60m", None), (7_000_000, 7_300_000, "40m", None),
    (10_100_000, 10_150_000, "30m", None), (14_000_000, 14_350_000, "20m", None),
    (18_068_000, 18_168_000, "17m", None), (21_000_000, 21_450_000, "15m", None),
    (24_890_000, 24_990_000, "12m", None), (28_000_000, 29_700_000, "10m", None),
    (50_000_000, 54_000_000, "6m", "50"), (70_000_000, 71_000_000, "4m", "70"),
    (144_000_000, 148_000_000, "2m", "144"),
    (430_000_000, 440_000_000, "70cm", "432"),
    (1_240_000_000, 1_300_000_000, "23cm", "1.2G"),
    (2_300_000_000, 2_450_000_000, "13cm", "2.3G"),
]
# Hamlib mode -> log mode
RIG_MODES = {"USB": "SSB", "LSB": "SSB", "CW": "CW", "CWR": "CW", "FM": "FM",
             "FMN": "FM", "WFM": "FM", "AM": "AM", "RTTY": "RTTY",
             "RTTYR": "RTTY"}
CABRILLO_MODES = {"CW": "CW", "SSB": "PH", "AM": "PH", "FM": "FM", "RTTY": "RY"}
TYPED_MODES = ("CW", "SSB", "FM", "AM", "RTTY")


def band_of(hz):
    for lo, hi, name, _ in BANDS:
        if lo <= hz <= hi:
            return name
    return ""


def cabrillo_freq(hz):
    for lo, hi, _, cab in BANDS:
        if lo <= hz <= hi and cab:
            return cab
    return str(round(hz / 1000))


def parse_freq(text):
    """'14060' or '7030.5' = kHz, values below 1000 ('14.060', '144.3') = MHz."""
    try:
        value = float(text.replace(",", "."))
    except ValueError:
        return None
    if value <= 0:
        return None
    return round(value * 1e6) if value < 1000 else round(value * 1e3)


def fmt_freq(hz):
    if not hz:
        return "---.---.--"
    khz, rest = divmod(hz, 1000)
    return f"{khz // 1000}.{khz % 1000:03d}.{rest // 10:02d}"


def is_call(text):
    return (len(text) >= 3 and any(c.isdigit() for c in text)
            and any(c.isalpha() for c in text))


def base_call(call):
    """DL1ABC/P -> DL1ABC, EA8/DL1ABC -> DL1ABC."""
    return max(call.split("/"), key=len)


LOCATOR_RE = re.compile(r"^[A-R]{2}[0-9]{2}([A-X]{2}([0-9]{2})?)?$")


def is_locator(text):
    return bool(LOCATOR_RE.match((text or "").upper()))


def locator_position(locator):
    """(latitude, longitude) of the centre of a Maidenhead locator."""
    loc = locator.upper()
    lon = (ord(loc[0]) - 65) * 20 - 180 + int(loc[2]) * 2
    lat = (ord(loc[1]) - 65) * 10 - 90 + int(loc[3])
    if len(loc) >= 6:
        lon += (ord(loc[4]) - 65) * 5 / 60
        lat += (ord(loc[5]) - 65) * 2.5 / 60
        if len(loc) >= 8:
            lon += int(loc[6]) * 0.5 / 60 + 0.25 / 60
            lat += int(loc[7]) * 0.25 / 60 + 0.125 / 60
        else:
            lon += 2.5 / 60
            lat += 1.25 / 60
    else:
        lon += 1
        lat += 0.5
    return lat, lon


def distance_km(loc_a, loc_b):
    """Great-circle distance between two locators in km, or None."""
    if not (is_locator(loc_a) and is_locator(loc_b)):
        return None
    lat1, lon1 = map(math.radians, locator_position(loc_a))
    lat2, lon2 = map(math.radians, locator_position(loc_b))
    a = (math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2)
         * math.sin((lon2 - lon1) / 2) ** 2)
    return round(2 * 6371 * math.asin(math.sqrt(a)))


def my_locator(cfg):
    loc = cfg["contest"].get("locator", "").strip().upper()
    if loc and not is_locator(loc):
        sys.exit(f"locator = {loc} in the contest file is not a valid locator "
                 "(e.g. JO31 or JO31AA)")
    return loc


def mode_group(mode):
    """Contest mode class: all phone modes count as one, all digital as one."""
    if mode == "CW":
        return "CW"
    return "PH" if mode in ("SSB", "FM", "AM") else "DIG"


DUPE_CHECKS = ("band", "band_mode", "mode", "once", "off")


def dupe_rules(cfg):
    con = cfg["contest"]
    check = con.get("dupe_check", "band").strip().lower()
    if check not in DUPE_CHECKS:
        sys.exit(f"dupe_check = {check!r} is not valid; use one of: "
                 + ", ".join(DUPE_CHECKS))
    try:
        return {"check": check,
                "ignore_suffix": con.getboolean("dupe_ignore_suffix", False),
                "exchange": con.getboolean("dupe_exchange", False),
                "minutes": con.getint("dupe_minutes", 0)}
    except ValueError as exc:
        sys.exit(f"Bad dupe setting in the contest file: {exc}")


def score_rules(cfg):
    """points = FM:2, SSB:4, CW:6   multipliers = N[0-9][0-9], WN, ..."""
    con = cfg["contest"]
    points = {}
    try:
        for item in con.get("points", "").replace(",", " ").split():
            mode, value = item.split(":")
            points[mode.upper()] = int(value)
    except ValueError:
        sys.exit("points must look like 'FM:2, SSB:4, CW:6' "
                 "(mode, PH, DIG or * followed by ':' and a number)")
    mults = con.get("multipliers", "").upper().replace(",", " ").split()
    per = con.get("mult_per", "band").strip().lower()
    if per not in ("band", "contest"):
        sys.exit("mult_per must be 'band' or 'contest'")
    return {"configured": bool(points or mults), "points": points or {"*": 1},
            "mults": mults, "mult_per": per}


def qso_points(rules, mode):
    table = rules["points"]
    return table.get(mode, table.get(mode_group(mode), table.get("*", 0)))


def mult_key(rules, exch, freq_hz):
    """Multiplier this exchange earns as (band, value), or None. The whole
    exchange is tried first, then each word of it, so '001 N13' counts as
    the multiplier N13."""
    text = (exch or "").strip().upper()
    for value in [text] + text.split():
        if value and any(fnmatch.fnmatchcase(value, pat) for pat in rules["mults"]):
            band = band_of(freq_hz or 0) if rules["mult_per"] == "band" else ""
            return (band, value)
    return None


def compute_score(rows, dupes, rules):
    """Scores a list of QSO rows. Dupes get no points and no multiplier."""
    worked, mults = {}, set()
    per_qso, bands = {}, {}
    for q in rows:
        band = band_of(q["freq_hz"] or 0)
        stat = bands.setdefault(band, {"qsos": 0, "dupes": 0, "points": 0,
                                       "mults": 0})
        stat["qsos"] += 1
        key = [base_call(q["call"]) if dupes["ignore_suffix"] else q["call"]]
        if dupes["check"] in ("band", "band_mode"):
            key.append(band)
        if dupes["check"] in ("mode", "band_mode"):
            key.append(mode_group(q["mode"]))
        if dupes["exchange"]:
            key.append(q["exch_r"])
        key = tuple(key)
        when = datetime.strptime(q["ts"], "%Y-%m-%d %H:%M:%S")
        last = worked.get(key)
        if dupes["check"] != "off" and last and (
                not dupes["minutes"]
                or (when - last).total_seconds() < dupes["minutes"] * 60):
            stat["dupes"] += 1
            per_qso[q["id"]] = (0, True, False)
            continue
        worked[key] = when
        points = qso_points(rules, q["mode"])
        mult = mult_key(rules, q["exch_r"], q["freq_hz"])
        new_mult = mult is not None and mult not in mults
        if new_mult:
            mults.add(mult)
            stat["mults"] += 1
        stat["points"] += points
        per_qso[q["id"]] = (points, False, new_mult)
    total_points = sum(b["points"] for b in bands.values())
    return {"points": total_points, "mults": len(mults), "seen": mults,
            "score": total_points * len(mults) if rules["mults"] else total_points,
            "per_qso": per_qso, "bands": bands}


def score_report(result, rules):
    lines = [f"{'Band':<6}{'QSOs':>6}{'Dupes':>7}{'Points':>8}{'Mults':>7}"]
    for band, b in result["bands"].items():
        lines.append(f"{band or '?':<6}{b['qsos']:>6}{b['dupes']:>7}"
                     f"{b['points']:>8}{b['mults']:>7}")
    if rules["mults"]:
        lines.append(f"Score: {result['points']} points x {result['mults']} "
                     f"multipliers = {result['score']}")
    else:
        lines.append(f"Score: {result['score']} points")
    return "\n".join(lines)


def rig_retune(cfg):
    how = cfg["rig"].get("vfo_switch", "retune").strip().lower()
    if how not in ("retune", "vfo"):
        sys.exit("vfo_switch in config.ini must be 'retune' or 'vfo'")
    return how == "retune"


def next_exchange(template, sent_so_far, start=1):
    """The exchange to send next. '#' in the template is a running number
    (at least three digits): '#' -> 001, '# N99' -> 001 N99. The number is
    one more than the highest already sent, so deleting a QSO never makes
    a number repeat."""
    if "#" not in template:
        return template
    head, tail = template.split("#", 1)
    pattern = re.compile(re.escape(head) + r"(\d+)" + re.escape(tail) + "$")
    used = [int(m.group(1)) for m in map(pattern.match, sent_so_far) if m]
    number = max(used) + 1 if used else start
    return f"{head}{number:03d}{tail}"


def ascii_only(text):
    return unicodedata.normalize("NFKD", text or "").encode(
        "ascii", "ignore").decode()


# --------------------------------------------------------------------- rig
class Rig(threading.Thread):
    """Polls rigctld for frequency/mode and sends commands for two "VFO" rows.

    retune=True (default): the radio's own VFO B is never used. Each row's
    frequency and mode are remembered here, and switching rows retunes the
    radio's active VFO. Works the same on every radio.

    retune=False: rows are the radio's VFO A and VFO B and switching sends
    a VFO select. Only for radios where VFO B can become the active VFO.
    With rigctld --vfo both VFOs are polled, otherwise only the active one.
    """

    def __init__(self, host, port, retune=True):
        super().__init__(daemon=True)
        self.addr = (host, port)
        self.retune = retune
        self.freq = {"A": None, "B": None}
        self.mode = {"A": None, "B": None}      # raw Hamlib mode, e.g. USB
        self.vfo = "A"                          # the active row
        self.vfo_mode = False                   # rigctld started with --vfo
        self.online = False
        self.error = ""
        self.tune_info = ""                     # result of the last tune()
        self._todo = queue.Queue()
        self._lock = threading.Lock()
        self._sock = None
        self._file = None

    def set_freq(self, vfo, hz):
        self._todo.put(("F", vfo, str(hz)))

    def set_mode(self, vfo, hamlib_mode):
        self._todo.put(("M", vfo, f"{hamlib_mode} 0"))

    def select(self, vfo):
        with self._lock:
            old, self.vfo = self.vfo, vfo
            if not self.retune:
                self._todo.put(("V", vfo, ""))
                return
            # bring the radio to what this row was last tuned to; a row that
            # was never used simply takes over the current frequency and mode
            if self.freq[vfo]:
                self._todo.put(("F", vfo, str(self.freq[vfo])))
            if self.mode[vfo] and self.mode[vfo] != self.mode[old]:
                self._todo.put(("M", vfo, f"{self.mode[vfo]} -1"))

    # ATU tune commands by Hamlib model name; anything else gets Hamlib's
    # generic tune operation
    TUNE_COMMANDS = {"KX2": "w SWT20;", "KX3": "w SWT44;"}

    def tune(self, command="auto"):
        """Starts an ATU tune cycle. Runs on its own connection so that a
        slow or missing answer cannot disturb the polling connection."""
        threading.Thread(target=self._tune, args=(command,), daemon=True).start()

    def _raw(self, line, until=None):
        with socket.create_connection(self.addr, timeout=4) as sock:
            sock.sendall((line + "\n").encode())
            data = b""
            try:
                while until is None or until not in data:
                    chunk = sock.recv(4096)
                    if not chunk or until is None:
                        data += chunk
                        break
                    data += chunk
            except OSError:
                pass
            return data.decode("utf-8", "replace")

    def _tune(self, command):
        try:
            if command == "auto":
                caps = self._raw("\\dump_caps", until=b"Mfg name")
                model = ""
                for line in caps.splitlines():
                    if line.startswith("Model name:"):
                        model = line.split(":", 1)[1].strip()
                command = self.TUNE_COMMANDS.get(
                    model.upper(),
                    "G currVFO TUNE" if self.vfo_mode else "G TUNE")
            self.tune_info = f"ATU tune sent: {command}"
            self._raw(command)
        except OSError as exc:
            self.tune_info = f"ATU tune failed: {exc}"

    def _ask(self, line, extra_lines=0):
        self._sock.sendall((line + "\n").encode())
        first = self._file.readline()
        if not first:
            raise ConnectionError("rigctld closed the connection")
        first = first.strip()
        if first.startswith("RPRT"):
            return None if first != "RPRT 0" else ""
        for _ in range(extra_lines):
            self._file.readline()
        return first

    def _vfo_arg(self, vfo):
        """VFO argument for rigctld --vfo; empty for a plain rigctld."""
        if not self.vfo_mode:
            return ""
        return " currVFO" if self.retune else f" VFO{vfo}"

    def _command(self, cmd, vfo, args):
        if cmd == "V":
            self._ask(f"V VFO{vfo}")
        else:
            self._ask(f"{cmd}{self._vfo_arg(vfo)} {args}")

    def _poll(self, vfo):
        freq = self._ask("f" + self._vfo_arg(vfo))
        mode = self._ask("m" + self._vfo_arg(vfo), extra_lines=1)
        with self._lock:
            # skip if the row changed meanwhile: the answer is for the old row
            if self._todo.empty() and (self.vfo == vfo or not self.retune):
                if freq:
                    self.freq[vfo] = int(float(freq))
                if mode:
                    self.mode[vfo] = mode
        return bool(freq)

    def run(self):
        while True:
            try:
                if self._sock is None:
                    self._sock = socket.create_connection(self.addr, timeout=2)
                    self._file = self._sock.makefile("r")
                    answer = self._ask("\\chk_vfo") or ""
                    self.vfo_mode = answer.endswith("1")
                while not self._todo.empty():
                    self._command(*self._todo.get())
                if not self.retune:
                    active = self._ask("v") or ""
                    with self._lock:   # follow the radio's A/B button
                        if active in ("VFOA", "VFOB") and self._todo.empty():
                            self.vfo = active[-1]
                vfo = self.vfo
                ok = self._poll(vfo)
                if self.vfo_mode and not self.retune:
                    self._poll("B" if vfo == "A" else "A")
                self.online = ok
                self.error = "" if ok else "rig not answering"
                time.sleep(0.3)
            except (OSError, ValueError) as exc:
                self.online = False
                self.error = str(exc)
                if self._sock:
                    self._sock.close()
                self._sock = self._file = None
                time.sleep(2)


# ------------------------------------------------------------ call history
ADIF_FIELD = re.compile(r"<([A-Za-z0-9_]+)(?::(\d+))?(?::[^>]*)?>")


def parse_adif(text):
    """Records of an ADIF file as dicts with upper-case field names."""
    start = re.search(r"<EOH>", text, re.I)
    pos = start.end() if start else 0
    records, record = [], {}
    while True:
        match = ADIF_FIELD.search(text, pos)
        if not match:
            return records
        name, length = match.group(1).upper(), int(match.group(2) or 0)
        pos = match.end() + length
        if name == "EOR":
            if record:
                records.append(record)
            record = {}
        elif length:
            record[name] = text[match.end():pos].strip()


class CallHistory:
    """Which stations were worked before, read from an ADIF logbook file."""

    def __init__(self, path=""):
        self.path = path
        self.calls = {}
        if not path:
            return
        with open(path, encoding="utf-8", errors="replace") as source:
            records = parse_adif(source.read())
        records.sort(key=lambda r: r.get("QSO_DATE", "") + r.get("TIME_ON", ""))
        for rec in records:                    # oldest first: the latest wins
            call = rec.get("CALL", "").upper()
            if not call:
                continue
            entry = self.calls.setdefault(
                call, {"count": 0, "last": "", "band": "", "mode": "",
                       "bands": [], "name": "", "loc": "", "exch": ""})
            band = rec.get("BAND", "").lower()
            if not band and rec.get("FREQ"):
                try:
                    band = band_of(round(float(rec["FREQ"]) * 1e6))
                except ValueError:
                    pass
            entry["count"] += 1
            entry["last"] = rec.get("QSO_DATE", "") or entry["last"]
            entry["band"], entry["mode"] = band, rec.get("MODE", "").upper()
            if band and band not in entry["bands"]:
                entry["bands"].append(band)
            loc = rec.get("GRIDSQUARE", "")
            entry["name"] = rec.get("NAME", "") or entry["name"]
            entry["loc"] = loc.upper() if is_locator(loc) else entry["loc"]
            entry["exch"] = (rec.get("DARC_DOK") or rec.get("SRX_STRING")
                             or entry["exch"]).upper()

    def lookup(self, call):
        """Entry for the callsign; DL1ABC/P falls back to DL1ABC."""
        return self.calls.get(call) or self.calls.get(base_call(call))

    @staticmethod
    def text(entry):
        date = entry["last"]
        if len(date) == 8:
            date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
        last = " ".join(x for x in (date, entry["band"], entry["mode"]) if x)
        parts = [f"{entry['count']} QSO{'s' if entry['count'] != 1 else ''}"
                 + (f", last {last}" if last else ""),
                 "bands " + " ".join(entry["bands"]) if entry["bands"] else "",
                 entry["exch"], entry["name"], entry["loc"]]
        return "Worked before: " + "  |  ".join(p for p in parts if p)


def load_history(cfg):
    """ADIF file from the contest file or, if that has none, config.ini."""
    path = (cfg["contest"].get("worked_before", "").strip()
            or cfg["station"].get("worked_before", "").strip())
    try:
        return CallHistory(os.path.expanduser(path))
    except OSError as exc:
        sys.exit(f"worked_before file: {exc}")


# ----------------------------------------------------------------- cluster
SPOT_RE = re.compile(r"^DX de\s+([A-Z0-9/#-]+):\s*(\d+(?:\.\d+)?)\s+"
                     r"([A-Z0-9/]+)\s*(.*?)\s*(\d{4})Z", re.I)


# line of a "sh/dx" listing, used to fill the list right after login
SHDX_RE = re.compile(r"^\s*(\d+\.\d+)\s+([A-Z0-9/]+)\s+"
                     r"(\d{1,2}-[A-Za-z]{3}-\d{4})\s+(\d{4})Z\s*(.*?)\s*"
                     r"<([A-Z0-9/#-]+)>", re.I)


class Cluster(threading.Thread):
    """Reads spots from a DX cluster node (telnet) and keeps the recent ones."""

    def __init__(self, host, port, call, minutes=30):
        super().__init__(daemon=True)
        self.host, self.port, self.call = host, port, call
        self.max_age = minutes * 60
        self.status = "connecting" if host else "off"
        self._spots = {}      # (dx call, band) -> spot, newest wins
        self._lock = threading.Lock()
        self._sock = None
        self.reply = ""       # what the node answered to our last spot
        self._sent = (0, "")  # (time, dx call) of the spot we sent last

    def feed(self, line):
        seen = time.time()
        waiting = seen - self._sent[0] < 10      # just sent a spot ourselves
        match = SPOT_RE.match(line.strip())
        if match:
            spotter, khz, call, comment, hhmm = match.groups()
            if (waiting and call.upper() == self._sent[1] and
                    spotter.split("-")[0].upper() == self.call.split("-")[0]):
                self.reply = f"Cluster accepted the spot for {call.upper()}."
                self._sent = (0, "")
        else:
            match = SHDX_RE.match(line)
            if not match:
                text = line.strip()
                # anything else right after our spot is the node's answer,
                # except its command prompt ("CALL de NODE ... >")
                if waiting and text and not text.endswith(">"):
                    self.reply = f"Cluster: {text}"
                return
            khz, call, day, hhmm, comment, spotter = match.groups()
            try:                # an old spot keeps its real age
                seen = datetime.strptime(f"{day} {hhmm}", "%d-%b-%Y %H%M").replace(
                    tzinfo=timezone.utc).timestamp()
            except ValueError:
                return
        hz = round(float(khz) * 1000)
        spot = {"call": call.upper(), "freq_hz": hz, "band": band_of(hz),
                "spotter": spotter.rstrip("-#:"), "comment": comment,
                "time": hhmm, "seen": seen}
        with self._lock:
            old = self._spots.get((spot["call"], spot["band"]))
            if not old or old["seen"] <= seen:
                self._spots[(spot["call"], spot["band"])] = spot

    def send_spot(self, freq_hz, call, comment=""):
        """Announces a station on the cluster. Returns an error text or ''."""
        line = f"DX {freq_hz / 1000:.1f} {call} {comment}".rstrip()
        sock = self._sock
        if self.status != "connected" or sock is None:
            return "DX cluster is not connected."
        try:
            sock.sendall((line + "\r\n").encode("latin-1", "replace"))
        except OSError as exc:
            return f"Spot not sent: {exc}"
        self._sent = (time.time(), call.upper())
        return ""

    def spots(self, band):
        """Recent spots on one band, newest first."""
        limit = time.time() - self.max_age
        with self._lock:
            for key in [k for k, v in self._spots.items() if v["seen"] < limit]:
                del self._spots[key]
            found = [v for v in self._spots.values() if v["band"] == band]
        return sorted(found, key=lambda v: v["seen"], reverse=True)

    def run(self):
        while self.host:
            try:
                self.status = "connecting"
                with socket.create_connection((self.host, self.port),
                                              timeout=10) as sock:
                    sock.settimeout(3)
                    logged_in, buf = False, b""
                    while True:
                        try:
                            data = sock.recv(4096)
                            if not data:
                                raise ConnectionError("connection closed")
                        except socket.timeout:
                            data = b""
                        if not logged_in:       # answer the login prompt once
                            sock.sendall((self.call + "\r\nsh/dx 50\r\n").encode())
                            sock.settimeout(120)
                            self._sock = sock
                            logged_in, self.status = True, "connected"
                        buf += re.sub(rb"\xff[\xfb-\xfe].", b"", data)
                        *lines, buf = buf.split(b"\n")
                        for line in lines:
                            self.feed(line.decode("latin-1"))
            except OSError as exc:
                self.status = f"offline: {exc}"
            self._sock = None
            time.sleep(30)


# --------------------------------------------------------------------- qrz
def parse_qrz(xml_text):
    """Returns (session dict, callsign dict) with namespace-free tags."""
    root = ET.fromstring(xml_text)
    out = {"Session": {}, "Callsign": {}}
    for block in root:
        tag = block.tag.split("}")[-1]
        if tag in out:
            out[tag] = {el.tag.split("}")[-1]: (el.text or "") for el in block}
    return out["Session"], out["Callsign"]


class Qrz:
    URL = "https://xmldata.qrz.com/xml/current/?"

    def __init__(self, user, password):
        self.user, self.password = user, password
        self.key = None
        self.cache = {}       # base call -> dict (may contain "error")
        self._busy = set()
        self._lock = threading.Lock()

    def _get(self, **params):
        url = self.URL + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=8) as resp:
            return parse_qrz(resp.read().decode("utf-8", "replace"))

    def _login(self):
        session, _ = self._get(username=self.user, password=self.password,
                               agent=AGENT)
        self.key = session.get("Key")
        if not self.key:
            raise RuntimeError(session.get("Error", "QRZ login failed"))

    def _lookup(self, call):
        for attempt in (1, 2):
            if not self.key:
                self._login()
            session, data = self._get(s=self.key, callsign=call)
            if data:
                return {"name": " ".join(x for x in (data.get("fname"),
                                                     data.get("name")) if x),
                        "qth": data.get("addr2", ""),
                        "country": data.get("country", ""),
                        "grid": data.get("grid", "")}
            error = session.get("Error", "no data")
            if attempt == 1 and ("Session" in error or "session" in error):
                self.key = None      # key expired: log in again, retry once
                continue
            return {"error": error}

    def _worker(self, call):
        try:
            result = self._lookup(call)
        except Exception as exc:              # network, XML, login
            result = {"error": str(exc)}
        with self._lock:
            # do not cache network failures, so a later Tab retries
            if "error" not in result or "Not found" in result["error"]:
                self.cache[call] = result
            else:
                self.cache[call] = dict(result, temporary=True)
            self._busy.discard(call)

    def request(self, call):
        call = base_call(call)
        if not self.user:
            return
        with self._lock:
            cached = self.cache.get(call)
            if call in self._busy or (cached and not cached.get("temporary")):
                return
            self._busy.add(call)
        threading.Thread(target=self._worker, args=(call,), daemon=True).start()

    def info(self, call):
        call = base_call(call)
        if call in self._busy:
            return {"pending": True}
        return self.cache.get(call)


# --------------------------------------------------------------------- log
class Log:
    def __init__(self, path):
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS qso (
            id INTEGER PRIMARY KEY, ts TEXT, mycall TEXT, call TEXT,
            freq_hz INTEGER, mode TEXT, rst_s TEXT, rst_r TEXT,
            exch_s TEXT, exch_r TEXT, name TEXT, qth TEXT, grid TEXT,
            country TEXT)""")
        # stations noted on a frequency to come back to later
        self.db.execute("""CREATE TABLE IF NOT EXISTS later (
            call TEXT, band TEXT, freq_hz INTEGER, mode TEXT, seen REAL,
            PRIMARY KEY (call, band))""")
        self.db.commit()

    def store_later(self, call, freq_hz, mode):
        self.db.execute("INSERT OR REPLACE INTO later VALUES (?,?,?,?,?)",
                        (call, band_of(freq_hz), freq_hz, mode, time.time()))
        self.db.commit()

    def later_rows(self, band):
        found = self.db.execute("SELECT call, freq_hz, mode, seen FROM later "
                                "WHERE band=?", (band,))
        return [dict(zip(("call", "freq_hz", "mode", "seen"), r)) for r in found]

    def delete_later(self, call, band):
        done = self.db.execute("DELETE FROM later WHERE call=? AND band=?",
                               (call, band)).rowcount
        self.db.commit()
        return done

    COLS = ("ts", "mycall", "call", "freq_hz", "mode", "rst_s", "rst_r",
            "exch_s", "exch_r", "name", "qth", "grid", "country")

    def add(self, **qso):
        self.db.execute(
            f"INSERT INTO qso ({','.join(self.COLS)}) VALUES "
            f"({','.join('?' * len(self.COLS))})",
            [qso.get(c, "") for c in self.COLS])
        self.db.commit()

    def count(self):
        return self.db.execute("SELECT COUNT(*) FROM qso").fetchone()[0]

    def rows(self, last=None):
        sql = "SELECT id," + ",".join(self.COLS) + " FROM qso ORDER BY ts, id"
        rows = [dict(zip(("id",) + self.COLS, r)) for r in self.db.execute(sql)]
        return rows[-last:] if last else rows

    def newest(self):
        """The QSO that was entered last (not the latest by time), or None."""
        sql = "SELECT id," + ",".join(self.COLS) + " FROM qso ORDER BY id DESC LIMIT 1"
        row = self.db.execute(sql).fetchone()
        return dict(zip(("id",) + self.COLS, row)) if row else None

    def delete(self, qso_id):
        self.db.execute("DELETE FROM qso WHERE id=?", (qso_id,))
        self.db.commit()

    def update(self, qso_id, **fields):
        self.db.execute(
            f"UPDATE qso SET {','.join(k + '=?' for k in fields)} WHERE id=?",
            list(fields.values()) + [qso_id])
        self.db.commit()

    def find_dupe(self, rules, call, freq_hz, mode, exch="", now=None,
                  before=None):
        """before = (ts, id) of an edited QSO: only look at earlier ones."""
        if rules["check"] == "off":
            return None
        now = now or datetime.now(timezone.utc)
        found = self.db.execute(
            "SELECT ts, call, freq_hz, mode, exch_r FROM qso "
            "WHERE call LIKE ? AND (ts < ? OR (ts = ? AND id < ?)) "
            "ORDER BY ts DESC, id DESC",
            (f"%{base_call(call)}%",) + (
                (before[0], before[0], before[1]) if before
                else ("9999", "9999", 0)))
        for ts, old_call, old_freq, old_mode, old_exch in found:
            if rules["ignore_suffix"]:
                if base_call(old_call) != base_call(call):
                    continue
            elif old_call != call:
                continue
            if (rules["check"] in ("band", "band_mode")
                    and band_of(old_freq or 0) != band_of(freq_hz or 0)):
                continue
            if (rules["check"] in ("mode", "band_mode")
                    and mode_group(old_mode) != mode_group(mode)):
                continue
            if rules["exchange"] and exch and old_exch != exch:
                continue
            if rules["minutes"]:
                then = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc)
                if (now - then).total_seconds() >= rules["minutes"] * 60:
                    continue
            return {"ts": ts, "call": old_call, "freq_hz": old_freq,
                    "mode": old_mode, "exch_r": old_exch}
        return None

    # ---- exports
    def export_adif(self, path, my_loc=""):
        def field(name, value):
            value = ascii_only(str(value))
            return f"<{name}:{len(value)}>{value} " if value else ""
        with open(path, "w", encoding="ascii", newline="\n") as out:
            out.write(f"llog export\n<ADIF_VER:5>3.1.4 "
                      f"<PROGRAMID:4>llog <EOH>\n")
            for q in self.rows():
                ts = q["ts"]
                out.write(
                    field("CALL", q["call"])
                    + field("QSO_DATE", ts[0:4] + ts[5:7] + ts[8:10])
                    + field("TIME_ON", ts[11:13] + ts[14:16] + ts[17:19])
                    + field("FREQ", f"{q['freq_hz'] / 1e6:.6f}"
                            if q["freq_hz"] else "")
                    + field("BAND", band_of(q["freq_hz"] or 0))
                    + field("MODE", q["mode"])
                    + field("RST_SENT", q["rst_s"])
                    + field("RST_RCVD", q["rst_r"])
                    + field("STX_STRING", q["exch_s"])
                    + field("SRX_STRING", q["exch_r"])
                    + field("STATION_CALLSIGN", q["mycall"])
                    + field("NAME", q["name"]) + field("QTH", q["qth"])
                    + field("GRIDSQUARE", q["grid"])
                    + field("MY_GRIDSQUARE", my_loc)
                    + field("DISTANCE", distance_km(my_loc, q["grid"]) or "")
                    + field("COUNTRY", q["country"]) + "<EOR>\n")
        return path

    def export_cabrillo(self, path, cfg):
        cab = cfg["contest"]
        mycall = cfg["station"]["call"]
        lines = ["START-OF-LOG: 3.0", f"CONTEST: {cab.get('contest', '')}",
                 f"CALLSIGN: {mycall}",
                 f"CATEGORY-OPERATOR: {cab.get('category_operator', 'SINGLE-OP')}"]
        rules = score_rules(cfg)
        if rules["configured"]:
            result = compute_score(self.rows(), dupe_rules(cfg), rules)
            lines.append(f"CLAIMED-SCORE: {result['score']}")
        if cab.get("email"):
            lines.append(f"EMAIL: {cab['email']}")
        if cab.get("specific"):
            lines.append(f"SPECIFIC: {cab['specific']}")
        lines.append("CREATED-BY: llog 0.3")
        for q in self.rows():
            ts = q["ts"]
            lines.append("QSO: {:>5} {:<2} {} {} {:<13} {:<3} {:<6} {:<13} {:<3} {}"
                         .format(cabrillo_freq(q["freq_hz"] or 0),
                                 CABRILLO_MODES.get(q["mode"], "DG"),
                                 ts[:10], ts[11:13] + ts[14:16],
                                 q["mycall"], q["rst_s"], q["exch_s"] or "-",
                                 q["call"], q["rst_r"], q["exch_r"] or "-"))
        lines.append("END-OF-LOG:")
        with open(path, "w", encoding="utf-8", newline="\n") as out:
            out.write("\n".join(lines) + "\n")
        return path


# ------------------------------------------------------------------ config
def ask(prompt, default=""):
    answer = input(f"{prompt} [{default}]: " if default else f"{prompt}: ").strip()
    return "" if answer == "-" else (answer or default)


def write_ini(path, cfg, sections):
    out_cfg = configparser.ConfigParser()
    for name in sections:
        out_cfg[name] = dict(cfg[name])
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        out_cfg.write(out)
    print(f"Saved {path}\n")


def contest_path(config_dir, contest):
    if contest.endswith(".ini") or os.sep in contest:
        return contest
    return os.path.join(config_dir, "contests", contest.lower() + ".ini")


def list_contests(config_dir):
    folder = os.path.join(config_dir, "contests")
    if not os.path.isdir(folder):
        return []
    return sorted(f[:-4] for f in os.listdir(folder) if f.endswith(".ini"))


def load_config(config_dir, contest):
    """Station data from config.ini, contest data from contests/<name>.ini."""
    cfg = configparser.ConfigParser()
    cfg.read_dict({"station": {"call": "", "email": "", "worked_before": ""},
                   "qrz": {"username": "", "password": ""},
                   "rig": {"host": "127.0.0.1", "port": "4532",
                           "vfo_switch": "retune", "tune_command": "auto"},
                   "cluster": {"host": "", "port": "7300", "minutes": "30",
                               "call": ""},
                   "contest": {"contest": "", "exchange_sent": "",
                               "category_operator": "SINGLE-OP",
                               "email": "", "specific": "",
                               "dupe_check": "band",
                               "dupe_ignore_suffix": "no",
                               "dupe_exchange": "no",
                               "dupe_minutes": "0",
                               "points": "", "multipliers": "",
                               "mult_per": "band", "locator": "",
                               "serial_start": "1", "worked_before": ""}})
    station_file = os.path.join(config_dir, "config.ini")
    if os.path.exists(station_file):
        cfg.read(station_file)
    else:
        print(f"No station config yet - creating {station_file}\n")
        cfg["station"]["call"] = ask("Your callsign").upper()
        cfg["station"]["email"] = ask("E-mail for log headers")
        cfg["qrz"]["username"] = ask("QRZ.com username (empty = no lookup)")
        if cfg["qrz"]["username"]:
            cfg["qrz"]["password"] = getpass.getpass("QRZ.com password: ")
        cfg["cluster"]["host"] = ask("DX cluster host (empty = no cluster)")
        if cfg["cluster"]["host"]:
            cfg["cluster"]["port"] = ask("DX cluster port", "7300")
        write_ini(station_file, cfg, ("station", "qrz", "rig", "cluster"))

    contest_file = contest_path(config_dir, contest)
    if os.path.exists(contest_file):
        cfg.read(contest_file)
    else:
        con = cfg["contest"]
        print(f"New contest - creating {contest_file}\n")
        name = os.path.splitext(os.path.basename(contest))[0].upper()
        con["contest"] = ask("Contest name for the log header", name)
        con["exchange_sent"] = ask(
            "Exchange you send (e.g. your DOK; '#' = running number, also "
            "inside a text like '# N99'; '-' = none)",
            con["exchange_sent"] or cfg["station"].get("exchange_sent", "")
        ).upper()
        con["category_operator"] = ask("Operator category",
                                       con["category_operator"])
        con["specific"] = ask(
            "SPECIFIC header (own DOK for DARC contests, '-' = none)",
            con["exchange_sent"] if con["exchange_sent"] != "#" else ""
        ).upper()
        con["locator"] = ask(
            "Your locator for this contest, e.g. JO31AA ('-' = none)",
            con["locator"]).upper()
        con["dupe_check"] = ask(
            "Dupe check: band / band_mode / mode / once / off",
            con["dupe_check"]).lower()
        con["points"] = ask(
            "QSO points per mode, e.g. 'FM:2, SSB:4, CW:6' ('-' = 1 per QSO)",
            con["points"])
        con["multipliers"] = ask(
            "Multipliers: received exchanges that count, e.g. "
            "'N[0-9][0-9], WN' or '*' for all ('-' = none)", con["multipliers"])
        write_ini(contest_file, cfg, ("contest",))
        print("More options (dupe_ignore_suffix, dupe_exchange, dupe_minutes, "
              "mult_per) can be edited in that file.\n")
    if not cfg["contest"]["email"]:
        cfg["contest"]["email"] = cfg["station"].get("email", "")
    return cfg


# ---------------------------------------------------------------------- ui
FIELDS = ("call", "rst_s", "rst_r", "exch", "loc")    # entry row fields
LABELS = {"call": "Call", "rst_s": "Sent", "rst_r": "Rcvd",
          "exch": "Exchange rcvd", "loc": "Locator"}
WIDTHS = {"call": 14, "rst_s": 5, "rst_r": 5, "exch": 20, "loc": 8}
# QSO list columns; a logged QSO is edited in place in these cells
COLUMNS = (("ts", 19, "UTC"), ("call", 13, "Call"), ("freq", 9, "kHz"),
           ("mode", 5, "Mode"), ("rst_s", 4, "Sent"), ("exch_s", 7, "Exch"),
           ("rst_r", 4, "Rcvd"), ("exch", 12, "Exch"), ("loc", 8, "Locator"))
SPACE_OK = ("exch", "exch_s", "ts")      # cells that may contain a space
# Alt-M: the main loop turns "Esc m" into this token; a few terminals send
# an 8-bit character for it
ALT_M = ("ALT-M", "\u00ed", "\u00cd", "\u00b5")
ENTRY_X = 26                             # column where the entry fields start


def parse_ts(text):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text.strip(), fmt).strftime(
                "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return None


def khz_text(hz):
    return f"{(hz or 0) / 1000:.3f}".rstrip("0").rstrip(".")


def cells(q):
    """Text of the list cells of one QSO."""
    return {"ts": q["ts"], "call": q["call"], "freq": khz_text(q["freq_hz"]),
            "mode": q["mode"], "rst_s": q["rst_s"], "exch_s": q["exch_s"],
            "rst_r": q["rst_r"], "exch": q["exch_r"], "loc": q["grid"]}


class App:
    def __init__(self, cfg, log):
        self.cfg, self.log = cfg, log
        self.mycall = cfg["station"]["call"]
        self.rules = dupe_rules(cfg)
        self.scoring = score_rules(cfg)
        self._score = (None, None)   # (log state, result)
        self._serial = (None, "")    # (log state, next exchange to send)
        self.rig = Rig(cfg["rig"]["host"], cfg["rig"].getint("port"),
                       retune=rig_retune(cfg))
        self.qrz = Qrz(os.environ.get("QRZ_USERNAME", cfg["qrz"]["username"]),
                       os.environ.get("QRZ_PASSWORD", cfg["qrz"]["password"]))
        self.cluster = Cluster(cfg["cluster"]["host"].strip(),
                               cfg["cluster"].getint("port"),
                               cfg["cluster"]["call"].strip().upper()
                               or self.mycall,
                               cfg["cluster"].getint("minutes"))
        # one entry row per VFO
        self.my_loc = my_locator(cfg)
        self.history = load_history(cfg)
        self.entry = {v: self.new_entry() for v in "AB"}
        self.row = "A"
        self.manual_freq = {"A": 0, "B": 0}
        self.manual_mode = {"A": "SSB", "B": "SSB"}
        self.spot = ""        # callsign of the highlighted panel entry
        self.view = "newest"  # panel view: "newest" or "map" (band map)
        self.prompt = None    # question on the message line: text, value, action
        self.menu = None      # index of the highlighted menu item, or None
        self.edit = None      # QSO being edited in the list, or None
        self.eval = {}        # its cell texts while editing
        self.efocus = 1       # edited cell (index into COLUMNS)
        self.msg = "Type a callsign. Enter = log QSO."

    # ---- the active entry row
    @property
    def val(self):
        return self.entry[self.row]["val"]

    @property
    def focus(self):
        return self.entry[self.row]["focus"]

    @focus.setter
    def focus(self, value):
        self.entry[self.row]["focus"] = value

    @property
    def dupe_ok(self):
        return self.entry[self.row]["dupe_ok"]

    @dupe_ok.setter
    def dupe_ok(self, value):
        self.entry[self.row]["dupe_ok"] = value

    # ---- state helpers
    def freq(self, row=None):
        row = row or self.row
        if self.rig.online and self.rig.freq[row]:
            return self.rig.freq[row]
        return self.manual_freq[row]

    def mode(self, row=None):
        row = row or self.row
        if self.rig.online and self.rig.mode[row]:
            return RIG_MODES.get(self.rig.mode[row], self.rig.mode[row])
        return self.manual_mode[row]

    def default_rst(self, row=None):
        return "59" if self.mode(row) in ("SSB", "FM", "AM") else "599"

    def exch_sent(self):
        template = self.cfg["contest"]["exchange_sent"]
        if "#" not in template:
            return template
        state = self.log.db.total_changes
        if self._serial[0] != state:
            self._serial = (state, next_exchange(
                template, [q["exch_s"] for q in self.log.rows()],
                self.cfg["contest"].getint("serial_start", 1)))
        return self._serial[1]

    @staticmethod
    def new_entry():
        # auto: field -> callsign for which it was filled in automatically
        # manual: field -> callsign for which it was edited by hand
        return {"val": dict.fromkeys(FIELDS, ""), "focus": 0, "dupe_ok": "",
                "auto": {}, "manual": {}}

    def clear(self):
        self.entry[self.row] = self.new_entry()

    def sync_prefill(self):
        """Fills the locator field from the QRZ lookup. A locator that was
        typed or emptied by hand is never overwritten."""
        for entry in self.entry.values():
            val, call = entry["val"], entry["val"]["call"]
            if entry["auto"].get("loc") not in (None, call):
                val["loc"] = ""                 # belonged to another callsign
                del entry["auto"]["loc"]
            if (val["loc"] or not is_call(call)
                    or entry["manual"].get("loc") == call):
                continue
            grid = (self.qrz.info(call) or {}).get("grid", "")
            if is_locator(grid):
                val["loc"], entry["auto"]["loc"] = grid.upper()[:8], call

    def dupe(self):
        if self.edit:      # compare with the QSOs made before the edited one
            call = self.eval["call"]
            if not is_call(call):
                return None
            new = self.edited()
            ts = new.get("ts", self.edit["ts"])
            when = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            return self.log.find_dupe(
                self.rules, call, new.get("freq_hz", self.edit["freq_hz"]),
                new.get("mode", self.edit["mode"]), self.eval["exch"].strip(),
                when.replace(tzinfo=timezone.utc), before=(ts, self.edit["id"]))
        call = self.val["call"]
        if not is_call(call):
            return None
        return self.log.find_dupe(self.rules, call, self.freq(), self.mode(),
                                  self.val["exch"].strip())

    def score(self):
        state = self.log.db.total_changes
        if self._score[0] != state:
            self._score = (state, compute_score(self.log.rows(), self.rules,
                                                self.scoring))
        return self._score[1]

    def new_mult(self):
        """Multiplier value if the exchange being typed is a new one."""
        if self.edit or not is_call(self.val["call"]):
            return None
        key = mult_key(self.scoring, self.val["exch"], self.freq())
        return key[1] if key and key not in self.score()["seen"] else None

    @staticmethod
    def dupe_text(q):
        return (f"DUPE: {q['call']} worked {q['ts'][11:16]}Z on "
                f"{band_of(q['freq_hz'] or 0) or '?'} {q['mode']}"
                + (f", exchange {q['exch_r']}" if q["exch_r"] else ""))

    # ---- actions on the active entry row
    def set_freq(self, hz):
        self.manual_freq[self.row] = hz
        if self.rig.online:
            self.rig.set_freq(self.row, hz)
            self.msg = f"Row {self.row} set to {fmt_freq(hz)}"
        else:
            self.msg = (f"Rig offline - row {self.row} frequency set manually "
                        f"to {fmt_freq(hz)}")

    def set_mode(self, mode):
        self.manual_mode[self.row] = mode
        if self.rig.online:
            hamlib = mode
            if mode == "SSB":
                hamlib = "LSB" if self.freq() < 10_000_000 else "USB"
            self.rig.set_mode(self.row, hamlib)
        self.msg = f"Row {self.row} mode {mode}"

    def switch_row(self):
        self.row = "B" if self.row == "A" else "A"
        if self.rig.online:
            self.rig.select(self.row)
        self.msg = f"Row {self.row}"

    def enter(self):
        call = self.val["call"]
        if self.focus == 0 and not call and self.spot and self.spot_take():
            pass
        elif self.focus == 0 and parse_freq(call) and not is_call(call):
            self.set_freq(parse_freq(call))
            self.val["call"] = ""
        elif self.focus == 0 and call in TYPED_MODES:
            self.set_mode(call)
            self.val["call"] = ""
        elif not is_call(call):
            self.msg = "Not a valid callsign."
        elif not self.freq():
            self.msg = "No frequency: start rigctld or type one (kHz) + Enter."
        elif self.val["loc"] and not is_locator(self.val["loc"]):
            self.msg = "Locator is not valid (e.g. JO31 or JO31AA) - fix or clear it."
        elif self.dupe() and self.dupe_ok != call:
            self.dupe_ok = call
            self.msg = self.dupe_text(self.dupe()) + " - Enter again logs it anyway."
        else:
            info = self.qrz.info(call) or {}
            self.log.add(
                ts=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                mycall=self.mycall, call=call, freq_hz=self.freq(),
                mode=self.mode(),
                rst_s=self.val["rst_s"] or self.default_rst(),
                rst_r=self.val["rst_r"] or self.default_rst(),
                exch_s=self.exch_sent(), exch_r=self.val["exch"].strip(),
                name=info.get("name", ""), qth=info.get("qth", ""),
                grid=self.val["loc"], country=info.get("country", ""))
            self.log.delete_later(call, band_of(self.freq()))   # no longer "later"
            self.msg = f"Logged {call} in row {self.row}"
            self.clear()

    def leave_call_field(self):
        if is_call(self.val["call"]):
            self.qrz.request(self.val["call"])

    def export(self, kind):
        stem = os.path.splitext(self.log.path)[0]
        try:
            if kind == "adif":
                path = self.log.export_adif(stem + ".adi", self.my_loc)
            else:
                rows = self.log.rows()
                day = rows[0]["ts"][:10] if rows else "empty"
                name = f"{self.mycall.replace('/', '-')}-{day}-1.cbr"
                path = self.log.export_cabrillo(
                    os.path.join(os.path.dirname(self.log.path), name), self.cfg)
            self.msg = f"Exported {self.log.count()} QSOs to {path}"
        except OSError as exc:
            self.msg = f"Export failed: {exc}"

    # ---- editing a logged QSO in the list
    def select(self, step):
        """Cursor up/down: move the edit cursor through the QSO list."""
        rows = self.log.rows()
        if not rows:
            return
        if self.edit is None:
            if step > 0:
                return
            index, self.efocus = len(rows) - 1, 1
        else:
            ids = [q["id"] for q in rows]
            index = ids.index(self.edit["id"]) + step if self.edit["id"] in ids \
                else len(rows) - 1
            if index >= len(rows):
                self.edit = None
                self.msg = "Back to QSO entry."
                return
            index = max(index, 0)
        self.edit = rows[index]
        self.eval = cells(self.edit)
        self.msg = (f"Editing QSO #{self.edit['id']} - Tab next cell, Enter "
                    "saves, Esc cancels, Up/Down other QSO, F8 deletes it.")

    def edited(self):
        """Valid frequency/mode/time typed while editing; bad ones are left out."""
        out, old = {}, self.edit
        hz = parse_freq(self.eval["freq"])
        if self.eval["freq"] == khz_text(old["freq_hz"]):
            out["freq_hz"] = old["freq_hz"]      # untouched: keep exact value
        elif hz:
            out["freq_hz"] = hz
        if self.eval["mode"].isalnum():
            out["mode"] = self.eval["mode"]
        if parse_ts(self.eval["ts"]):
            out["ts"] = parse_ts(self.eval["ts"])
        return out

    def save_edit(self):
        call, old, new = self.eval["call"], self.edit, self.edited()
        loc = self.eval["loc"].strip()
        problem = ("Not a valid callsign." if not is_call(call) else
                   "Locator is not valid (e.g. JO31 or JO31AA)."
                   if loc and not is_locator(loc) else
                   "Frequency must be a number in kHz, e.g. 144300."
                   if "freq_hz" not in new else
                   "Mode must be letters/digits, e.g. CW, SSB, FM."
                   if "mode" not in new else
                   "Time must look like 2026-12-01 18:45:00 (UTC)."
                   if "ts" not in new else "")
        if problem:
            self.msg = problem
            return
        fields = dict(new, call=call, exch_r=self.eval["exch"].strip(),
                      exch_s=self.eval["exch_s"].strip(), grid=loc,
                      rst_s=self.eval["rst_s"] or old["rst_s"],
                      rst_r=self.eval["rst_r"] or old["rst_r"])
        if call != old["call"]:        # name etc. belonged to the old callsign
            info = self.qrz.info(call) or {}
            fields.update(name=info.get("name", ""), qth=info.get("qth", ""),
                          country=info.get("country", ""))
        self.log.update(old["id"], **fields)
        self.edit = None
        self.msg = f"Saved QSO #{old['id']} ({call})"

    def edit_key(self, ch):
        name, width, _ = COLUMNS[self.efocus]
        if ch in ("\n", "\r", curses.KEY_ENTER):
            self.save_edit()
        elif ch == "\x1b":
            self.edit = None
            self.msg = "Edit cancelled."
        elif ch == curses.KEY_BTAB:
            self.efocus = (self.efocus - 1) % len(COLUMNS)
        elif ch == "\t" or (ch == " " and name not in SPACE_OK):
            self.efocus = (self.efocus + 1) % len(COLUMNS)
        elif ch in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            self.eval[name] = self.eval[name][:-1]
        elif isinstance(ch, str) and ch.isprintable():
            if len(self.eval[name]) < width:
                self.eval[name] += ch.upper()

    # ---- keys
    # ---- menu (Alt-M)
    MENU = (("a", "Export ADIF", "adif"), ("c", "Export Cabrillo", "cabrillo"),
            ("t", "ATU tune (transmits)", "atu"), ("q", "Quit", "quit"))

    def atu_tune(self):
        if self.rig.online:
            self.msg = "ATU tune ..."
            self.rig.tune_info = ""
            self.rig.tune(self.cfg["rig"].get("tune_command", "auto").strip()
                          or "auto")
        else:
            self.msg = "Rig offline - cannot start ATU tune."

    def menu_key(self, ch):
        """Returns False to quit."""
        hotkeys = [key for key, _, _ in self.MENU]
        if ch == curses.KEY_UP:
            self.menu = (self.menu - 1) % len(self.MENU)
        elif ch == curses.KEY_DOWN:
            self.menu = (self.menu + 1) % len(self.MENU)
        elif ch in ("\n", "\r", curses.KEY_ENTER) or (
                isinstance(ch, str) and ch.lower() in hotkeys):
            if isinstance(ch, str) and ch.lower() in hotkeys:
                self.menu = hotkeys.index(ch.lower())
            action = self.MENU[self.menu][2]
            self.menu = None
            if action == "quit":
                return False
            if action == "atu":
                self.atu_tune()
            else:
                self.export(action)
        elif ch == "\x1b" or ch in ALT_M:
            self.menu, self.msg = None, "Menu closed."
        return True

    def typed(self, name):
        """The user edited this field: automatic filling keeps its hands off."""
        entry = self.entry[self.row]
        entry["auto"].pop(name, None)
        entry["manual"][name] = self.val["call"]

    def entry_key(self, ch):
        name = FIELDS[self.focus]
        if ch == "\n":         # Ctrl-J, and Ctrl-Enter where the terminal sends it
            self.store_for_later()
        elif ch in ("\r", curses.KEY_ENTER):
            self.enter()
        elif ch == "\\":
            self.switch_row()
        elif ch == "\x1b":                                       # Esc
            self.clear()
            self.spot = ""
            self.msg = "Cleared."
        elif ch in ("\t", curses.KEY_BTAB) or (ch == " " and name != "exch"):
            if self.focus == 0:
                self.leave_call_field()
            if ch == curses.KEY_BTAB:
                self.focus = (self.focus - 1) % len(FIELDS)
            elif ch == " " and self.focus == 0:
                self.focus = FIELDS.index("exch")   # skip the RST fields
            else:
                self.focus = (self.focus + 1) % len(FIELDS)
        elif ch in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            self.val[name] = self.val[name][:-1]
            self.dupe_ok = ""
            self.typed(name)
        elif isinstance(ch, str) and ch.isprintable():
            if len(self.val[name]) < WIDTHS[name]:
                self.val[name] += ch.upper()
            self.dupe_ok = ""
            self.typed(name)

    def key(self, scr, ch):
        """Returns False to quit."""
        if self.prompt:
            self.prompt_key(ch)
            return True
        if self.menu is not None:
            return self.menu_key(ch)
        if ch == "\x18":            # Ctrl-X: quit without the menu, as a fallback
            return False
        if ch in ALT_M:
            self.menu, self.msg = 0, "Menu: Up/Down, Enter selects, Esc closes."
        elif ch == curses.KEY_F6:
            self.spot_send()
        elif ch == curses.KEY_F7:
            self.toggle_view()
        elif ch == curses.KEY_F9:
            self.store_for_later()
        elif ch == curses.KEY_DC:
            self.spot_delete()
        elif ch == curses.KEY_NPAGE:
            self.spot_step(+1)
        elif ch == curses.KEY_PPAGE:
            self.spot_step(-1)
        elif ch == curses.KEY_UP:
            self.select(-1)
        elif ch == curses.KEY_DOWN:
            self.select(+1)
        elif ch == curses.KEY_F8:
            rows = [self.edit or self.log.newest()]
            if rows[0]:
                which = "QSO #%d" % rows[0]["id"] if self.edit else "last QSO"
                self.msg = f"Delete {which} with {rows[0]['call']}? (y/N)"
                self.draw(scr)
                scr.timeout(-1)
                answer = scr.get_wch()
                scr.timeout(200)
                if answer in ("y", "Y"):
                    self.log.delete(rows[0]["id"])
                    self.edit = None
                    self.msg = "Deleted."
                else:
                    self.msg = "Kept."
        elif self.edit:
            self.edit_key(ch)
        else:
            self.entry_key(ch)
        return True

    # ---- drawing
    def mult_panel(self, width):
        """Rows of (text, worked): every multiplier named in the contest file,
        then worked ones that matched a pattern such as N[0-9][0-9]."""
        if not self.scoring["mults"]:
            return []
        names = [m for m in self.scoring["mults"]
                 if not any(c in m for c in "*?[")]
        per_band = self.scoring["mult_per"] == "band"
        band = band_of(self.freq() or 0) if per_band else ""
        # without a frequency the band is unknown: show all bands together
        any_band = per_band and not band
        worked = {value for b, value in self.score()["seen"]
                  if b == band or any_band}
        items = [(m, m in worked) for m in names]
        items += [(m, True) for m in sorted(worked - set(names))]
        if not items:
            return []
        label = ("Mults all bands" if any_band else
                 f"Mults {band}" if per_band else "Mults")
        row = [(f"{label} ({len(worked)} worked):", False)]
        rows, used = [], len(row[0][0]) + 1
        for text, done in items:
            if used + len(text) + 1 > width:
                rows.append(row)
                row, used = [], 0
            row.append((text, done))
            used += len(text) + 1
        rows.append(row)
        return rows

    def panel_rows(self):
        """Entries of the right-hand panel for the band of the current row.

        'newest' view: cluster spots and stored stations, newest first.
        'map' view: the same plus the stations in the log, by frequency,
        with a marker line for the current frequency (kind 'here').
        """
        here = self.freq() or 0
        band = band_of(here)
        entries = {}
        if self.view == "map":
            for q in self.log.rows():
                if band and band_of(q["freq_hz"] or 0) == band:
                    entries[q["call"]] = {
                        "call": q["call"], "freq_hz": q["freq_hz"],
                        "time": q["ts"][11:13] + q["ts"][14:16],
                        "comment": "worked", "kind": "worked", "seen": 0}
        for spot in self.cluster.spots(band):
            entries[spot["call"]] = dict(spot, kind="spot")
        for st in self.log.later_rows(band):
            stamp = datetime.fromtimestamp(st["seen"], timezone.utc)
            entries[st["call"]] = dict(st, kind="stored", comment="stored",
                                       time=stamp.strftime("%H%M"))
        rows = list(entries.values())
        for row in rows:
            row["worked"] = row["kind"] == "worked" or bool(self.log.find_dupe(
                self.rules, row["call"], row["freq_hz"], self.mode()))
        if self.view == "map":
            rows.sort(key=lambda r: (r["freq_hz"], r["call"]))
            # mark our position: on the stations we are tuned to (within
            # 200 Hz), or else on a line of its own between its neighbours
            on = [r for r in rows if abs(r["freq_hz"] - here) <= 200]
            for row in on:
                row["here"] = True
            if here and not on:
                at = sum(r["freq_hz"] < here for r in rows)
                rows.insert(at, {"kind": "here", "call": "", "freq_hz": here,
                                 "time": "", "comment": f"<<< row {self.row}",
                                 "worked": False, "here": True})
        else:
            rows.sort(key=lambda r: r["seen"], reverse=True)
        # frequencies right-aligned among themselves, flush with the header
        width = max((len(f"{r['freq_hz'] / 1000:.1f}") for r in rows), default=0)
        for row in rows:
            flag = ">" if row.get("here") else "*" if row["kind"] == "stored" \
                else " "
            row["text"] = (f"{flag}{row['freq_hz'] / 1000:>{width}.1f} "
                           f"{row['call']:<12} {row['time']:<4} "
                           f"{row['comment']}").rstrip()
        return rows

    def spot_step(self, step):
        """PgDn/PgUp: move the highlight through the panel. In the band map
        the first step starts from the current frequency."""
        rows = [r for r in self.panel_rows() if r["kind"] != "here"]
        calls = [r["call"] for r in rows]
        if not calls:
            self.msg = "Nothing in the panel on this band."
            return
        if self.spot in calls:
            index = min(max(calls.index(self.spot) + step, 0), len(calls) - 1)
        elif self.view == "map":
            here = self.freq() or 0
            above = [i for i, r in enumerate(rows) if r["freq_hz"] > here]
            below = [i for i, r in enumerate(rows) if r["freq_hz"] < here]
            index = (above[0] if above else len(rows) - 1) if step > 0 \
                else (below[-1] if below else 0)
        else:
            index = 0
        self.spot = calls[index]
        self.msg = (f"{self.spot} - Enter in an empty call field takes it"
                    + (", Del removes it." if rows[index]["kind"] == "stored"
                       else "."))

    def store_for_later(self):
        """F9 / Ctrl-J: note the station in the call field on the current
        frequency, to come back to it later."""
        call = self.val["call"]
        if not is_call(call):
            self.msg = "Type the callsign to store first."
        elif not band_of(self.freq() or 0):
            self.msg = "No amateur band frequency - cannot store."
        else:
            self.log.store_later(call, self.freq(), self.mode())
            self.msg = (f"Stored {call} on {fmt_freq(self.freq())} for later "
                        "(shown with * in the panel).")
            self.clear()

    def spot_delete(self):
        band = band_of(self.freq() or 0)
        if self.spot and self.log.delete_later(self.spot, band):
            self.msg, self.spot = f"Removed stored station {self.spot}.", ""
        else:
            self.msg = "Del removes a highlighted stored station (marked *)."

    def toggle_view(self):
        self.view = "map" if self.view == "newest" else "newest"
        self.msg = ("Band map: by frequency, with worked stations."
                    if self.view == "map" else "Panel: newest first.")

    def spot_send(self):
        """F6: spot the station in the call field, or else the last QSO."""
        call, hz = self.val["call"], self.freq()
        if not is_call(call):
            last = self.log.newest()
            if not last:
                self.msg = "Nothing to spot: type a callsign or log a QSO first."
                return
            call, hz = last["call"], last["freq_hz"]
        if not hz:
            self.msg = "No frequency for the spot."
            return
        if self.cluster.status != "connected":
            self.msg = "DX cluster is not connected."
            return

        def send(comment):
            error = self.cluster.send_spot(hz, call, comment.strip())
            self.msg = error or (f"Spot for {call} on {hz / 1000:.1f} kHz sent, "
                                 "waiting for the node ...")

        self.prompt = {"text": f"Spot {call} on {hz / 1000:.1f} kHz - comment "
                               "(Enter sends, Esc cancels): ",
                       "value": "", "action": send}

    def prompt_key(self, ch):
        prompt = self.prompt
        if ch in ("\n", "\r", curses.KEY_ENTER):
            self.prompt = None
            prompt["action"](prompt["value"])
        elif ch == "\x1b":
            self.prompt = None
            self.msg = "Cancelled."
        elif ch in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            prompt["value"] = prompt["value"][:-1]
        elif isinstance(ch, str) and ch.isprintable() and len(prompt["value"]) < 30:
            prompt["value"] += ch

    def spot_take(self):
        """Tune the current row to the highlighted entry and fill in its call."""
        for spot in self.panel_rows():
            if spot["kind"] != "here" and spot["call"] == self.spot:
                self.set_freq(spot["freq_hz"])
                if (spot["kind"] == "stored" and spot["mode"] in TYPED_MODES
                        and spot["mode"] != self.mode()):
                    self.set_mode(spot["mode"])
                self.val["call"] = spot["call"]
                self.qrz.request(spot["call"])
                self.msg = (f"Row {self.row}: {spot['call']} on "
                            f"{fmt_freq(spot['freq_hz'])}")
                self.spot = ""
                return True
        return False

    def score_text(self):
        if not self.scoring["configured"]:
            return ""
        r = self.score()
        if self.scoring["mults"]:
            return f"Score: {r['points']}x{r['mults']}={r['score']} | "
        return f"Score: {r['score']} | "

    def draw(self, scr):
        scr.erase()
        h, w = scr.getmaxyx()
        cursor = (h - 5, ENTRY_X)

        def put(y, x, text, attr=0):
            if 0 <= y < h and 0 <= x < w:
                try:
                    scr.addstr(y, x, text[:w - x - 1], attr)
                except curses.error:
                    pass

        rig = "RIG ok" if self.rig.online else "RIG offline (manual)"
        utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + "Z"
        head = (f" {self.mycall} {self.cfg['contest']['contest']} | "
                f"Row {self.row} {band_of(self.freq() or 0) or '-'} "
                f"{self.mode()} | {rig} | {self.score_text()}"
                f"QSOs: {self.log.count()} | {utc} ")
        put(0, 0, head.ljust(w - 1), curses.A_REVERSE)

        # QSO list, with the edited QSO shown as editable cells
        extra = 1 if self.history.path else 0     # line for "worked before"
        mult_rows = self.mult_panel(w - 2)
        if h - 13 - extra - len(mult_rows) < 3:   # terminal too small for it
            mult_rows = []
        space = max(h - 12 - extra - (len(mult_rows) + 1 if mult_rows else 0), 1)
        for n, row in enumerate(mult_rows):
            x = 1
            for text, worked in row:
                put(h - 9 - extra - len(mult_rows) + n, x, text,
                    self.good_attr if worked else 0)
                x += len(text) + 1
        # DX cluster panel to the right of the QSO list
        # it takes the space right of the list columns, up to 60 columns, so
        # that spot comments fit; what is left beyond that goes to the names
        panel_w = min(w - 112, 60)
        panel_x = w - panel_w if w >= 142 else None
        head = (f"{'#':>4} " + " ".join(f"{title:<{width}}"
                                         for _, width, title in COLUMNS)
                + f" {'km':>5} {'Pts':<6}")
        if not panel_x or panel_x - 3 >= len(head) + 4:
            head += "Name"
        put(2, 1, head, curses.A_BOLD)
        if panel_x:
            band = band_of(self.freq() or 0) or "?"
            # rows start with a one-character flag column, hence the +1
            put(2, panel_x + 1, f"Band map {band} (F7: newest first)"
                if self.view == "map" else f"Newest {band} (F7: band map)",
                curses.A_BOLD)
            spots = self.panel_rows()
            line = 3
            if self.cluster.host and self.cluster.status != "connected":
                put(line, panel_x + 1,
                    f"cluster {self.cluster.status}"[:panel_w - 3])
                line += 1
            elif not spots:
                put(line, panel_x + 1, "nothing on this band yet")
            room = space - (line - 3)
            calls = [spot["call"] for spot in spots]
            kinds = ["here" if spot.get("here") else spot["kind"]
                     for spot in spots]
            first = 0
            if self.spot and self.spot in calls:    # keep the highlight in view
                first = max(calls.index(self.spot) - room + 1, 0)
            elif "here" in kinds:                   # else centre on our frequency
                first = max(min(kinds.index("here") - room // 2,
                                len(spots) - room), 0)
            for spot in spots[first:first + room]:
                attr = (curses.A_REVERSE if spot["kind"] != "here"
                        and spot["call"] == self.spot else
                        curses.A_DIM if spot["worked"] else
                        curses.A_BOLD if spot.get("here")
                        or spot["kind"] == "stored" else 0)
                put(line, panel_x, spot["text"][:panel_w - 2], attr)
                line += 1
        per_qso = self.score()["per_qso"]
        rows = self.log.rows()
        end = len(rows)
        if self.edit:                    # keep the edited QSO in view
            ids = [q["id"] for q in rows]
            if self.edit["id"] in ids:
                end = max(min(ids.index(self.edit["id"]) + 1 + space // 2,
                              len(rows)), min(space, len(rows)))
        for i, q in enumerate(rows[max(end - space, 0):end]):
            points, is_dupe, is_mult = per_qso.get(q["id"], (0, False, False))
            pts = "DUPE" if is_dupe else f"{points}{'+M' if is_mult else ''}"
            editing = self.edit and q["id"] == self.edit["id"]
            texts = self.eval if editing else cells(q)
            put(3 + i, 1, f"{q['id']:>4}", curses.A_BOLD if editing else 0)
            x = 6
            for n, (name, width, _) in enumerate(COLUMNS):
                attr = 0
                if editing:
                    attr = curses.A_REVERSE if n == self.efocus \
                        else curses.A_UNDERLINE
                    if n == self.efocus:
                        cursor = (3 + i, x + min(len(texts[name]), width - 1))
                put(3 + i, x, texts[name][:width].ljust(width), attr)
                x += width + 1
            km = distance_km(self.my_loc, texts["loc"])
            tail = f"{'' if km is None else km:>5} {pts:<6}{q['name']}"
            put(3 + i, x, tail[:panel_x - x - 2] if panel_x else tail)

        # hints for the active entry row (or the edited QSO)
        call = self.eval["call"] if self.edit else self.val["call"]
        if extra:
            known = self.history.lookup(call) if is_call(call) else None
            if known:
                put(h - 9, 1, self.history.text(known), curses.A_BOLD)
            elif is_call(call):
                put(h - 9, 1, "Worked before: no")
        info = self.qrz.info(call) if is_call(call) else None
        if not self.qrz.user:
            line = "QRZ: no username configured"
        elif not info:
            line = "QRZ: (Tab or Space after the callsign starts the lookup)"
        elif info.get("pending"):
            line = f"QRZ: looking up {base_call(call)} ..."
        elif "error" in info:
            line = f"QRZ: {info['error']}"
        else:
            line = "QRZ: " + "  |  ".join(
                x for x in (info["name"], info["qth"], info["country"],
                            info["grid"]) if x)
        loc = self.eval["loc"] if self.edit else self.val["loc"]
        km = distance_km(self.my_loc, loc)
        if km is not None:
            line += f"   [{loc}: {km} km]"
        put(h - 8, 1, line, curses.A_BOLD)
        dupe = self.dupe()
        if dupe:
            put(h - 7, 1, " " + self.dupe_text(dupe) + " ",
                self.warn_attr | curses.A_BOLD)
        elif self.new_mult():
            put(h - 7, 1, f" NEW MULTIPLIER: {self.new_mult()} ",
                self.good_attr | curses.A_BOLD)

        # two entry rows, one per VFO
        put(h - 6, 1, "Row   Frequency  Mode")
        x = ENTRY_X
        for name in FIELDS:
            put(h - 6, x, LABELS[name])
            x += max(WIDTHS[name], len(LABELS[name])) + 2
        put(h - 6, x, "Exch sent")
        for line, vfo in ((h - 5, "A"), (h - 4, "B")):
            active = vfo == self.row and not self.edit
            put(line, 1, f"{'>' if vfo == self.row else ' '}{vfo}",
                curses.A_REVERSE | curses.A_BOLD if vfo == self.row else 0)
            put(line, 5, f"{fmt_freq(self.freq(vfo)):>11}  {self.mode(vfo):<5}",
                curses.A_BOLD if vfo == self.row else 0)
            entry = self.entry[vfo]
            x = ENTRY_X
            for i, name in enumerate(FIELDS):
                shown = entry["val"][name]
                if not shown and name.startswith("rst"):
                    shown = self.default_rst(vfo)
                attr = curses.A_REVERSE if active and i == entry["focus"] \
                    else curses.A_UNDERLINE
                put(line, x, shown.ljust(WIDTHS[name]), attr)
                if active and i == entry["focus"]:
                    cursor = (line, x + len(entry["val"][name]))
                x += max(WIDTHS[name], len(LABELS[name])) + 2
            put(line, x, self.exch_sent() or "-")

        if self.prompt:
            line = self.prompt["text"] + self.prompt["value"]
            put(h - 2, 1, line, curses.A_BOLD)
            cursor = (h - 2, min(1 + len(line), w - 2))
        else:
            put(h - 2, 1, self.msg)
        put(h - 1, 0, " Enter log | Tab next | Esc clear | \\ row A/B | "
                      "<kHz>/<mode> Enter QSY | Up edit | PgUp/PgDn panel | "
                      "F6 spot | F7 map | F9 store | F8 delete | "
                      "Alt-M menu (export, ATU, quit) ".ljust(w - 1),
            curses.A_REVERSE)
        if self.menu is not None:        # menu box in the middle of the screen
            width = max(len(title) for _, title, _ in self.MENU) + 10
            top, left = max((h - len(self.MENU) - 2) // 2, 0), max((w - width) // 2, 0)
            put(top, left, "+" + " Menu ".center(width - 2, "-") + "+")
            for i, (hot, title, _) in enumerate(self.MENU):
                put(top + 1 + i, left, "|" + " " * (width - 2) + "|")
                put(top + 1 + i, left + 2, f"{hot.upper()}  {title}".ljust(width - 4),
                    curses.A_REVERSE if i == self.menu else 0)
            put(top + 1 + len(self.MENU), left, "+" + "-" * (width - 2) + "+")
            cursor = (top + 1 + self.menu, left + 2)
        try:
            scr.move(*cursor)
        except curses.error:
            pass
        scr.refresh()

    def run(self, scr):
        if hasattr(curses, "set_escdelay"):
            curses.set_escdelay(25)
        self.warn_attr = self.good_attr = curses.A_REVERSE
        if curses.has_colors():
            curses.start_color()
            curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_RED)
            self.warn_attr = curses.color_pair(1)
            curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_GREEN)
            self.good_attr = curses.color_pair(2)
        curses.nonl()        # Enter arrives as CR, so Ctrl-J (LF) is its own key
        curses.curs_set(1)
        scr.keypad(True)
        scr.timeout(200)
        self.rig.start()
        self.cluster.start()
        while True:
            if self.rig.online and self.rig.vfo != self.row:
                self.row = self.rig.vfo      # A/B was pressed on the radio
            if self.rig.tune_info:
                self.msg, self.rig.tune_info = self.rig.tune_info, ""
            self.sync_prefill()
            if self.cluster.reply:
                self.msg, self.cluster.reply = self.cluster.reply, ""
            self.draw(scr)
            try:
                ch = scr.get_wch()
            except curses.error:
                continue                 # timeout: just redraw
            except KeyboardInterrupt:
                return
            if ch == curses.KEY_RESIZE:
                continue
            keys = [ch]
            if ch == "\x1b":             # Alt-M arrives as Esc directly followed by m
                scr.timeout(30)
                try:
                    follow = scr.get_wch()
                    keys = ["ALT-M"] if follow in ("m", "M") else [ch, follow]
                except curses.error:
                    pass                 # a plain Esc
                scr.timeout(200)
            for one in keys:
                if not self.key(scr, one):
                    return


def main():
    ap = argparse.ArgumentParser(description="Simple terminal contest logger")
    ap.add_argument("contest", nargs="?",
                    help="contest name (config in contests/<name>.ini) "
                         "or path to a contest .ini file")
    ap.add_argument("logfile", nargs="?",
                    help="SQLite log file (default: <contest>.db)")
    ap.add_argument("--config-dir", default=DEFAULT_CONFIG_DIR)
    ap.add_argument("--export-adif", metavar="FILE")
    ap.add_argument("--export-cabrillo", metavar="FILE")
    ap.add_argument("--score", action="store_true",
                    help="print the score and exit")
    args = ap.parse_args()

    if not args.contest:
        known = list_contests(args.config_dir)
        ap.print_usage()
        sys.exit("Configured contests: " + (", ".join(known) or "none yet")
                 + "\nStart with: llog.py <contest>  (a new name creates it)")
    cfg = load_config(args.config_dir, args.contest)
    if not cfg["station"]["call"]:
        sys.exit(f"No callsign configured in {args.config_dir}/config.ini")
    if not args.logfile:
        stem = os.path.splitext(os.path.basename(args.contest))[0].lower()
        args.logfile = stem + ".db"
    dupes, scoring = dupe_rules(cfg), score_rules(cfg)   # fail early
    rig_retune(cfg)
    my_locator(cfg)
    load_history(cfg)
    log = Log(args.logfile)
    if args.score:
        print(score_report(compute_score(log.rows(), dupes, scoring), scoring))
    if args.score or args.export_adif or args.export_cabrillo:
        if args.export_adif:
            print("ADIF:", log.export_adif(args.export_adif, my_locator(cfg)))
        if args.export_cabrillo:
            print("Cabrillo:", log.export_cabrillo(args.export_cabrillo, cfg))
        return
    curses.wrapper(App(cfg, log).run)
    print(f"{log.count()} QSOs in {args.logfile}")
    if scoring["configured"]:
        print(score_report(compute_score(log.rows(), dupes, scoring), scoring))


if __name__ == "__main__":
    main()
