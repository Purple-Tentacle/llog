#!/usr/bin/env python3
"""llog - a contest logger for the Linux terminal.

Needs only Python 3.9+ (standard library). Radio control goes through
Hamlib's rigctld, which llog can set up and start itself; callsign lookup
through the QRZ.com XML API. See README.md.

    python3 llog.py <contest> [logfile.db]
"""
import argparse
import configparser
import curses
import fcntl
import fnmatch
import getpass
import math
import os
import queue
import re
import select
import socket
import sqlite3
import struct
import subprocess
import sys
import termios
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


def great_circle(lat1, lon1, lat2, lon2):
    """(distance in km, initial bearing in degrees) from point 1 to point 2
    along the short path."""
    lat1, lon1, lat2, lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    a = (math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2)
         * math.sin((lon2 - lon1) / 2) ** 2)
    y = math.sin(lon2 - lon1) * math.cos(lat2)
    x = (math.cos(lat1) * math.sin(lat2)
         - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1))
    return (round(2 * 6371 * math.asin(math.sqrt(a))),
            round(math.degrees(math.atan2(y, x))) % 360)


def distance_km(loc_a, loc_b):
    """Great-circle distance between two locators in km, or None."""
    if not (is_locator(loc_a) and is_locator(loc_b)):
        return None
    return great_circle(*locator_position(loc_a), *locator_position(loc_b))[0]


def bearing_deg(loc_a, loc_b):
    """Direction from locator a to locator b in degrees (0 = north,
    90 = east), or None. The same locator on both sides has no direction."""
    if not (is_locator(loc_a) and is_locator(loc_b)):
        return None
    km, deg = great_circle(*locator_position(loc_a), *locator_position(loc_b))
    return deg if km else None


def paths(deg):
    """Short-path and long-path bearing as text; the long path is the
    opposite direction."""
    return f"SP {deg}° / LP {(deg + 180) % 360}°"


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


class CountryFile:
    """Callsign -> DXCC country, continent and zones, from a cty.dat file
    (the "country file" used by most contest loggers, www.country-files.com).
    """
    # cty.dat also lists some non-DXCC areas (marked *); their DXCC country
    WAE = {"*IG9": "I", "*IT9": "I", "*TA1": "TA", "*GM/s": "GM",
           "*JW/b": "JW", "*4U1V": "OE"}
    MODIFIERS = {"P", "M", "QRP", "A", "AM", "B", "LH", "PM"}

    def __init__(self, path):
        self.path = path
        self.prefixes, self.exact = {}, {}
        with open(path, encoding="utf-8", errors="replace") as source:
            text = source.read()
        for block in text.split(";"):
            fields = block.strip().split(":", 8)
            if len(fields) < 9:
                continue
            name, cq, itu, cont = (f.strip() for f in fields[:4])
            prefix = fields[7].strip()
            try:                    # cty.dat counts longitude positive to the west
                lat, lon = float(fields[4]), -float(fields[5])
            except ValueError:
                lat = lon = None
            base = {"name": name, "cont": cont, "cq": cq.lstrip("0"),
                    "lat": lat, "lon": lon,
                    "itu": itu.lstrip("0"),
                    "prefix": self.WAE.get(prefix, prefix.lstrip("*"))}
            for alias in fields[8].replace("\n", "").split(","):
                alias = alias.strip()
                if not alias:
                    continue
                info = dict(base)
                cq_over = re.search(r"\((\d+)\)", alias)
                cont_over = re.search(r"\{(\w+)\}", alias)
                if cq_over:
                    info["cq"] = cq_over.group(1).lstrip("0")
                if cont_over:
                    info["cont"] = cont_over.group(1)
                alias = re.sub(r"\(.*?\)|\[.*?\]|<.*?>|\{.*?\}|~.*?~", "", alias)
                if alias.startswith("="):
                    self.exact[alias[1:].upper()] = info
                else:
                    self.prefixes[alias.upper()] = info
        self.longest = max(map(len, self.prefixes), default=0)

    def lookup(self, call):
        """Country data for a callsign, or None (unknown, maritime mobile)."""
        call = call.upper()
        if call in self.exact:
            return self.exact[call]
        parts = [p for p in call.split("/") if p]
        if "MM" in parts[1:]:
            return None
        parts = [p for i, p in enumerate(parts)
                 if i == 0 or (p not in self.MODIFIERS and not p.isdigit())]
        # EA8/DL1ABC and DL1ABC/EA8: the shorter part names the country
        part = min(parts, key=len) if parts else ""
        for size in range(min(len(part), self.longest), 0, -1):
            if part[:size] in self.prefixes:
                return self.prefixes[part[:size]]
        return None


_country_files = {}


def load_countries(cfg, needed=False):
    """The cty.dat named by country_file in config.ini, or else
    ~/.config/llog/cty.dat or cty.dat next to llog.py if one is there."""
    named = cfg["station"].get("country_file", "").strip()
    candidates = [os.path.expanduser(named)] if named else [
        os.path.join(cfg["station"].get("config_dir", DEFAULT_CONFIG_DIR),
                     "cty.dat"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "cty.dat")]
    for path in candidates:
        if path in _country_files:
            return _country_files[path]
        if os.path.exists(path):
            try:
                _country_files[path] = CountryFile(path)
            except OSError as exc:
                sys.exit(f"country_file {path}: {exc}")
            return _country_files[path]
    if named or needed:
        sys.exit("This contest is scored by country or continent and needs a "
                 "country file.\nDownload cty.dat from www.country-files.com "
                 "and save it as " + candidates[0])
    return None


PORTABLE = {"P", "M", "MM", "AM", "QRP", "A", "B", "LH", "PM"}


def wpx_prefix(call):
    """Prefix as used for prefix multipliers (the WPX definition): the
    start of the callsign up to its last digit. DL1ABC -> DL1,
    DL2025X -> DL2025, DL1ABC/7 -> DL7, EA8/DL1ABC -> EA8, LX/DL1ABC -> LX0."""
    def lead(text):
        match = re.match(r"^(.*\d)[A-Z]*$", text)
        return match.group(1) if match else text + "0"

    parts = [p for p in call.upper().split("/") if p]
    if not parts:
        return ""
    extra = [p for p in parts[1:] if p not in PORTABLE]
    if not extra:
        return lead(parts[0])
    if extra[0].isdigit():                       # DL1ABC/7: call area changed
        return re.sub(r"\d+$", "", lead(parts[0])) + extra[0]
    return lead(min(parts[0], extra[0], key=len))  # the country designator


def show_mult(value):
    """Multiplier value for display (prefixes are stored as 'pfx:DL1')."""
    return value.split(":", 1)[-1]


BAND_NAMES = {name.upper() for _, _, name, _ in BANDS}
RELATIONS = ("SAME", "DX", "OWN")     # same continent / other continent / own country


def score_rules(cfg):
    """Scoring settings of the contest file, see README "Scoring"."""
    con = cfg["contest"]
    points = []                      # (set of conditions, points)
    try:
        for item in con.get("points", "").upper().replace(",", " ").split():
            conds, value = item.rsplit(":", 1)
            points.append((frozenset(c for c in conds.split("+") if c != "*"),
                           int(value)))
    except ValueError:
        sys.exit("points must look like 'FM:2, SSB:4, CW:6' or "
                 "'SAME:1, DX:2, 40M+SAME:3, 40M+DX:6'")
    mults = con.get("multipliers", "").upper().replace(",", " ").split()
    per = con.get("mult_per", "band").strip().lower()
    if per not in ("band", "contest"):
        sys.exit("mult_per must be 'band' or 'contest'")
    try:
        mult_country = con.getboolean("mult_country", False)
    except ValueError:
        sys.exit("mult_country must be yes or no")
    mult_calls = con.get("mult_calls", "").upper().replace(",", " ").split()
    try:
        mult_prefix = con.getboolean("mult_prefix", False)
    except ValueError:
        sys.exit("mult_prefix must be yes or no")
    exclude = con.get("mult_exclude", "").upper().replace(",", " ").split()
    by_country = (mult_country or bool(mult_calls)
                  or any(conds & set(RELATIONS) for conds, _ in points))
    countries = load_countries(cfg, needed=by_country)
    home = countries.lookup(cfg["station"]["call"]) if countries else None
    if home and home["prefix"].upper() in mult_calls:
        # operating from one of these countries yourself: its stations are
        # then no multipliers of their own (OK DX RTTY: "OK/OL stations:
        # DXCC countries on each band")
        mult_calls = []
    return {"configured": bool(points or mults or mult_country or mult_calls
                               or mult_prefix),
            "mult_prefix": mult_prefix, "mult_exclude": exclude,
            "points": points or [(frozenset(), 1)],
            "mults": mults, "mult_per": per, "mult_country": mult_country,
            "mult_calls": mult_calls,
            "has_mults": bool(mults or mult_country or mult_calls or mult_prefix),
            "countries": countries, "home": home}


def qso_points(rules, call, mode, freq_hz):
    """Points of one QSO: the most specific matching entry of 'points'
    (most conditions; the later one on a tie). No match = 0 points."""
    facts = {mode, mode_group(mode), band_of(freq_hz or 0).upper()}
    them = rules["countries"].lookup(call) if rules["countries"] else None
    if them and rules["home"]:
        facts.add("SAME" if them["cont"] == rules["home"]["cont"] else "DX")
        if them["prefix"] == rules["home"]["prefix"]:
            facts.add("OWN")
    best, value = -1, 0
    for conds, pts in rules["points"]:
        if conds <= facts and len(conds) >= best:
            best, value = len(conds), pts
    return value


