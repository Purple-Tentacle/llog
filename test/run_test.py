"""End-to-end test: fake rigctld + the real UI driven through a pty."""
import os, pty, select, socket, sqlite3, sys, threading, time, importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "llog.py")
state = {"freq": 14060000, "mode": "CW", "cmds": []}


def fake_rigctld(server):
    while True:
        conn, _ = server.accept()
        f = conn.makefile("r")
        for line in f:
            line = line.strip()
            state["cmds"].append(line)
            if line == "f":
                conn.sendall(f"{state['freq']}\n".encode())
            elif line == "m":
                conn.sendall(f"{state['mode']}\n500\n".encode())
            elif line.startswith("F "):
                state["freq"] = int(line.split()[1]); conn.sendall(b"RPRT 0\n")
            elif line.startswith("M "):
                state["mode"] = line.split()[1]; conn.sendall(b"RPRT 0\n")
            else:
                conn.sendall(b"RPRT -1\n")


server = socket.socket(); server.bind(("127.0.0.1", 0)); server.listen()
port = server.getsockname()[1]
threading.Thread(target=fake_rigctld, args=(server,), daemon=True).start()

work = os.path.join(HERE, "work"); os.makedirs(work, exist_ok=True)
db = os.path.join(work, "t.db")
import shutil; shutil.rmtree(work); os.makedirs(work)
os.makedirs(os.path.join(work, "contests"), exist_ok=True)
open(os.path.join(work, "config.ini"), "w").write(f"""[station]
call = DA2IT
email = test@example.org
[qrz]
username =
password =
[rig]
host = 127.0.0.1
port = {port}
""")
EX = os.path.join(os.path.dirname(HERE), "examples")
open(os.path.join(work, "contests", "wsa.ini"), "w").write(
    open(os.path.join(EX, "wsa.ini")).read()
    .replace("exchange_sent =", "exchange_sent = N99")
    .replace("specific =", "specific = N99"))

pid, fd = pty.fork()
if pid == 0:
    os.environ["TERM"] = "xterm"; os.environ["LINES"] = "30"; os.environ["COLUMNS"] = "120"
    os.execv(sys.executable, [sys.executable, APP, "wsa", db, "--config-dir", work])

screen = b""
def pump(sec):
    global screen
    end = time.time() + sec
    while time.time() < end:
        if select.select([fd], [], [], 0.05)[0]:
            try: screen += os.read(fd, 65536)
            except OSError: return
def send(s, wait=0.6):
    os.write(fd, s.encode()); pump(wait)

pump(1.5)
send("dl1abc/p"); send(" "); send("o05"); send("\r")          # QSO 1 on 14060 CW
send("dl1abc/p\r"); assert b"DUPE" in screen                 # dupe: 1st Enter warns
send("\x1b")                                                 # Esc, not logged
send("7030.5\r", 1.2)                                         # QSY via logger
send("ssb\r", 1.2)                                            # mode via logger
send("dk2xyz\t"); send("57\t"); send("55\t"); send("nm\r")    # QSO 2 with RSTs
send("oops1x\r"); send("\x1b[19~"); send("y")                 # QSO 3, F8 delete
UP = "\x1bOA"
send(UP); send("\t\t\t"); send("\x7f\x7f"); send("o05\r")      # edit QSO 2 exchange
send(UP); send(UP); send("x"); send("\x1b")                   # edit QSO 1, cancel
send("oops2x\r"); send(UP); send("\x1b[19~"); send("y")        # delete selected QSO
send("zz\x7f\x7f")
send(UP); send(UP); send("\x7f\x7f"); send("\r")               # QSO 1: DL1ABC/P -> DL1ABC
send("\x1bOQ"); send("\x1bOR")                            # F2, F3 exports
send("\x1b[21~", 1.0)                                         # F10 quit
os.waitpid(pid, 0)

rows = sqlite3.connect(db).execute(
    "SELECT call,freq_hz,mode,rst_s,rst_r,exch_s,exch_r FROM qso").fetchall()
print(rows)
assert rows == [("DL1ABC", 14060000, "CW", "599", "599", "N99", "O05"),
                ("DK2XYZ", 7030500, "SSB", "57", "55", "N99", "O05")], rows
assert "F 7030500" in state["cmds"] and "M LSB 0" in state["cmds"], state["cmds"][-20:]
for name in sorted(os.listdir(work)):
    if name.endswith((".adi", ".cbr")):
        print("-----", name); print(open(os.path.join(work, name)).read())
assert b"RIG ok" in screen

