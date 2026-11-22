#!/usr/bin/env python3
"""llog - a very simple terminal contest logger.

Needs only Python 3.9+ (standard library). Radio control goes through
Hamlib's rigctld, callsign lookup through the QRZ.com XML API.

    rigctld -m <model> -r /dev/ttyUSB0 -s 38400 &
    python3 llog.py mylog.db
"""
import argparse
import configparser
import curses
import getpass
import os
import queue
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
AGENT = "llog-0.2"

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


def ascii_only(text):
    return unicodedata.normalize("NFKD", text or "").encode(
        "ascii", "ignore").decode()


# --------------------------------------------------------------------- rig
class Rig(threading.Thread):
    """Polls rigctld for frequency/mode and sends set commands."""

    def __init__(self, host, port):
        super().__init__(daemon=True)
        self.addr = (host, port)
        self.freq = None
        self.mode = None      # raw Hamlib mode, e.g. USB
        self.online = False
        self.error = ""
        self._todo = queue.Queue()
        self._sock = None
        self._file = None

    def set_freq(self, hz):
        self._todo.put(f"F {hz}")

    def set_mode(self, hamlib_mode):
        self._todo.put(f"M {hamlib_mode} 0")

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

    def run(self):
        while True:
            try:
                if self._sock is None:
                    self._sock = socket.create_connection(self.addr, timeout=2)
                    self._file = self._sock.makefile("r")
                while not self._todo.empty():
                    self._ask(self._todo.get())
                freq = self._ask("f")
                mode = self._ask("m", extra_lines=1)
                if freq:
                    self.freq = int(float(freq))
                if mode:
                    self.mode = mode
                self.online = bool(freq)
                self.error = "" if freq else "rig not answering"
                time.sleep(0.3)
            except (OSError, ValueError) as exc:
                self.online = False
                self.error = str(exc)
                if self._sock:
                    self._sock.close()
                self._sock = self._file = None
                time.sleep(2)


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
        self.db.commit()

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
        sql = "SELECT id," + ",".join(self.COLS) + " FROM qso ORDER BY id"
        rows = [dict(zip(("id",) + self.COLS, r)) for r in self.db.execute(sql)]
        return rows[-last:] if last else rows

    def delete_last(self):
        self.db.execute("DELETE FROM qso WHERE id=(SELECT MAX(id) FROM qso)")
        self.db.commit()

    def find_dupe(self, rules, call, freq_hz, mode, exch="", now=None):
        """Returns the earlier QSO that makes this one a dupe, or None."""
        if rules["check"] == "off":
            return None
        now = now or datetime.now(timezone.utc)
        found = self.db.execute(
            "SELECT ts, call, freq_hz, mode, exch_r FROM qso "
            "WHERE call LIKE ? ORDER BY id DESC", (f"%{base_call(call)}%",))
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
    def export_adif(self, path):
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
                    + field("COUNTRY", q["country"]) + "<EOR>\n")
        return path

    def export_cabrillo(self, path, cfg):
        cab = cfg["contest"]
        mycall = cfg["station"]["call"]
        lines = ["START-OF-LOG: 3.0", f"CONTEST: {cab.get('contest', '')}",
                 f"CALLSIGN: {mycall}",
                 f"CATEGORY-OPERATOR: {cab.get('category_operator', 'SINGLE-OP')}"]
        if cab.get("email"):
            lines.append(f"EMAIL: {cab['email']}")
        if cab.get("specific"):
            lines.append(f"SPECIFIC: {cab['specific']}")
        lines.append("CREATED-BY: llog 0.2")
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

    def export_stf(self, path, cfg):
        """STF1 (Stuetzerbach format). Empty fields are written as '-'."""
        con = cfg["contest"]
        now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M")
        rows = self.rows()
        lines = ["STF1", f'#Data written "{now}"',
                 "#STF-Export generated with llog 0.2", "Header",
                 f"Contest      {con.get('contest', '')}",
                 f"MyCall       {cfg['station']['call']}",
                 f"Category     {con.get('category_operator', 'SINGLE-OP')}"]
        if con.get("email"):
            lines.append(f"EMail        {con['email']}")
        if con.get("specific"):
            lines.append(f"Specific     {con['specific']}")
        lines += [f"ClaimedQSO   {len(rows)}",
                  "QsoOrder     Date Time Band Mode Call SRst Sent RRst Rcvd",
                  "EndHeader", "QsoList"]
        for q in rows:
            ts = q["ts"]
            band = band_of(q["freq_hz"] or 0).rstrip("cm") or "-"
            lines.append("{} {} {:>4} {:<4} {:<13} {:<3} {:<6} {:<3} {}".format(
                ts[0:4] + ts[5:7] + ts[8:10], ts[11:13] + ts[14:16], band,
                q["mode"], q["call"], q["rst_s"] or "-", q["exch_s"] or "-",
                q["rst_r"] or "-", q["exch_r"].replace(" ", "_") or "-"))
        lines.append("EndQSOList")
        with open(path, "w", encoding="ascii", errors="replace",
                  newline="\n") as out:
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
    cfg.read_dict({"station": {"call": "", "email": ""},
                   "qrz": {"username": "", "password": ""},
                   "rig": {"host": "127.0.0.1", "port": "4532"},
                   "contest": {"contest": "", "exchange_sent": "",
                               "category_operator": "SINGLE-OP",
                               "email": "", "specific": "",
                               "dupe_check": "band",
                               "dupe_ignore_suffix": "no",
                               "dupe_exchange": "no",
                               "dupe_minutes": "0"}})
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
        write_ini(station_file, cfg, ("station", "qrz", "rig"))

    contest_file = contest_path(config_dir, contest)
    if os.path.exists(contest_file):
        cfg.read(contest_file)
    else:
        con = cfg["contest"]
        print(f"New contest - creating {contest_file}\n")
        name = os.path.splitext(os.path.basename(contest))[0].upper()
        con["contest"] = ask("Contest name for the log header", name)
        con["exchange_sent"] = ask(
            "Exchange you send (e.g. your DOK, '#' = serial number, '-' = none)",
            con["exchange_sent"] or cfg["station"].get("exchange_sent", "")
        ).upper()
        con["category_operator"] = ask("Operator category",
                                       con["category_operator"])
        con["specific"] = ask(
            "SPECIFIC header (own DOK for DARC contests, '-' = none)",
            con["exchange_sent"] if con["exchange_sent"] != "#" else ""
        ).upper()
        con["dupe_check"] = ask(
            "Dupe check: band / band_mode / mode / once / off",
            con["dupe_check"]).lower()
        write_ini(contest_file, cfg, ("contest",))
        print("More dupe options (dupe_ignore_suffix, dupe_exchange, "
              "dupe_minutes) can be edited in that file.\n")
    if not cfg["contest"]["email"]:
        cfg["contest"]["email"] = cfg["station"].get("email", "")
    return cfg