def mult_key(rules, exch, freq_hz):
    """Multiplier the received exchange earns as (band, value), or None. The
    whole exchange is tried first, then each word of it, so '001 N13' counts
    as the multiplier N13."""
    text = (exch or "").strip().upper()

    def excluded(value):             # '#' in mult_exclude = any plain number
        return any(value.isdigit() if pat == "#" else
                   fnmatch.fnmatchcase(value, pat)
                   for pat in rules.get("mult_exclude", ()))

    for value in [text] + text.split():
        if (value and not excluded(value) and
                any(fnmatch.fnmatchcase(value, pat) for pat in rules["mults"])):
            band = band_of(freq_hz or 0) if rules["mult_per"] == "band" else ""
            return (band, value)
    return None


def qso_mults(rules, call, exch, freq_hz):
    """All multipliers one QSO can earn, as (band, value) keys: from the
    exchange, its DXCC country (mult_country) and the station itself if it
    is in one of the mult_calls countries."""
    band = band_of(freq_hz or 0) if rules["mult_per"] == "band" else ""
    keys = [mult_key(rules, exch, freq_hz)]
    if rules.get("mult_prefix") and wpx_prefix(call):
        keys.append((band, "pfx:" + wpx_prefix(call)))
    them = rules["countries"].lookup(call) if rules["countries"] else None
    if them and rules["mult_country"]:
        keys.append((band, them["prefix"]))
    if them and them["prefix"].upper() in rules["mult_calls"]:
        keys.append((band, base_call(call)))
    return [k for k in keys if k]


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
            per_qso[q["id"]] = (0, True, 0)
            continue
        worked[key] = when
        points = qso_points(rules, q["call"], q["mode"], q["freq_hz"])
        new = set(qso_mults(rules, q["call"], q["exch_r"], q["freq_hz"])) - mults
        mults |= new
        stat["mults"] += len(new)
        stat["points"] += points
        per_qso[q["id"]] = (points, False, len(new))   # points, dupe, new mults
    total_points = sum(b["points"] for b in bands.values())
    return {"points": total_points, "mults": len(mults), "seen": mults,
            "score": total_points * len(mults) if rules["has_mults"]
            else total_points,
            "per_qso": per_qso, "bands": bands}


def score_report(result, rules):
    lines = [f"{'Band':<6}{'QSOs':>6}{'Dupes':>7}{'Points':>8}{'Mults':>7}"]
    for band, b in result["bands"].items():
        lines.append(f"{band or '?':<6}{b['qsos']:>6}{b['dupes']:>7}"
                     f"{b['points']:>8}{b['mults']:>7}")
    if rules["has_mults"]:
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


MORSE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.",
    "G": "--.", "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..",
    "M": "--", "N": "-.", "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.",
    "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
    "Y": "-.--", "Z": "--..", "0": "-----", "1": ".----", "2": "..---",
    "3": "...--", "4": "....-", "5": ".....", "6": "-....", "7": "--...",
    "8": "---..", "9": "----.", "/": "-..-.", "?": "..--..", ".": ".-.-.-",
    ",": "--..--", "=": "-...-", "+": ".-.-.", "-": "-....-"}
CW_WPM_UNKNOWN = 20     # assumed when the radio does not tell its keyer speed


def cw_seconds(text, wpm):
    """How long a CW text takes at the given speed (PARIS timing: a dot is
    1.2 / wpm seconds, a dash 3 dots, gaps 1 / 3 / 7 dots)."""
    units = 0
    words = text.upper().split()
    for word in words:
        for letter in word:
            code = MORSE.get(letter, "")
            units += sum(1 if e == "." else 3 for e in code) + max(len(code) - 1, 0)
        units += 3 * max(len(word) - 1, 0)
    units += 7 * max(len(words) - 1, 0)
    return units * 1.2 / (wpm or CW_WPM_UNKNOWN)


def ascii_only(text):
    return unicodedata.normalize("NFKD", text or "").encode(
        "ascii", "ignore").decode()