spec = importlib.util.spec_from_file_location("llog", APP)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
s, c = m.parse_qrz('<?xml version="1.0"?><QRZDatabase version="1.34" '
    'xmlns="http://xmldata.qrz.com"><Callsign><call>DL1ABC</call><fname>Jörg</fname>'
    '<name>Muster</name><addr2>Münster</addr2><country>Germany</country>'
    '<grid>JO31</grid></Callsign><Session><Key>abc</Key></Session></QRZDatabase>')
assert s["Key"] == "abc" and c["fname"] == "Jörg" and c["grid"] == "JO31"
assert m.parse_freq("144300") == 144300000 and m.parse_freq("14.060") == 14060000
assert m.cabrillo_freq(144300000) == "144" and m.cabrillo_freq(7030500) == "7030"
# dupe rules
from datetime import datetime, timezone, timedelta
lg = m.Log(os.path.join(work, "d.db"))
lg.add(ts="2026-12-01 18:10:00", mycall="DA2IT", call="DL1ABC", freq_hz=144300000,
       mode="SSB", rst_s="59", rst_r="59", exch_s="N99", exch_r="N01")
R = lambda **k: dict({"check": "band", "ignore_suffix": False, "exchange": False,
                      "minutes": 0}, **k)
now = datetime(2026, 12, 1, 18, 30, tzinfo=timezone.utc)
d = lambda rules, call="DL1ABC", f=144250000, mode="FM", exch="": bool(
    lg.find_dupe(rules, call, f, mode, exch, now))
assert d(R()) and not d(R(), f=432200000)                    # once per band
assert not d(R(check="band_mode"), mode="CW")                # CW vs phone
assert d(R(check="band_mode"), mode="FM")                    # FM and SSB = phone
assert d(R(check="once"), f=432200000, mode="CW")
assert d(R(check="mode"), f=432200000) and not d(R(check="mode"), mode="CW")
assert not d(R(check="off"))
assert not d(R(), call="DL1ABC/P") and d(R(ignore_suffix=True), call="DL1ABC/P")
assert not d(R(), call="DL1AB")                              # no substring match
assert d(R(exchange=True)) and d(R(exchange=True), exch="N01")
assert not d(R(exchange=True), exch="N02")                   # moved: new exchange
assert d(R(minutes=30)) and not d(R(minutes=15))             # 20 minutes ago
# scoring with the shipped example files
import configparser
def rules_for(name):
    c = configparser.ConfigParser(); c.read(os.path.join(EX, name))
    return m.dupe_rules(c), m.score_rules(c)
def rows(*qsos):
    return [dict(id=i, ts=f"2026-12-01 18:{i:02d}:00", call=c, freq_hz=f, mode=mo,
                 exch_r=e) for i, (c, f, mo, e) in enumerate(qsos, 1)]
V, U = 144300000, 432200000
dr, sr = rules_for("wna.ini")
r = m.compute_score(rows(("DL1A", V, "FM", "N01"),    # 2, mult N01 on 2m
                         ("DL1B", V, "SSB", "N01"),   # 4, same mult
                         ("DL1A", U, "CW", "N01"),    # 6, mult N01 on 70cm
                         ("DL1C", V, "FM", "NM"),     # 2, NM is no multiplier
                         ("DL1A", V, "FM", "N01"),    # dupe
                         ("DL1D", V, "FM", "WN"),     # 2, special DOK
                         ("DL1E", V, "FM", "O05")),   # 2, other district
                    dr, sr)
assert (r["points"], r["mults"], r["score"]) == (18, 3, 54), r
assert r["per_qso"][5] == (0, True, False) and r["bands"]["2m"]["dupes"] == 1
dr, sr = rules_for("wsa.ini")
r = m.compute_score(rows(("DL1A", V, "CW", "O05"), ("DL1B", V, "FM", "NOTO"),
                         ("DL1C", V, "SSB", "N01"), ("DL1D", V, "FM", "O07"),
                         ("DL1E", U, "FM", "O05"), ("DL1F", V, "FM", "NM")),
                    dr, sr)
assert (r["points"], r["mults"], r["score"]) == (18, 3, 54), r
assert "CLAIMED-SCORE: 20" in open(os.path.join(work, "DA2IT-" +
    datetime.now(timezone.utc).strftime("%Y-%m-%d") + "-1.cbr")).read()
import re; txt = re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]", b" ", screen)
assert b"NEW MULTIPLIER: O05" in txt and b"Score: 6x1=6" in txt
print("ALL OK")