# ---------------------------------------------------------------------- ui
FIELDS = ("call", "rst_s", "rst_r", "exch")
LABELS = {"call": "Call", "rst_s": "Sent", "rst_r": "Rcvd", "exch": "Exchange rcvd"}
WIDTHS = {"call": 14, "rst_s": 5, "rst_r": 5, "exch": 20}


class App:
    def __init__(self, cfg, log):
        self.cfg, self.log = cfg, log
        self.mycall = cfg["station"]["call"]
        self.rules = dupe_rules(cfg)
        self.dupe_ok = ""     # callsign whose dupe warning was confirmed
        self.rig = Rig(cfg["rig"]["host"], cfg["rig"].getint("port"))
        self.qrz = Qrz(os.environ.get("QRZ_USERNAME", cfg["qrz"]["username"]),
                       os.environ.get("QRZ_PASSWORD", cfg["qrz"]["password"]))
        self.manual_freq = 0
        self.manual_mode = "SSB"
        self.val = dict.fromkeys(FIELDS, "")
        self.focus = 0
        self.msg = "Type a callsign. Enter = log QSO."

    # ---- state helpers
    def freq(self):
        return self.rig.freq if self.rig.online and self.rig.freq else self.manual_freq

    def mode(self):
        if self.rig.online and self.rig.mode:
            return RIG_MODES.get(self.rig.mode, self.rig.mode)
        return self.manual_mode

    def default_rst(self):
        return "59" if self.mode() in ("SSB", "FM", "AM") else "599"

    def exch_sent(self):
        sent = self.cfg["contest"]["exchange_sent"]
        return f"{self.log.count() + 1:03d}" if sent == "#" else sent

    def clear(self):
        self.val = dict.fromkeys(FIELDS, "")
        self.focus = 0
        self.dupe_ok = ""

    def dupe(self):
        call = self.val["call"]
        if not is_call(call):
            return None
        return self.log.find_dupe(self.rules, call, self.freq(), self.mode(),
                                  self.val["exch"].strip())

    @staticmethod
    def dupe_text(q):
        return (f"DUPE: {q['call']} worked {q['ts'][11:16]}Z on "
                f"{band_of(q['freq_hz'] or 0) or '?'} {q['mode']}"
                + (f", exchange {q['exch_r']}" if q["exch_r"] else ""))

    # ---- actions
    def set_freq(self, hz):
        self.manual_freq = hz
        if self.rig.online:
            self.rig.set_freq(hz)
            self.msg = f"Rig set to {fmt_freq(hz)}"
        else:
            self.msg = f"Rig offline - frequency set manually to {fmt_freq(hz)}"

    def set_mode(self, mode):
        self.manual_mode = mode
        if self.rig.online:
            hamlib = mode
            if mode == "SSB":
                hamlib = "LSB" if self.freq() < 10_000_000 else "USB"
            self.rig.set_mode(hamlib)
        self.msg = f"Mode {mode}"

    def enter(self):
        call = self.val["call"]
        if self.focus == 0 and parse_freq(call) and not is_call(call):
            self.set_freq(parse_freq(call))
            self.val["call"] = ""
        elif self.focus == 0 and call in TYPED_MODES:
            self.set_mode(call)
            self.val["call"] = ""
        elif not is_call(call):
            self.msg = "Not a valid callsign."
        elif not self.freq():
            self.msg = "No frequency: start rigctld or type one (kHz) + Enter."
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
                grid=info.get("grid", ""), country=info.get("country", ""))
            self.msg = f"Logged {call}"
            self.clear()

    def leave_call_field(self):
        if is_call(self.val["call"]):
            self.qrz.request(self.val["call"])

    def export(self, kind):
        stem = os.path.splitext(self.log.path)[0]
        try:
            if kind == "adif":
                path = self.log.export_adif(stem + ".adi")
            elif kind == "stf":
                rows = self.log.rows()
                ts = rows[0]["ts"] if rows else "0000-00"
                contest = self.cfg["contest"].get("contest") or "LOG"
                name = (f"{self.mycall.replace('/', '-')}_{ts[5:7]}_{ts[0:4]}_"
                        f"{contest.replace(' ', '-')}.stf")
                path = self.log.export_stf(
                    os.path.join(os.path.dirname(self.log.path), name), self.cfg)
            else:
                rows = self.log.rows()
                day = rows[0]["ts"][:10] if rows else "empty"
                name = f"{self.mycall.replace('/', '-')}-{day}-1.cbr"
                path = self.log.export_cabrillo(
                    os.path.join(os.path.dirname(self.log.path), name), self.cfg)
            self.msg = f"Exported {self.log.count()} QSOs to {path}"
        except OSError as exc:
            self.msg = f"Export failed: {exc}"

    def key(self, scr, ch):
        """Returns False to quit."""
        name = FIELDS[self.focus]
        if ch in (curses.KEY_F10, "\x18"):                       # F10, Ctrl-X
            return False
        if ch == curses.KEY_F2:
            self.export("adif")
        elif ch == curses.KEY_F3:
            self.export("cabrillo")
        elif ch == curses.KEY_F4:
            self.export("stf")
        elif ch == curses.KEY_F8:
            rows = self.log.rows(last=1)
            if rows:
                self.msg = f"Delete last QSO with {rows[0]['call']}? (y/N)"
                self.draw(scr)
                scr.timeout(-1)
                answer = scr.get_wch()
                scr.timeout(200)
                if answer in ("y", "Y"):
                    self.log.delete_last()
                    self.msg = "Deleted."
                else:
                    self.msg = "Kept."
        elif ch in ("\n", "\r", curses.KEY_ENTER):
            self.enter()
        elif ch == "\x1b":                                       # Esc
            self.clear()
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
        elif isinstance(ch, str) and ch.isprintable():
            if len(self.val[name]) < WIDTHS[name]:
                self.val[name] += ch.upper()
            self.dupe_ok = ""
        return True

    # ---- drawing
    def draw(self, scr):
        scr.erase()
        h, w = scr.getmaxyx()

        def put(y, x, text, attr=0):
            if 0 <= y < h and 0 <= x < w:
                try:
                    scr.addstr(y, x, text[:w - x - 1], attr)
                except curses.error:
                    pass

        rig = "RIG ok" if self.rig.online else "RIG offline (manual)"
        utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + "Z"
        head = (f" {self.mycall}  {self.cfg['contest']['contest']}  |  {fmt_freq(self.freq())} kHz  {self.mode()}  "
                f"{band_of(self.freq() or 0)}  |  {rig}  |  QSOs: "
                f"{self.log.count()}  |  {utc} ")
        put(0, 0, head.ljust(w - 1), curses.A_REVERSE)

        put(2, 1, f"{'#':>4} {'UTC':<16} {'Call':<13} {'kHz':>9} {'Mode':<5}"
                  f"{'Sent':<5}{'Exch':<7}{'Rcvd':<5}{'Exch':<10} Name",
            curses.A_BOLD)
        space = max(h - 11, 1)
        for i, q in enumerate(self.log.rows(last=space)):
            put(3 + i, 1,
                f"{q['id']:>4} {q['ts'][:16]:<16} {q['call']:<13} "
                f"{(q['freq_hz'] or 0) / 1000:>9.1f} {q['mode']:<5}"
                f"{q['rst_s']:<5}{q['exch_s']:<7}{q['rst_r']:<5}"
                f"{q['exch_r']:<10} {q['name']}")

        call = self.val["call"]
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
        put(h - 7, 1, line, curses.A_BOLD)
        dupe = self.dupe()
        if dupe:
            put(h - 6, 1, " " + self.dupe_text(dupe) + " ",
                self.warn_attr | curses.A_BOLD)

        x, cursor = 1, (h - 4, 1)
        for i, name in enumerate(FIELDS):
            put(h - 5, x, LABELS[name])
            shown = self.val[name]
            if not shown and name.startswith("rst"):
                shown = self.default_rst()
            attr = curses.A_REVERSE if i == self.focus else curses.A_UNDERLINE
            put(h - 4, x, shown.ljust(WIDTHS[name]), attr)
            if i == self.focus:
                cursor = (h - 4, x + len(self.val[name]))
            x += max(WIDTHS[name], len(LABELS[name])) + 2
        put(h - 5, x, "Exch sent")
        put(h - 4, x, self.exch_sent() or "-")

        put(h - 2, 1, self.msg)
        put(h - 1, 0, " Enter log | Tab next | Space call>exch | Esc clear | "
                      "<kHz> Enter = QSY | CW/SSB/FM Enter = mode | F2 ADIF | "
                      "F3 Cabrillo | F4 STF | F8 del last | F10 quit ".ljust(w - 1),
            curses.A_REVERSE)
        try:
            scr.move(*cursor)
        except curses.error:
            pass
        scr.refresh()

    def run(self, scr):
        if hasattr(curses, "set_escdelay"):
            curses.set_escdelay(25)
        self.warn_attr = curses.A_REVERSE
        if curses.has_colors():
            curses.start_color()
            curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_RED)
            self.warn_attr = curses.color_pair(1)
        curses.curs_set(1)
        scr.keypad(True)
        scr.timeout(200)
        self.rig.start()
        while True:
            self.draw(scr)
            try:
                ch = scr.get_wch()
            except curses.error:
                continue                 # timeout: just redraw
            except KeyboardInterrupt:
                return
            if ch == curses.KEY_RESIZE:
                continue
            if not self.key(scr, ch):
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
    ap.add_argument("--export-stf", metavar="FILE")
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
    dupe_rules(cfg)                      # fail early on a bad setting
    log = Log(args.logfile)
    if args.export_adif or args.export_cabrillo or args.export_stf:
        if args.export_stf:
            print("STF:", log.export_stf(args.export_stf, cfg))
        if args.export_adif:
            print("ADIF:", log.export_adif(args.export_adif))
        if args.export_cabrillo:
            print("Cabrillo:", log.export_cabrillo(args.export_cabrillo, cfg))
        return
    curses.wrapper(App(cfg, log).run)
    print(f"{log.count()} QSOs in {args.logfile}")


if __name__ == "__main__":
    main()