# ------------------------------------------------------------- radio setup
def rigctl_models(command="rigctl"):
    """Hamlib model numbers by model name, read from 'rigctl -l'."""
    try:
        listing = subprocess.run([command, "-l"], capture_output=True,
                                 text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    models = {}
    for line in listing.splitlines():
        match = re.match(r"\s*(\d+)\s+(.*?)\s{2,}(\S.*?)\s{2,}", line)
        if match:
            models.setdefault(match.group(3).strip().upper(), match.group(1))
    return models


def elecraft_model(options):
    """Radio model from the answer to the OM command. KX2 and KX3 answer
    'OM APF---TBXI0n;' with n = 1 for the KX2 and 2 for the KX3; the K3
    answers with option letters only, and an R among them marks a K3S."""
    options = options.strip()
    if options.endswith("01;"):
        return "KX2"
    if options.endswith("02;"):
        return "KX3"
    return "K3S" if "R" in options[2:] else "K3"


def probe_elecraft(device):
    """Asks a serial port whether an Elecraft radio is listening.
    Returns (model name, baud rate), (None, error text) or (None, "")."""
    speeds = ((38400, termios.B38400), (19200, termios.B19200),
              (9600, termios.B9600), (4800, termios.B4800))
    try:
        fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as exc:
        return None, exc.strerror or str(exc)
    try:
        # keep DTR and RTS low: some radios use them for PTT or keying
        fcntl.ioctl(fd, termios.TIOCMBIC,
                    struct.pack("I", termios.TIOCM_DTR | termios.TIOCM_RTS))

        def ask(command):
            termios.tcflush(fd, termios.TCIOFLUSH)
            os.write(fd, command)
            answer, end = b"", time.time() + 0.4
            while time.time() < end and not answer.endswith(b";"):
                if select.select([fd], [], [], 0.05)[0]:
                    answer += os.read(fd, 100)
            return answer.decode("ascii", "replace")

        for baud, flag in speeds:
            attrs = termios.tcgetattr(fd)
            attrs[0] = attrs[1] = attrs[3] = 0                  # raw
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[4] = attrs[5] = flag
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            if "ID017;" not in ask(b"ID;"):
                continue
            return elecraft_model(ask(b"OM;")), baud
        return None, ""
    except (OSError, termios.error) as exc:
        return None, str(exc)
    finally:
        os.close(fd)


def detect_radios(by_id="/dev/serial/by-id", probe=probe_elecraft):
    """Radios on the serial ports: (list of settings dicts, list of notes).
    Icom radios are recognised by their USB name, Elecraft radios by asking
    the device behind each USB serial adapter."""
    found, notes = [], []
    try:
        names = sorted(os.listdir(by_id))
    except OSError:
        return found, ["No USB serial device found (" + by_id + " is missing)."]
    seen = set()
    for name in names:
        path = os.path.join(by_id, name)
        icom = re.search(r"Icom.*?(IC-[0-9A-Za-z]+)", name)
        if icom:
            if icom.group(1) in seen:    # second port of the same radio
                continue
            seen.add(icom.group(1))
            found.append({"radio": icom.group(1).upper(), "device": path,
                          "speed": ""})
            continue
        model, info = probe(path)
        if model:
            found.append({"radio": model, "device": path, "speed": str(info)})
        elif info:
            notes.append(f"{name}: {info}")
    return found, notes


def set_ini_values(path, section, values):
    """Sets key = value lines in one section of an ini file and leaves
    everything else, comments included, as it is."""
    try:
        with open(path, encoding="utf-8") as source:
            lines = source.read().splitlines()
    except OSError:
        lines = []
    todo = dict(values)
    out, inside, done = [], False, False
    for line in lines + ["[__end__]"]:
        header = re.match(r"\s*\[(.+?)\]", line)
        if header:
            if inside:                           # leaving our section
                while out and not out[-1].strip():
                    out.pop()
                out += [f"{k} = {v}" for k, v in todo.items()] + [""]
                todo, done = {}, True
            inside = header.group(1) == section
        elif inside:
            key = line.split("=", 1)[0].strip()
            if "=" in line and key in todo:
                line = f"{key} = {todo.pop(key)}"
        out.append(line)
    out.pop()                                    # the [__end__] marker
    if not done:
        out += [f"[{section}]"] + [f"{k} = {v}" for k, v in todo.items()]
    with open(path, "w", encoding="utf-8") as target:
        target.write("\n".join(out).rstrip("\n") + "\n")


class Rigctld:
    """Starts and stops a rigctld that llog runs itself."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.proc = None
        self.radio_missing = False
        self.log_path = os.path.join(
            cfg["station"].get("config_dir", DEFAULT_CONFIG_DIR), "rigctld.log")

    def model_number(self, radio):
        """Hamlib model number for a radio name such as KX2, or ''."""
        command = self.cfg["rig"].get("rigctld_command", "rigctld").strip()
        rigctl = os.path.join(os.path.dirname(command), "rigctl")   # same folder
        return rigctl_models(rigctl).get(radio.strip().upper(), "")

    def reachable(self):
        rig = self.cfg["rig"]
        try:
            socket.create_connection((rig["host"], rig.getint("port")),
                                     timeout=0.5).close()
            return True
        except (OSError, ValueError):
            return False

    def ours(self):
        return self.proc is not None and self.proc.poll() is None

    def describe(self):
        rig = self.cfg["rig"]
        if not rig.get("rigctld_device", "").strip():
            return "not set up yet - use Detect"
        speed = rig.get("rigctld_speed", "").strip()
        return (f"{rig.get('rigctld_radio', '') or 'model ' + rig.get('rigctld_model', '?')}"
                f" on {os.path.basename(rig['rigctld_device'])[:34]}"
                + (f", {speed} Bd" if speed else ""))

    def state(self):
        if self.ours():
            return "running (started by llog)"
        return "running (started elsewhere)" if self.reachable() else "not running"

    def start(self):
        """Returns a text for the message line. self.radio_missing tells
        whether a failure looks like 'the saved radio is not there'."""
        rig = self.cfg["rig"]
        device = rig.get("rigctld_device", "").strip()
        self.radio_missing = False
        if self.ours() or self.reachable():
            return "rigctld is already running."
        if not device:
            self.radio_missing = True
            return "No radio set up yet: menu > Radio > Detect radio."
        if not os.path.exists(device):
            self.radio_missing = True
            return (f"The saved radio ({self.describe()}) is not connected: "
                    "menu > Radio > Detect radio.")
        if rig["host"] not in ("127.0.0.1", "localhost", "::1"):
            return "rigctld host in config.ini is another computer - not started."
        command = rig.get("rigctld_command", "rigctld").strip() or "rigctld"
        model = rig.get("rigctld_model", "").strip()
        if not model.isdigit():          # look the number up from the name
            model = self.model_number(rig.get("rigctld_radio", ""))
        if not model:
            return ("Hamlib model number unknown - is Hamlib installed? "
                    "Set rigctld_model in config.ini.")
        args = [command, "-m", model, "-r", device, "-t", rig["port"]]
        if rig.get("rigctld_speed", "").strip():
            args += ["-s", rig["rigctld_speed"].strip()]
        args += rig.get("rigctld_options", "").split()
        try:
            with open(self.log_path, "w") as log:
                log.write(" ".join(args) + "\n")
                log.flush()
                self.proc = subprocess.Popen(args, stdout=log, stderr=log,
                                             stdin=subprocess.DEVNULL)
        except OSError as exc:
            self.proc = None
            return f"Could not start {command}: {exc.strerror or exc}"
        end = time.time() + 4
        while time.time() < end:
            if self.proc.poll() is not None:
                self.radio_missing = True        # wrong radio or switched off
                return "rigctld stopped at once: " + self.last_log_line()
            if self.reachable():
                return f"rigctld started ({self.describe()})."
            time.sleep(0.1)
        return "rigctld started but does not answer yet - see " + self.log_path

    def last_log_line(self):
        try:
            with open(self.log_path) as log:
                lines = [line.strip() for line in log if line.strip()]
            return lines[-1][:120] if len(lines) > 1 else "no message"
        except OSError:
            return "no message"

    def stop(self):
        if not self.ours():
            self.proc = None
            return ("rigctld was not started by llog - stop it where you "
                    "started it." if self.reachable() else "rigctld is not running.")
        self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None
        return "rigctld stopped."


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
        self.model = ""                         # radio name reported by rigctld
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

    wpm = 0      # keyer speed of the radio, 0 while unknown

    # ATU tune commands by Hamlib model name; anything else gets Hamlib's
    # generic tune operation
    TUNE_COMMANDS = {"KX2": "w SWT20;", "KX3": "w SWT44;"}

    def send_cw(self, text):
        """Lets the radio key a CW text (Hamlib 'send_morse'). Runs on its
        own connection: the answer only comes when the radio took the text."""
        threading.Thread(target=self._send_cw, args=(text,), daemon=True).start()

    def _send_cw(self, text):
        # the keyer speed tells how long the text takes (see cw_seconds)
        try:
            answer = self._raw(f"l{self._vfo_arg(self.vfo)} KEYSPD").split()
            if answer and answer[0].isdigit() and 5 <= int(answer[0]) <= 100:
                self.wpm = int(answer[0])
        except OSError:
            pass
        self._cw(f"b{self._vfo_arg(self.vfo)} {text}", f"CW: {text}")

    ELECRAFT = ("KX2", "KX3", "K3", "K3S", "K4")

    def stop_cw(self):
        """Ends a CW message that is being sent. Elecraft radios get their
        own 'back to receive' command, which also empties the text buffer;
        other radios Hamlib's stop_morse."""
        command = (f"T{self._vfo_arg(self.vfo)} 0" if self.model.upper() in self.ELECRAFT
                   else "\\stop_morse")
        threading.Thread(target=self._cw, args=(command, "CW stopped.",
                                                "CW not stopped"), daemon=True).start()

    # ---- voice memories of the radio (SSB): llog only starts and stops them
    # KX2 / KX3: a tap on MSG, then on the switch that carries the number
    # rigctld waits a few seconds for a reply to such a raw command, which
    # the radio never gives, and serves no other command meanwhile. So raw
    # commands are only used where Hamlib has no command of its own.
    VOICE_SWITCHES = {1: "SWT11;SWT19;", 2: "SWT11;SWT27;"}
    _voice_id = 0         # counts the started messages; a stop makes it move on
    voice_busy = False    # a voice memory was started and is not finished
    voice_ok = True       # the last one was really transmitted
    voice_end = 0         # when the radio went back to receive

    def send_voice(self, number):
        """Lets the radio transmit its voice memory 1 or 2 and watches the
        transmit state, so that voice_end tells when it was finished."""
        self.voice_busy, self.voice_ok = True, True
        self._voice_id += 1
        threading.Thread(target=self._voice, args=(number, self._voice_id),
                         daemon=True).start()

    def _icom_raw(self, data):
        """A CI-V command for the IC-705 (default address A4), passed through."""
        frame = "".join(f"\\0x{b:02x}" for b in (0xFE, 0xFE, 0xA4, 0xE0, *data, 0xFD))
        self._raw("w " + frame)

    def _voice(self, number, ident):
        try:
            ok, problem = self._voice_run(number, ident), ""
        except OSError as exc:
            ok, problem = False, f"Voice memory not started: {exc}"
        if ident == self._voice_id:      # not stopped or replaced meanwhile
            self.voice_ok = ok
            self.tune_info = problem or self.tune_info or (
                f"Voice memory {number} sent." if ok else "")
            self.voice_end = time.time()
            self.voice_busy = False

    def _voice_run(self, number, ident=None):
        if ident is None:
            ident = self._voice_id
        model = (self.model or self._identify()).upper()
        if model in ("KX2", "KX3"):
            self._raw("w " + self.VOICE_SWITCHES[number])
        else:
            answer = self._raw(
                f"\\send_voice_mem{self._vfo_arg(self.vfo)} {number}").strip()
            if answer != "RPRT 0" and "705" in model:
                self._icom_raw((0x28, 0x00, number))
            elif answer != "RPRT 0":
                self.tune_info = (f"Voice memory not sent: rigctld answered "
                                  f"{answer or 'nothing'}")
                return False
        # follow the transmit state: on within 3 s, then off again.
        # rigctld drops a connection when the radio once fails to answer,
        # so a lost connection is opened again instead of giving up.
        started, seen, sock, failures = time.time(), False, None, 0
        try:
            while time.time() < started + 120 and ident == self._voice_id:
                try:
                    if sock is None:
                        sock = socket.create_connection(self.addr, timeout=4)
                    sock.sendall(f"t{self._vfo_arg(self.vfo)}\n".encode())
                    answer = sock.recv(100).decode("ascii", "replace").strip()
                    if not answer:
                        raise OSError("rigctld closed the connection")
                except OSError as exc:
                    if sock is not None:
                        sock.close()
                    sock, failures = None, failures + 1
                    if failures > 8:
                        self.tune_info = ("Voice memory started, but the transmit "
                                          f"state cannot be read: {exc}")
                        return False
                    time.sleep(0.3)
                    continue
                if answer.startswith("RPRT"):
                    # the radio did not answer this once; ask again
                    failures += 1
                    if failures > 8:
                        self.tune_info = ("Voice memory started, but the radio does "
                                          f"not tell when it transmits ({answer}).")
                        return False
                elif answer not in ("0", "1"):
                    self.tune_info = ("Voice memory started, but the radio does "
                                      "not tell when it transmits.")
                    return False
                else:
                    failures = 0
                    if answer == "1":
                        seen = True
                    elif seen:
                        return True
                    elif time.time() > started + 3:
                        self.tune_info = (f"Voice memory {number}: the radio did not "
                                          "transmit. Is the memory recorded?")
                        return False
                time.sleep(0.25)
        finally:
            if sock is not None:
                sock.close()
        return True

    def stop_voice(self):
        """Stops the voice message. llog is free for the next one at once,
        without waiting until the radio reports receive."""
        self._voice_id += 1              # ends the transmit watch
        self.voice_busy, self.voice_ok, self.voice_end = False, True, time.time()
        threading.Thread(target=self._stop_voice, daemon=True).start()

    def _stop_voice(self):
        try:
            model = (self.model or self._identify()).upper()
            if model in self.ELECRAFT:
                # Hamlib's "PTT off" sends RX; and returns at once
                self._raw(f"T{self._vfo_arg(self.vfo)} 0")
            elif self._raw("\\stop_voice_mem").strip() != "RPRT 0" and "705" in model:
                self._icom_raw((0x28, 0x00, 0x00))
            self.tune_info = "Voice message stopped."
        except OSError as exc:
            self.tune_info = f"Voice message not stopped: {exc}"

    def _cw(self, command, done, failed="CW not sent"):
        try:
            with socket.create_connection(self.addr, timeout=30) as sock:
                sock.sendall((command + "\n").encode())
                answer = sock.recv(200).decode("ascii", "replace").strip()
        except OSError as exc:
            self.tune_info = f"{failed}: {exc}"
            return
        self.tune_info = done if answer in ("", "RPRT 0") else (
            f"{failed}: rigctld answered {answer}")

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

    def _identify(self):
        """Asks rigctld which radio it controls (shown in the header)."""
        try:
            caps = self._raw("\\dump_caps", until=b"Mfg name")
        except OSError:
            return ""
        for line in caps.splitlines():
            if line.startswith("Model name:"):
                self.model = line.split(":", 1)[1].strip()
        return self.model

    def _tune(self, command):
        try:
            if command == "auto":
                model = self.model or self._identify()
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
                    # generous: rigctld is busy for seconds after a raw command
                    self._sock = socket.create_connection(self.addr, timeout=8)
                    self._file = self._sock.makefile("r")
                    self.model = ""
                    threading.Thread(target=self._identify, daemon=True).start()
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


# ----------------------------------------------------------- check partial
class PartialCheck:
    """Super check partial: known contest callsigns that contain what has
    been typed so far. The list comes from a MASTER.SCP file
    (www.supercheckpartial.com), one callsign per line."""
    NAMES = ("MASTER.SCP", "master.scp", "MASTER.DTA.SCP")

    def __init__(self, cfg):
        named = cfg["station"].get("scp_file", "").strip()
        folders = (cfg["station"].get("config_dir", DEFAULT_CONFIG_DIR),
                   os.path.dirname(os.path.abspath(__file__)))
        candidates = [os.path.expanduser(named)] if named else [
            os.path.join(folder, name) for folder in folders for name in self.NAMES]
        self.path, self.calls = "", set()
        for path in candidates:
            if os.path.exists(path):
                try:
                    with open(path, encoding="ascii", errors="ignore") as source:
                        self.calls = {line.strip().upper() for line in source
                                      if line.strip() and not line.startswith("#")}
                    self.path = path
                except OSError as exc:
                    sys.exit(f"scp_file {path}: {exc}")
                break
        if named and not self.path:
            sys.exit(f"scp_file {named} not found")
        self._last = (None, None, [])

    def match(self, text, extra=()):
        """Callsigns containing the text: the exact one first, then those
        starting with it, then the rest; each group in alphabetical order."""
        text = text.upper()
        if len(text) < 3:
            return []
        extra = frozenset(extra)
        if self._last[:2] != (text, extra):
            found = [c for c in self.calls | extra if text in c]
            found.sort(key=lambda c: (c != text, not c.startswith(text), c))
            self._last = (text, extra, found)
        return self._last[2]


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
        have = {row[1] for row in self.db.execute("PRAGMA table_info(later)")}
        for column in ("exch", "loc"):      # added later: upgrade older logs
            if column not in have:
                self.db.execute(f"ALTER TABLE later ADD COLUMN {column} TEXT "
                                "DEFAULT ''")
        self.db.commit()

    def store_later(self, call, freq_hz, mode, exch="", loc=""):
        self.db.execute(
            "INSERT OR REPLACE INTO later (call, band, freq_hz, mode, seen, "
            "exch, loc) VALUES (?,?,?,?,?,?,?)",
            (call, band_of(freq_hz), freq_hz, mode, time.time(), exch, loc))
        self.db.commit()

    def later_rows(self, band):
        names = ("call", "freq_hz", "mode", "seen", "exch", "loc")
        found = self.db.execute(f"SELECT {', '.join(names)} FROM later "
                                "WHERE band=?", (band,))
        return [dict(zip(names, r)) for r in found]

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

    def backup(self, folder="", keep=50):
        """Writes a consistent copy of the log file and removes old copies.
        Default folder: 'backups' next to the log file."""
        folder = os.path.expanduser(folder) or os.path.join(
            os.path.dirname(os.path.abspath(self.path)), "backups")
        os.makedirs(folder, exist_ok=True)
        stem = os.path.splitext(os.path.basename(self.path))[0]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        target = os.path.join(folder, f"{stem}-{stamp}.db")
        copy = sqlite3.connect(target)
        try:
            self.db.backup(copy)
        finally:
            copy.close()
        old = sorted(name for name in os.listdir(folder)
                     if re.fullmatch(re.escape(stem) + r"-\d{8}-\d{6}\.db", name))
        for name in old[:-keep] if keep > 0 else []:
            os.remove(os.path.join(folder, name))
        return target

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
        for key in ("band", "mode", "power"):
            if cab.get("category_" + key, "").strip():
                lines.append(f"CATEGORY-{key.upper()}: "
                             f"{cab['category_' + key].strip().upper()}")
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
# CW message keys F1 to F10, "label, text", for running and for search and
# pounce. Key numbers and meaning follow N1MM Logger.
FKEY_DEFAULTS = {
    "run": {1: "CQ, CQ TEST {MYCALL} {MYCALL} K", 2: "Exch, {RST} {EXCH}",
            3: "TU, TU {MYCALL}", 4: "MyCall, {MYCALL}", 5: "HisCall, {CALL}",
            6: "B4, {CALL} QSO B4", 7: "QRZ, {MYCALL} QRZ", 8: "Agn, AGN",
            9: "Nr?, NR?", 10: "Call?, CL?"},
    "sp": {1: "QRL?, QRL? DE {MYCALL}", 2: "Exch, TU {RST} {EXCH}",
           3: "TU, TU", 4: "MyCall, {MYCALL}", 5: "HisCall, {CALL}",
           6: "B4, QSO B4", 7: "", 8: "Agn, AGN", 9: "Nr?, NR?",
           10: "Call?, CL?"}}
FKEY_CONFIG = {f"{mode}_f{n}": text for mode, keys in FKEY_DEFAULTS.items()
               for n, text in keys.items()}


def message_keys(cfg):
    """{mode: {number: (label, text)}} from the run_f1 ... sp_f10 lines."""
    keys = {}
    for mode in ("run", "sp"):
        keys[mode] = {}
        for n in range(1, 11):
            value = cfg["contest"].get(f"{mode}_f{n}", "").strip()
            label, comma, text = value.partition(",")
            if not comma:                     # no label given: text only
                label, text = value[:8], value
            keys[mode][n] = (label.strip(), text.strip())
    return keys


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
    cfg.read_dict({"station": {"call": "", "email": "", "worked_before": "",
                               "country_file": "", "scp_file": ""},
                   "qrz": {"username": "", "password": ""},
                   "rig": {"host": "127.0.0.1", "port": "4532",
                           "vfo_switch": "retune", "tune_command": "auto",
                           "rigctld_radio": "", "rigctld_model": "",
                           "rigctld_device": "", "rigctld_speed": "",
                           "rigctld_options": "", "rigctld_autostart": "no",
                           "rigctld_command": "rigctld"},
                   "cluster": {"host": "", "port": "7300", "minutes": "30",
                               "call": ""},
                   "backup": {"minutes": "5", "keep": "50", "folder": ""},
                   "cw": {"enter_sends": "yes", "cq_pause": "3"},
                   "contest": {"contest": "", "exchange_sent": "",
                               "category_operator": "SINGLE-OP",
                               "email": "", "specific": "",
                               "dupe_check": "band",
                               "dupe_ignore_suffix": "no",
                               "dupe_exchange": "no",
                               "dupe_minutes": "0",
                               "points": "", "multipliers": "",
                               "mult_per": "band", "locator": "",
                               "serial_start": "1", "worked_before": "",
                               "mult_country": "no", "mult_calls": "",
                               "mult_prefix": "no", "mult_exclude": "",
                               **FKEY_CONFIG, "esm_empty": "f1",
                               "esm_work_dupes": "yes",
                               "esm_required": "exch", "esm_start": "run",
                               "mode": "", "category_band": "",
                               "category_mode": "", "category_power": ""}})
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
    if cfg["contest"].get("call", "").strip():   # e.g. OK/DA2IT for one contest
        cfg["station"]["call"] = cfg["contest"]["call"].strip().upper()
    cfg["station"]["config_dir"] = config_dir      # not saved; for cty.dat
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
# Alt-M: the main loop turns "Esc m" into this token (and other letters into
# ALT-<letter>); a few terminals send an 8-bit character for Alt-M
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
        self.forced_mode = cfg["contest"].get("mode", "").strip().upper()
        self.history = load_history(cfg)
        self.partial = PartialCheck(cfg)
        self._logged = (None, frozenset())   # (log state, callsigns in the log)
        self.entry = {v: self.new_entry() for v in "AB"}
        self.row = "A"
        self.manual_freq = {"A": 0, "B": 0}
        self.manual_mode = {"A": "SSB", "B": "SSB"}
        self.spot = ""        # callsign of the highlighted panel entry
        self.view = "newest"  # panel view: "newest" or "map" (band map)
        self.prompt = None    # question on the message line: text, value, action
        self.menus = []       # open menus, the last one is shown (see main_menu)
        self.cw_until = 0     # until when a CW message is probably still sent
        self.cq_auto = False  # F11: the CQ message is repeated
        self.cq_sent = (0, "")   # when the last repeated CQ started, and its text
        con = cfg["contest"]
        self.esm_mode = con.get("esm_start", "run").strip().lower()   # run / sp
        self.esm_required = con.get("esm_required", "exch").lower().replace(
            ",", " ").split()
        try:
            self.esm_work_dupes = con.getboolean("esm_work_dupes", True)
        except ValueError:
            sys.exit("Contest file: esm_work_dupes must be yes or no")
        self.fkeys = message_keys(cfg)
        empty = re.fullmatch(r"f(\d+)", con.get("esm_empty", "f1").strip().lower())
        if not empty or not 1 <= int(empty.group(1)) <= 10:
            sys.exit("Contest file: esm_empty must be a key from f1 to f10")
        self.esm_empty = int(empty.group(1))
        if self.esm_mode not in ("run", "sp") or set(self.esm_required) - {"exch", "loc"}:
            sys.exit("Contest file: esm_start must be run or sp, esm_required "
                     "a list of exch and loc (or empty)")
        self.rigctld = Rigctld(cfg)
        self.edit = None      # QSO being edited in the list, or None
        self.eval = {}        # its cell texts while editing
        self.efocus = 1       # edited cell (index into COLUMNS)
        self.msg = "Type a callsign. Enter = log QSO."
        try:
            self.backup_every = cfg["backup"].getint("minutes") * 60
            self.backup_keep = cfg["backup"].getint("keep")
        except ValueError:
            sys.exit("[backup] minutes and keep in config.ini must be numbers")
        self.backup_folder = cfg["backup"]["folder"].strip()
        self.backup_at = time.time()             # time of the last check
        self.backup_state = log.db.total_changes  # log state at the last backup

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
        if self.forced_mode:             # 'mode = RTTY' in the contest file
            return self.forced_mode
        if self.rig.online and self.rig.mode[row]:
            return RIG_MODES.get(self.rig.mode[row], self.rig.mode[row])
        return self.manual_mode[row]

    def default_rst(self, row=None):
        return "59" if self.mode(row) in ("SSB", "FM", "AM") else "599"

    def exch_sent(self, row=None):
        """The exchange to send in a row. A running number that has already
        gone out for the QSO in that row (ESM) is kept until it is logged,
        and the other row does not get the same number meanwhile."""
        template = self.cfg["contest"]["exchange_sent"]
        if "#" not in template:
            return template
        held = self.entry[row or self.row]["esm_exch"]
        if held:
            return held
        reserved = tuple(e["esm_exch"] for e in self.entry.values() if e["esm_exch"])
        state = (self.log.db.total_changes, reserved)
        if self._serial[0] != state:
            self._serial = (state, next_exchange(
                template, [q["exch_s"] for q in self.log.rows()] + list(reserved),
                self.cfg["contest"].getint("serial_start", 1)))
        return self._serial[1]

    @staticmethod
    def new_entry():
        # auto: field -> callsign for which it was filled in automatically
        # manual: field -> callsign for which it was edited by hand
        # esm_sent: callsign for which the exchange has gone out (ESM, Run)
        # esm_exch: the exchange that went out, kept until the QSO is logged
        # dupe_told: callsign that was told "QSO B4" (esm_work_dupes = no)
        return {"val": dict.fromkeys(FIELDS, ""), "focus": 0, "dupe_ok": "",
                "auto": {}, "manual": {}, "esm_sent": "", "esm_exch": "",
                "dupe_told": ""}

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
        """The new multipliers the station being entered would give, as text."""
        if self.edit or not is_call(self.val["call"]):
            return None
        keys = qso_mults(self.scoring, self.val["call"], self.val["exch"],
                         self.freq())
        new = [show_mult(key[1]) for key in keys
               if key not in self.score()["seen"]]
        return ", ".join(new) or None

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

    def message_text(self, template):
        """CW message with its placeholders filled in."""
        report = (self.val["rst_s"] or self.default_rst()).replace("9", "N")
        values = {"MYCALL": self.mycall, "CALL": self.val["call"],
                  "EXCH": self.exch_sent(), "RST": report}
        text = re.sub(r"\{(\w+)\}",
                      lambda m: values.get(m.group(1).upper(), ""), template)
        return " ".join(text.upper().split())

    def cw_ready(self):
        return self.rig.online and (self.rig.mode.get(self.row) or "") in ("CW", "CWR")

    VOICE_MODES = ("USB", "LSB", "AM", "FM")

    def voice_ready(self):
        return self.rig.online and (self.rig.mode.get(self.row) or "") in self.VOICE_MODES

    def voice_memory(self, key):
        """Voice memory of the radio behind a message key, 0 for none:
        F1 (CQ, Run only) is memory 1, F4 (my call) is memory 2."""
        if key == 1:
            return 1 if self.esm_mode == "run" else 0
        return 2 if key == 4 else 0

    def voice_next(self):
        """ESM in SSB: the message key Enter sends now (0 = none, Enter does
        what it does without ESM). Run: CQ on an empty call field. S&P: my
        call while a required field of the QSO is still empty."""
        call = self.val["call"]
        if not (self.enter_sends() and self.voice_ready()) or self.edit:
            return 0
        if not call:
            return 0 if (self.focus == 0 and self.spot) else self.voice_memory(1) and 1
        if (self.esm_mode == "sp" and is_call(call) and self.freq()
                and not (self.val["loc"] and not is_locator(self.val["loc"]))
                and any(not self.val[f].strip() for f in self.esm_required)):
            return 4
        return 0

    def send_voice(self, key):
        memory = self.voice_memory(key)
        label = self.fkeys[self.esm_mode][key][0] or ("CQ" if key == 1 else "MyCall")
        if not memory:
            self.msg = (f"F{key} has no voice message. In SSB: "
                        + ("F1 CQ and " if self.esm_mode == "run" else "")
                        + "F4 MyCall.")
        elif self.rig.voice_busy:
            self.msg = "A voice message is still running (Esc stops it)."
        else:
            self.rig.tune_info = ""
            self.rig.send_voice(memory)
            self.msg = f"Voice memory {memory} ({label}) ...  (Esc stops)"
            return True
        return False

    def keys_text(self, numbers, prefix=""):
        """Text of the given function keys in the current operating mode,
        joined to one transmission; keys without a message are left out."""
        parts = [prefix] + [self.message_text(self.fkeys[self.esm_mode][n][1])
                            for n in numbers]
        return " ".join(part for part in parts if part)

    def send_text(self, text):
        """Hands a CW text to the radio. Returns True if it went out."""
        raw_mode = self.rig.mode.get(self.row) or ""
        if not text:
            return False
        if not self.rig.online:
            self.msg = "Rig offline - cannot send CW."
        elif raw_mode not in ("CW", "CWR"):
            self.msg = f"Radio is in {raw_mode or 'an unknown mode'}, not CW - nothing sent."
        else:
            self.rig.tune_info = ""
            self.rig.send_cw(text)
            self.msg = f"CW: {text} ...  (Esc stops)"
            # rough length of the transmission at 12 WPM, for the Esc key
            self.cw_until = time.time() + len(text) * 1.2 + 2
            return True
        return False

    def fkey(self, number):
        """F1 to F10: send the message of that key for the current mode."""
        label, template = self.fkeys[self.esm_mode][number]
        call = self.val["call"]
        mode = "Run" if self.esm_mode == "run" else "S&P"
        if self.voice_ready():
            self.send_voice(number)
        elif not template:
            self.msg = f"F{number} has no message in {mode}."
        elif "{CALL}" in template.upper() and not is_call(call):
            self.msg = f"F{number} {label} needs a callsign in the call field."
        elif self.send_text(self.keys_text([number])):
            entry = self.entry[self.row]
            if "{EXCH}" in template.upper() and is_call(call):
                # the exchange has gone out: keep its number for this QSO
                entry["esm_exch"] = entry["esm_exch"] or self.exch_sent()
                if self.esm_mode == "run":
                    entry["esm_sent"] = call

    # ---- repeated CQ (F11)
    def cq_pause(self):
        """Seconds between the end of a CQ and the start of the next one."""
        try:
            return max(0.0, float(self.cfg["cw"].get("cq_pause", "3")))
        except ValueError:
            return 3.0

    def auto_cq_toggle(self):
        if self.cq_auto:
            self.cq_auto = False
            self.msg = "Auto CQ off."
        elif self.esm_mode != "run":
            self.msg = "Auto CQ works in Run only (Alt-M, then S)."
        elif not self.voice_ready() and not self.fkeys["run"][1][1]:
            self.msg = "Auto CQ sends the F1 message, and F1 has none."
        elif self.auto_cq_send():
            self.cq_auto = True
            self.msg = (f"Auto CQ on, pause {self.cq_pause():g} s: "
                        f"{self.cq_sent[1] or 'voice memory 1'}  (any key stops)")

    def auto_cq_send(self):
        if self.voice_ready():                   # SSB: voice memory 1
            if self.send_voice(1):
                self.cq_sent = (time.time(), "")
                return True
            return False
        text = self.keys_text([1])
        if self.send_text(text):
            self.cq_sent = (time.time(), text)
            return True
        return False

    def auto_cq_tick(self):
        """Called regularly: sends the next CQ when the pause is over."""
        if not self.cq_auto:
            return
        started, text = self.cq_sent
        due = started + cw_seconds(text, self.rig.wpm) + self.cq_pause()
        if not text:                 # voice: the radio tells when it is finished
            if self.rig.voice_busy:
                return
            if not self.rig.voice_ok:
                self.cq_auto = False
                self.msg += " Auto CQ off."
                return
            due = self.rig.voice_end + self.cq_pause()
        left = due - time.time()
        if self.esm_mode != "run":
            self.cq_auto = False
        elif 0 < left <= self.cq_pause():        # in the pause: count down
            self.msg = (f"Auto CQ: next CQ in {math.ceil(left)} s  "
                        "(any key stops)")
        elif time.time() >= due and not self.auto_cq_send():
            self.cq_auto = False
            self.msg += " Auto CQ off."

    def switch_row(self):
        self.row = "B" if self.row == "A" else "A"
        if self.rig.online:
            self.rig.select(self.row)
        self.msg = f"Row {self.row}"

    def enter(self):
        call = self.val["call"]
        esm = self.enter_sends() and self.cw_ready()
        if self.focus == 0 and not call and self.spot and self.spot_take():
            pass
        elif self.voice_next():
            # ESM in SSB: the radio's voice memory; in S&P on to the exchange
            if self.send_voice(self.voice_next()) and call:
                missing = [f for f in self.esm_required if not self.val[f].strip()]
                self.focus = FIELDS.index(missing[0])
        elif not call and esm:
            # ESM, empty call field: CQ when running, own call in S&P
            keys, prefix, _, _ = self.esm_next()
            self.send_text(self.keys_text(keys, prefix))
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
        elif esm:
            self.esm_enter(call)
        elif self.dupe() and self.dupe_ok != call:
            self.dupe_ok = call
            self.msg = self.dupe_text(self.dupe()) + " - Enter again logs it anyway."
        else:
            self.log_qso(call)

    # function keys that Enter sends for each ESM step, as in N1MM Logger
    ESM_KEYS = {"dupe_run": [6], "mycall": [4], "call": [5, 2], "again": [8]}

    def esm_step(self, call):
        """What Enter does next for the callsign in the active row (ESM, CW):

        Run:  callsign              -> 'call'   F5 + F2, his call + my exchange
              again, still nothing  -> 'again'  F8
              required fields there -> 'finish' F3, QSO logged
        S&P:  callsign              -> 'mycall' F4, again on each Enter
              required fields there -> 'finish' F2, QSO logged
        Dupes with esm_work_dupes = no: 'dupe_run' (F6) or 'dupe_sp' (no
        call) first; 'dupe_warn' is the usual question before logging.
        """
        entry = self.entry[self.row]
        run = self.esm_mode == "run"
        if (not self.esm_work_dupes and entry["dupe_told"] != call
                and not entry["esm_sent"] and self.dupe()):
            return "dupe_run" if run else "dupe_sp"
        missing = [f for f in self.esm_required if not self.val[f].strip()]
        if missing or (run and not entry["esm_sent"]):
            if not run:
                return "mycall"
            return "call" if entry["esm_sent"] != call else "again"
        if self.dupe() and self.dupe_ok != call:
            return "dupe_warn"
        return "finish"

    def esm_next(self):
        """What Enter would do now: (function keys it sends, text sent in
        front of them, whether it logs, short note for the key line)."""
        call = self.val["call"]
        run = self.esm_mode == "run"
        if not call:
            if self.focus == 0 and self.spot:
                return [], "", False, f"takes {self.spot}"
            return ([self.esm_empty] if run else [4]), "", False, ""
        if self.focus == 0 and not is_call(call) and (parse_freq(call)
                                                      or call in TYPED_MODES):
            return [], "", False, "QSY"
        if (not is_call(call) or not self.freq()
                or (self.val["loc"] and not is_locator(self.val["loc"]))):
            return [], "", False, "-"
        step = self.esm_step(call)
        if step == "dupe_sp":
            return [], "", False, "dupe, no call"
        if step == "dupe_warn":
            return [], "", False, "dupe warning"
        if step == "finish":
            # a callsign corrected after the exchange went out is repeated
            fixed = run and self.entry[self.row]["esm_sent"] != call
            return ([3] if run else [2]), (call if fixed else ""), True, ""
        return self.ESM_KEYS[step], "", False, ""

    def esm_enter(self, call):
        """ESM in CW: Enter sends the next message of the QSO and logs it
        once the required fields are filled."""
        entry = self.entry[self.row]
        step = self.esm_step(call)
        keys, prefix, logs, _ = self.esm_next()
        if step in ("dupe_run", "dupe_sp"):
            # worked before: say so instead of starting the QSO. A further
            # Enter works the station anyway, without asking again.
            entry["dupe_told"] = self.dupe_ok = call
            text = self.dupe_text(self.dupe())
            if step == "dupe_run":
                self.send_text(self.keys_text(keys))
                self.msg = text + " - told him. Enter again works him anyway."
            else:
                self.msg = text + " - not calling. Enter again calls anyway."
            return
        if step == "dupe_warn":
            self.dupe_ok = call
            self.msg = self.dupe_text(self.dupe()) + " - Enter again logs it anyway."
            return
        if not entry["esm_exch"]:
            # the exchange (running number) stays the same for this QSO
            entry["esm_exch"] = self.exch_sent()
        self.send_text(self.keys_text(keys, prefix))
        if logs:
            sent = self.msg
            self.log_qso(call)
            if sent.startswith("CW:"):
                self.msg += "  -  " + sent
        else:
            if step == "call":
                entry["esm_sent"] = call
            missing = [f for f in self.esm_required if not self.val[f].strip()]
            self.focus = FIELDS.index((missing or ["exch"])[0])

    def key_line(self):
        """The message key line: [(text, highlighted)] segments and a note at
        the end. Keys that Enter would send now (ESM) are highlighted."""
        keys, note = [], ""
        if self.enter_sends() and not self.edit:
            if self.cw_ready():
                keys, prefix, logs, note = self.esm_next()
                names = "+".join(f"F{n}" for n in keys
                                 if self.fkeys[self.esm_mode][n][1])
                note = "Enter: " + (" + ".join(
                    x for x in (names, "log" if logs else "", note) if x) or "-")
            elif not self.rig.online:
                note = "Rig offline: no messages, Enter logs"
            else:
                raw = self.rig.mode.get(self.row) or "this mode"
                note = f"No messages in {raw}, Enter logs"
        mode = "RUN" if self.esm_mode == "run" else "S&P"
        segments = [(f" {mode} ", False)]
        voice, cw = self.voice_ready(), self.cw_ready()
        if not (voice or cw) and not note:       # ESM off: still say why
            note = ("Rig offline: no messages" if not self.rig.online else
                    f"No messages in {self.rig.mode.get(self.row) or 'this mode'}")
        if voice:
            keys = [self.voice_next()]
            note = "Voice memories of the radio" + (
                f", Enter: F{keys[0]}" if keys[0] else
                ", Enter logs" if self.enter_sends() else "")
        for number in range(1, 11):
            label, template = self.fkeys[self.esm_mode][number]
            if voice and self.voice_memory(number):
                segments.append((f" F{number} {label or ('CQ' if number == 1 else 'MyCall')} ",
                                 number in keys))
            elif template and cw:
                segments.append((f" F{number} {label} ", number in keys))
        if self.esm_mode == "run" and (cw or voice):
            segments.append((f" F11 Auto CQ {self.cq_pause():g}s ", self.cq_auto))
        segments.append((" F12 Wipe ", False))
        return segments, note

    def log_qso(self, call):
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
                    "saves, Esc cancels, Up/Down other QSO, Alt-D deletes it.")

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
    def auto_backup(self, force=False):
        """Copies the log file every few minutes if it has changed since the
        last copy; force=True (on quit) skips the waiting time."""
        if not self.backup_every:
            return None
        if not force and time.time() - self.backup_at < self.backup_every:
            return None
        self.backup_at = time.time()
        if self.log.db.total_changes == self.backup_state:
            return None
        try:
            target = self.log.backup(self.backup_folder, self.backup_keep)
        except (OSError, sqlite3.Error) as exc:
            self.msg = f"BACKUP FAILED: {exc}"
            return None
        self.backup_state = self.log.db.total_changes
        return target

    def manual_backup(self):
        """Menu: the same kind of copy as the periodic backup, right now."""
        try:
            target = self.log.backup(self.backup_folder, self.backup_keep)
        except (OSError, sqlite3.Error) as exc:
            self.msg = f"BACKUP FAILED: {exc}"
            return
        self.backup_at = time.time()
        self.backup_state = self.log.db.total_changes
        self.msg = f"Backup written: {target}"

    # ---- menu (Alt-M)
    # a menu is a dict: title, items [(hotkey, text, action)], index, info lines
    def main_menu(self):
        enter = "on" if self.enter_sends() else "off"
        return {"title": "Menu", "index": 0, "info": [], "items": [
            ("a", "Export ADIF", "adif"), ("c", "Export Cabrillo", "cabrillo"),
            ("b", "Backup now", "backup"), ("r", "Radio / rigctld ...", "radio"),
            ("e", f"ESM, Enter sends message: {enter} (change)", "enter_sends"),
            ("s", "Operating: " + ("Run, calling CQ" if self.esm_mode == "run"
                                   else "Search & pounce") + " (change)", "esm_mode"),
            ("p", f"Auto CQ (F11) pause: {self.cq_pause():g} s (change)",
             "cq_pause"),
            ("t", "ATU tune (transmits)", "atu"), ("q", "Quit", "quit")]}

    def radio_menu(self):
        auto = self.cfg["rig"].get("rigctld_autostart", "no").strip().lower()
        auto = "on" if auto in ("yes", "true", "on", "1") else "off"
        return {"title": "Radio", "index": 0,
                "info": ["Radio:   " + self.rigctld.describe(),
                         "rigctld: " + self.rigctld.state()],
                "items": [("d", "Detect radio and save settings", "detect"),
                          ("s", "Start rigctld", "rig_start"),
                          ("x", "Stop rigctld", "rig_stop"),
                          ("o", f"Start with llog: {auto} (change)", "rig_auto")]}

    def enter_sends(self):
        """Whether ESM is on: Enter sends the CW messages of the QSO."""
        return self.cfg["cw"].get("enter_sends", "yes").strip().lower() in (
            "yes", "true", "on", "1")

    def save_setting(self, section, **values):
        """Stores settings in config.ini and in the running config."""
        path = os.path.join(self.cfg["station"].get("config_dir",
                                                    DEFAULT_CONFIG_DIR), "config.ini")
        self.cfg[section].update(values)
        try:
            set_ini_values(path, section, values)
            return ""
        except OSError as exc:
            return f" (not saved to config.ini: {exc.strerror or exc})"

    def atu_tune(self):
        if self.rig.online:
            self.msg = "ATU tune ..."
            self.rig.tune_info = ""
            self.rig.tune(self.cfg["rig"].get("tune_command", "auto").strip()
                          or "auto")
        else:
            self.msg = "Rig offline - cannot start ATU tune."

    def save_rig(self, **values):
        return self.save_setting("rig", **values)

    def use_radio(self, radio):
        number = self.rigctld.model_number(radio["radio"])
        problem = self.save_rig(rigctld_radio=radio["radio"], rigctld_model=number,
                                rigctld_device=radio["device"],
                                rigctld_speed=radio["speed"])
        self.msg = (f"Radio set: {self.rigctld.describe()}{problem}. "
                    "Start rigctld from the Radio menu."
                    + ("" if number else " Hamlib model number not found - "
                       "is Hamlib installed?"))

    def detect(self, scr):
        if self.rigctld.reachable():
            self.msg = "Stop rigctld first: detection needs the serial port."
            return
        self.msg = "Detecting radios ..."
        if scr:
            self.draw(scr)
        found, notes = detect_radios()
        if len(found) == 1:
            self.use_radio(found[0])
        elif found:
            self.menus = [{"title": "Which radio?", "index": 0, "info": [], "items": [
                (str(i), f"{r['radio']} on {os.path.basename(r['device'])[:40]}",
                 ("pick", r)) for i, r in enumerate(found, 1)]}]
            self.msg = "Several radios found - choose one."
        else:
            hint = notes[0] if notes else "no radio answered"
            if "ermission" in hint:
                hint += " - add your user to the group 'dialout' and log in again"
            self.msg = ("No radio found: " + hint + ". Is it switched on and "
                        "connected? (KX2: menu RS232 not OFF)")

    def autostart(self, scr=None):
        """Start of llog with 'Start with llog: on': start rigctld with the
        saved settings; only if the saved radio is not there, look for one."""
        self.msg = self.rigctld.start()
        if not self.rigctld.radio_missing:
            return
        first = self.msg
        self.msg = "Saved radio not found - looking for a radio ..."
        if scr:
            self.draw(scr)
        found, notes = detect_radios()
        if len(found) == 1:
            self.use_radio(found[0])
            self.msg = self.rigctld.start()
        elif found:
            self.menus = [{"title": "Which radio?", "index": 0, "info": [], "items": [
                (str(i), f"{r['radio']} on {os.path.basename(r['device'])[:40]}",
                 ("pick_start", r)) for i, r in enumerate(found, 1)]}]
            self.msg = "Several radios found - choose one."
        else:
            self.msg = first + " No radio found by scanning either."

    def menu_action(self, scr, action):
        """Returns False to quit."""
        if isinstance(action, tuple):            # ("pick", radio)
            self.use_radio(action[1])
            if action[0] == "pick_start":
                self.msg = self.rigctld.start()
        elif action == "quit":
            return False
        elif action == "radio":
            self.menus = [self.main_menu(), self.radio_menu()]
            self.menus[0]["index"] = 3           # Esc returns to "Radio"
        elif action == "atu":
            self.atu_tune()
        elif action == "enter_sends":
            new = "no" if self.enter_sends() else "yes"
            problem = self.save_setting("cw", enter_sends=new)
            self.msg = ("ESM on: in CW, Enter sends the messages"
                        if new == "yes" else
                        "ESM off: Enter only logs") + f"{problem}."
        elif action == "backup":
            self.manual_backup()
        elif action == "cq_pause":
            def save(value):
                try:
                    seconds = float(value.strip().replace(",", "."))
                    if not 0 <= seconds <= 600:
                        raise ValueError
                except ValueError:
                    self.msg = "Pause not changed: type seconds from 0 to 600."
                    return
                problem = self.save_setting("cw", cq_pause=f"{seconds:g}")
                self.msg = f"Auto CQ pause is now {seconds:g} s{problem}."
            self.prompt = {"text": f"Auto CQ pause in seconds (now "
                                   f"{self.cq_pause():g}; Enter saves, Esc "
                                   "cancels): ",
                           "value": "", "action": save}
        elif action == "esm_mode":
            self.esm_mode = "sp" if self.esm_mode == "run" else "run"
            self.msg = ("Run: Enter answers callers with their call and your "
                        "exchange." if self.esm_mode == "run" else
                        "Search & pounce: Enter sends your call, then your "
                        "exchange.")
        elif action == "detect":
            self.detect(scr)
        elif action == "rig_start":
            self.msg = "Starting rigctld ..."
            if scr:
                self.draw(scr)
            self.msg = self.rigctld.start()
        elif action == "rig_stop":
            self.msg = self.rigctld.stop()
        elif action == "rig_auto":
            now = self.cfg["rig"].get("rigctld_autostart", "no").strip().lower()
            new = "no" if now in ("yes", "true", "on", "1") else "yes"
            problem = self.save_rig(rigctld_autostart=new)
            self.msg = (f"rigctld is {'now' if new == 'yes' else 'no longer'} "
                        f"started together with llog{problem}.")
        else:
            self.export(action)
        return True

    def menu_key(self, ch, scr=None):
        """Returns False to quit."""
        menu = self.menus[-1]
        hotkeys = [key for key, _, _ in menu["items"]]
        if ch == curses.KEY_UP:
            menu["index"] = (menu["index"] - 1) % len(hotkeys)
        elif ch == curses.KEY_DOWN:
            menu["index"] = (menu["index"] + 1) % len(hotkeys)
        elif ch in ("\n", "\r", curses.KEY_ENTER) or (
                isinstance(ch, str) and ch.lower() in hotkeys):
            if isinstance(ch, str) and ch.lower() in hotkeys:
                menu["index"] = hotkeys.index(ch.lower())
            action = menu["items"][menu["index"]][2]
            self.menus = []
            return self.menu_action(scr, action)
        elif ch == "\x1b" or ch == curses.KEY_LEFT:
            self.menus.pop()                     # back to the parent menu
            if not self.menus:
                self.msg = "Menu closed."
        elif ch in ALT_M:
            self.menus, self.msg = [], "Menu closed."
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
            if self.rig.voice_busy:              # a voice memory is playing
                self.rig.stop_voice()
            elif time.time() < self.cw_until:    # a CW message may still be running
                self.rig.stop_cw()
                self.cw_until = 0
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
        if self.cq_auto and ch != curses.KEY_F11:
            self.cq_auto = False         # any key ends the repeated CQ
            self.msg = "Auto CQ off."
            if ch == "\x1b" and not self.prompt and not self.menus:
                self.cw_until = time.time() + 1    # Esc also cuts a running CQ
        if self.prompt:
            self.prompt_key(ch)
            return True
        if self.menus:
            return self.menu_key(ch, scr)
        if ch == "\x18":            # Ctrl-X: quit without the menu, as a fallback
            return False
        if ch in ALT_M:
            self.menus = [self.main_menu()]
            self.msg = "Menu: Up/Down, Enter selects, Esc goes back."
        elif isinstance(ch, int) and curses.KEY_F1 <= ch <= curses.KEY_F10:
            self.fkey(ch - curses.KEY_F0)
        elif ch == curses.KEY_F11:
            self.auto_cq_toggle()
        elif ch == curses.KEY_F12:
            self.clear()
            self.spot = ""
            self.msg = "Cleared."
        elif ch == "ALT-S":
            self.spot_send()
        elif ch == "ALT-B":
            self.toggle_view()
        elif ch == "ALT-L":
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
        elif ch == "ALT-D":
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
        if not self.scoring["has_mults"]:
            return []
        names = [m for m in self.scoring["mults"]
                 if not any(c in m for c in "*?[")]
        per_band = self.scoring["mult_per"] == "band"
        band = band_of(self.freq() or 0) if per_band else ""
        # without a frequency the band is unknown: show all bands together
        any_band = per_band and not band
        worked = {show_mult(value) for b, value in self.score()["seen"]
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
        """Alt-L / Ctrl-J: note the station in the call field on the current
        frequency, to come back to it later."""
        call = self.val["call"]
        if not is_call(call):
            self.msg = "Type the callsign to store first."
        elif not band_of(self.freq() or 0):
            self.msg = "No amateur band frequency - cannot store."
        else:
            self.log.store_later(call, self.freq(), self.mode(),
                                 self.val["exch"].strip(), self.val["loc"])
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
        """Alt-S: spot the station in the call field, or else the last QSO."""
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
                if spot["kind"] == "stored":
                    # bring back what was already typed; fields restored
                    # here count as typed by hand, so QRZ does not replace them
                    entry = self.entry[self.row]
                    for name, value in (("exch", spot["exch"]), ("loc", spot["loc"])):
                        if value:
                            self.val[name] = value
                            entry["manual"][name] = spot["call"]
                self.qrz.request(spot["call"])
                self.msg = (f"Row {self.row}: {spot['call']} on "
                            f"{fmt_freq(spot['freq_hz'])}")
                self.spot = ""
                return True
        return False

    def partial_matches(self):
        """Check partial for the call field of the active row: callsigns from
        the SCP file, this log and the worked-before file."""
        state = self.log.db.total_changes
        if self._logged[0] != state:
            self._logged = (state, frozenset(q["call"] for q in self.log.rows())
                            | frozenset(self.history.calls))
        return self.partial.match(self.val["call"], self._logged[1])

    def score_text(self):
        if not self.scoring["configured"]:
            return ""
        r = self.score()
        if self.scoring["has_mults"]:
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

        rig = (self.rig.model or "RIG ok") if self.rig.online \
            else "RIG offline (manual)"
        utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + "Z"
        head = (f" {self.mycall} {self.cfg['contest']['contest']} | "
                + ("RUN | " if self.esm_mode == "run" else "S&P | ") +
                f"Row {self.row} {band_of(self.freq() or 0) or '-'} "
                f"{self.mode()} | {rig} | {self.score_text()}"
                f"QSOs: {self.log.count()} | {utc} ")
        put(0, 0, head.ljust(w - 1), curses.A_REVERSE)

        # the panel on the right (cluster / band map) takes the space right
        # of the list columns, up to 60 columns, so that spot comments fit;
        # what is left beyond that goes to the names. It runs from the top
        # down to the message line.
        panel_w = min(w - 112, 60)
        panel_x = w - panel_w if w >= 142 else None
        left_w = panel_x - 2 if panel_x else w - 1    # width left of the panel

        # QSO list, with the edited QSO shown as editable cells
        extra = 1 if self.history.path else 0     # line for "worked before"
        mult_rows = self.mult_panel(left_w - 1)
        if h - 13 - extra - len(mult_rows) < 3:   # terminal too small for it
            mult_rows = []
        space = max(h - 12 - extra - (len(mult_rows) + 1 if mult_rows else 0), 1)
        for n, row in enumerate(mult_rows):
            x = 1
            for text, worked in row:
                put(h - 9 - extra - len(mult_rows) + n, x, text,
                    self.good_attr if worked else 0)
                x += len(text) + 1
        head = (f"{'#':>4} " + " ".join(f"{title:<{width}}"
                                         for _, width, title in COLUMNS)
                + f" {'km':>5} {'Pts':<6}")
        if not panel_x or panel_x - 3 >= len(head) + 4:
            head += "Name"
        put(2, 1, head, curses.A_BOLD)
        if panel_x:
            band = band_of(self.freq() or 0) or "?"
            # rows start with a one-character flag column, hence the +1
            put(2, panel_x + 1, f"Band map {band} (Alt-B: newest first)"
                if self.view == "map" else f"Newest {band} (Alt-B: band map)",
                curses.A_BOLD)
            spots = self.panel_rows()
            line = 3
            if self.cluster.host and self.cluster.status != "connected":
                put(line, panel_x + 1,
                    f"cluster {self.cluster.status}"[:panel_w - 3])
                line += 1
            elif not spots:
                put(line, panel_x + 1, "nothing on this band yet")
            room = max(h - 3 - line, 1)             # down to the message line
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
            points, is_dupe, new_mults = per_qso.get(q["id"], (0, False, 0))
            pts = "DUPE" if is_dupe else str(points) + (
                "" if not new_mults else "+M" if new_mults == 1
                else f"+{new_mults}M")
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
                put(h - 9, 1, self.history.text(known)[:left_w - 1], curses.A_BOLD)
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
        deg = bearing_deg(self.my_loc, loc)
        land = (self.scoring["countries"].lookup(call)
                if self.scoring["countries"] and is_call(call) else None)
        if km is not None:
            line += f"   [{loc}: {km} km" + (
                "]" if deg is None else f", {paths(deg)}]")
        elif (land and land["lat"] is not None and is_locator(self.my_loc)
              and not is_locator(loc)):
            # no locator: rough direction to the middle of the country
            far, deg = great_circle(*locator_position(self.my_loc),
                                    land["lat"], land["lon"])
            if far > 300:
                line += f"   [about {far} km, {paths(deg)}]"
        if land:
            line += (f"   [{land['prefix']} {land['name']}, {land['cont']}, "
                     f"CQ zone {land['cq']}]")
        put(h - 8, 1, line[:left_w - 1], curses.A_BOLD)
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
            put(line, x, self.exch_sent(vfo) or "-")

        scp_x = x + 13

        # check partial: between the entry fields and the panel
        scp_w = left_w - scp_x
        if scp_w >= 14 and not self.edit:
            matches = self.partial_matches()
            typed = self.val["call"]
            title = (f"Check partial ({len(matches)})" if matches
                     else "Check partial" if len(typed) < 3
                     else "Check partial: no match")
            if not self.partial.path and len(typed) < 3:
                title = "Check partial (no MASTER.SCP)"
            put(h - 6, scp_x, title[:scp_w], curses.A_BOLD)
            row, col = h - 5, scp_x
            for call in matches:
                if col + len(call) > scp_x + scp_w:
                    row, col = row + 1, scp_x
                    if row > h - 4:
                        break
                put(row, col, call, curses.A_BOLD | curses.A_UNDERLINE
                    if call == typed else 0)
                col += len(call) + 1

        if self.prompt:
            line = self.prompt["text"] + self.prompt["value"]
            put(h - 3, 1, line, curses.A_BOLD)
            cursor = (h - 3, min(1 + len(line), w - 2))
        else:
            put(h - 3, 1, self.msg)
        # message keys: F1 to F10 for the current operating mode; the keys
        # Enter would send now (ESM) stand out, with a note at the right
        segments, note = self.key_line()
        put(h - 2, 0, " " * (w - 1), curses.A_REVERSE)
        x = 0
        for text, lit in segments:
            put(h - 2, x, text, curses.A_BOLD if lit else curses.A_REVERSE)
            x += len(text)
        if note and x + len(note) + 3 < w:
            put(h - 2, w - len(note) - 2, note, curses.A_REVERSE | curses.A_BOLD)
        put(h - 1, 0, " Enter log | Tab next | Esc clear | \\ row A/B | "
                      "<kHz>/<mode> Enter QSY | Up edit | PgUp/PgDn panel | "
                      "Alt-S spot | Alt-B band map | Alt-L store | Alt-D delete | "
                      "Alt-M menu ".ljust(w - 1), curses.A_REVERSE)
        if self.menus:                   # menu box in the middle of the screen
            menu = self.menus[-1]
            rows = ([(text, None) for text in menu["info"]]
                    + ([("", None)] if menu["info"] else [])
                    + [(f"{hot.upper()}  {title}", i)
                       for i, (hot, title, _) in enumerate(menu["items"])])
            width = min(max(len(text) for text, _ in rows) + 6, w - 2)
            top, left = max((h - len(rows) - 2) // 2, 0), max((w - width) // 2, 0)
            put(top, left, "+" + f" {menu['title']} ".center(width - 2, "-") + "+")
            for n, (text, index) in enumerate(rows):
                put(top + 1 + n, left, "|" + " " * (width - 2) + "|")
                put(top + 1 + n, left + 2, text[:width - 4].ljust(width - 4),
                    curses.A_REVERSE if index == menu["index"] else 0)
                if index == menu["index"]:
                    cursor = (top + 1 + n, left + 2)
            put(top + 1 + len(rows), left, "+" + "-" * (width - 2) + "+")
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
        if self.cfg["rig"].get("rigctld_autostart", "no").strip().lower() in (
                "yes", "true", "on", "1"):
            self.autostart(scr)
        self.rig.start()
        self.cluster.start()
        while True:
            if self.rig.online and self.rig.vfo != self.row:
                self.row = self.rig.vfo      # A/B was pressed on the radio
            if self.rig.tune_info:
                self.msg, self.rig.tune_info = self.rig.tune_info, ""
            self.sync_prefill()
            self.auto_backup()
            self.auto_cq_tick()
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
            if ch == "\x1b":             # Alt-<letter> arrives as Esc + the letter
                scr.timeout(30)
                try:
                    follow = scr.get_wch()
                    keys = [f"ALT-{follow.upper()}"] if (
                        isinstance(follow, str) and follow.isalpha()
                        and len(follow) == 1) else [ch, follow]
                except curses.error:
                    pass                 # a plain Esc
                scr.timeout(200)
            for one in keys:
                if not self.key(scr, one):
                    return


def main():
    ap = argparse.ArgumentParser(description="Contest logger for the terminal")
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
    app = App(cfg, log)
    try:
        curses.wrapper(app.run)
    finally:
        if app.rigctld.ours():           # a rigctld we started ends with us
            print(app.rigctld.stop())
    saved = app.auto_backup(force=True)
    if saved:
        print(f"Backup: {saved}")
    print(f"{log.count()} QSOs in {args.logfile}")
    if scoring["configured"]:
        print(score_report(compute_score(log.rows(), dupes, scoring), scoring))


if __name__ == "__main__":
    main()
