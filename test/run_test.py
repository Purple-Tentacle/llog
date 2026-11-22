"""End-to-end test: fake rigctld + the real UI driven through a pty."""
import configparser, os, pty, select, socket, sqlite3, sys, threading, time, importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "llog.py")
state = {"vfo": "A", "freq": {"A": 14060000, "B": 432200000},
         "mode": {"A": "CW", "B": "FM"}, "cmds": []}


def fake_rigctld(server):          # plain rigctld (no --vfo): acts on active VFO
    while True:
        conn, _ = server.accept()
        threading.Thread(target=fake_client, args=(conn,), daemon=True).start()


def fake_client(conn):
    if True:
        f = conn.makefile("r")
        for line in f:
            line = line.strip()
            state["cmds"].append(line)
            v = state["vfo"]
            if line == "f":
                conn.sendall(f"{state['freq'][v]}\n".encode())
            elif line == "m":
                conn.sendall(f"{state['mode'][v]}\n500\n".encode())
            elif line == "v":
                conn.sendall(f"VFO{v}\n".encode())
            elif line == "\\chk_vfo":
                conn.sendall(b"CHKVFO 0\n")
            elif line == "\\dump_caps":
                conn.sendall(b"Caps dump for model: 2044\nModel name:\tKX2\n"
                             b"Mfg name:\tElecraft\n")
            elif line.startswith("w "):
                conn.sendall(b"\n")
            elif line.startswith("V VFO"):
                state["vfo"] = line[-1]; conn.sendall(b"RPRT 0\n")
            elif line.startswith("F "):
                state["freq"][v] = int(line.split()[1]); conn.sendall(b"RPRT 0\n")
            elif line.startswith("M "):
                state["mode"][v] = line.split()[1]; conn.sendall(b"RPRT 0\n")
            else:
                conn.sendall(b"RPRT -1\n")


server = socket.socket(); server.bind(("127.0.0.1", 0)); server.listen()
port = server.getsockname()[1]
threading.Thread(target=fake_rigctld, args=(server,), daemon=True).start()

def fake_cluster(srv):             # DX cluster node: login prompt, then spots
    conn, _ = srv.accept()
    conn.sendall(b"\xff\xfb\x01login: ")
    state["cluster_login"] = conn.recv(100)
    conn.sendall(b"Hello\r\n"
                 b"DX de DL1AAA:     14025.0  K1ABC        CQ TEST                        2153Z\r\n"
                 b"DX de DL2BBB-#:    7033.0  DK9ZZ        cq                             2154Z\r\n"
                 b"DX de DL2BBB-#:    7012.0  DJ5ZZ        cq                             2153Z\r\n"
                 b"DX de DL3CCC:     14060.0  DL1ABC/P     wsa                            2155Z JO31\r\n")
    state["cluster_rx"] = b""
    while True:
        data = conn.recv(1000)
        if not data:
            break
        state["cluster_rx"] += data


csrv = socket.socket(); csrv.bind(("127.0.0.1", 0)); csrv.listen()
threading.Thread(target=fake_cluster, args=(csrv,), daemon=True).start()

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
[cluster]
host = 127.0.0.1
port = {csrv.getsockname()[1]}
call = da2it-1
""")
EX = os.path.join(os.path.dirname(HERE), "examples")
open(os.path.join(work, "contests", "wsa.ini"), "w").write(
    open(os.path.join(EX, "wsa.ini")).read()
    .replace("exchange_sent =", "exchange_sent = N99")
    .replace("specific =", "specific = N99"))

pid, fd = pty.fork()
if pid == 0:
    os.environ["TERM"] = "xterm"; os.environ["LINES"] = "30"; os.environ["COLUMNS"] = "140"
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
UP, BTAB = "\x1bOA", "\x1b[Z"
send("dl9"); send(UP)                                         # typed text stays in row
send("\t" * 6); send("\x7f\x7f"); send("o05\r")               # edit QSO 2 exchange in list
send(UP); send(UP); send("x"); send("\x1b")                   # edit QSO 1, cancel
send("\x7f\x7f\x7f")                                          # remove "dl9" again
send("oops2x\r"); send(UP); send("\x1b[19~"); send("y")        # delete selected QSO
send(UP); send(UP); send("\x7f\x7f"); send("\r")               # QSO 1: DL1ABC/P -> DL1ABC
send(UP); send("\t"); send("\x7f" * 6); send("7040\t")         # QSO 2: frequency,
send("\x7f" * 3); send("cw"); send(BTAB * 3)                   # mode, back to time cell
send("\x7f" * 2); send("\r")                                  # bad time refused
assert b"Time must look like" in screen
send("59\r")                                                  # time fixed, saved
send("dl7");  send("\\", 1.2)                                  # leave text in row A, go to VFO B
send("432200\r", 1.2); send("fm\r", 1.2)                       # row B: own frequency and mode
send("db1xx\r"); send("\\", 1.2)                              # QSO in row B, back to A
send("\x7f\x7fk3aa\r")                                        # row A kept "DL7": DLK3AA
send("\x1b[6~"); send("\x1b[6~"); send("\x1b[5~")              # PgDn PgDn PgUp: latest 40m spot
send("\r", 1.2); send("\r")                                   # take it (QSY + call), log it
send("\x1b[17~"); send("\x1b")                                # F6: spot last QSO, cancel
send("\x1b[17~"); send("tnx Qso\r")                            # F6 again, comment, send
send("\x1b[15~", 1.5)                                         # F5: ATU tune
send("\x1bOQ"); send("\x1bOR")                            # F2, F3 exports
send("\x1b[21~", 1.0)                                         # F10 quit
os.waitpid(pid, 0)

rows = sqlite3.connect(db).execute(
    "SELECT call,freq_hz,mode,rst_s,rst_r,exch_s,exch_r FROM qso").fetchall()
print(rows)
assert rows == [("DL1ABC", 14060000, "CW", "599", "599", "N99", "O05"),
                ("DK2XYZ", 7040000, "CW", "57", "55", "N99", "O05"),
                ("DB1XX", 432200000, "FM", "59", "59", "N99", ""),
                ("DK3AA", 7030500, "SSB", "59", "59", "N99", ""),
                ("DJ5ZZ", 7012000, "SSB", "59", "59", "N99", "")], rows
assert "F 7012000" in state["cmds"]
assert state["cluster_rx"].count(b"DX ") == 1, state["cluster_rx"]
assert b"DX 7012.0 DJ5ZZ tnx Qso\r\n" in state["cluster_rx"]
# default vfo_switch = retune: no VFO select, row A is retuned when coming back
assert not any(c.startswith("V ") for c in state["cmds"])
assert state["cmds"].count("F 7030500") == 2 and "M LSB -1" in state["cmds"]
assert "F 432200000" in state["cmds"] and "M FM 0" in state["cmds"]
assert "w SWT20;" in state["cmds"] and b"sent: w SWT20;" in screen
assert sqlite3.connect(db).execute(
    "SELECT ts FROM qso WHERE call='DK2XYZ'").fetchone()[0].endswith(":59")
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
assert m.parse_ts("2026-12-01 18:45") == "2026-12-01 18:45:00"
assert m.parse_ts("18:45") is None and m.khz_text(7030500) == "7030.5"
# rig thread against a rigctld in --vfo mode: both VFOs are read and addressed
vstate = {"freq": {"A": 144300000, "B": 432200000}, "cmds": []}
def fake_vfo_rigctld(srv):
    conn, _ = srv.accept()
    for line in conn.makefile("r"):
        p = line.split(); vstate["cmds"].append(line.strip())
        if p[0] == "\\chk_vfo": conn.sendall(b"CHKVFO 1\n")
        elif p[0] == "v": conn.sendall(b"VFOA\n")
        elif p[0] == "f": conn.sendall(f"{vstate['freq'][p[1][-1]]}\n".encode())
        elif p[0] == "m": conn.sendall(b"FM\n15000\n")
        elif p[0] == "F": vstate["freq"][p[1][-1]] = int(p[2]); conn.sendall(b"RPRT 0\n")
        else: conn.sendall(b"RPRT 0\n")
srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen()
threading.Thread(target=fake_vfo_rigctld, args=(srv,), daemon=True).start()
rig = m.Rig("127.0.0.1", srv.getsockname()[1], retune=False); rig.start(); time.sleep(1)
assert rig.vfo_mode and rig.online and rig.freq == {"A": 144300000, "B": 432200000}
rig.set_freq("B", 433500000); time.sleep(1)
assert "F VFOB 433500000" in vstate["cmds"] and rig.freq["B"] == 433500000
rig.select("B"); time.sleep(1); assert "V VFOB" in vstate["cmds"]
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
assert "CLAIMED-SCORE: 44" in open(os.path.join(work, "DA2IT-" +
    datetime.now(timezone.utc).strftime("%Y-%m-%d") + "-1.cbr")).read()
import re; txt = re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]", b" ", screen)
assert b"NEW MULTIPLIER: O05" in txt and b"Score: 6x1=6" in txt
assert b"Mults" in txt and b"NOTO" in txt and b"O55" in txt
assert b"DX cluster" in txt and b"K1ABC" in txt and state["cluster_login"].startswith(b"DA2IT-1\r\n")
cl = m.Cluster("", 0, "DA2IT")
cl.feed("DX de DL1AAA:     14025.0  K1ABC        CQ TEST                        2153Z")
cl.feed("DX de DL2BBB-#:    7030.5  DK9ZZ        cq                             2154Z")
cl.feed("DX de DL1AAA:     14027.3  K1ABC        QSY                            2156Z")
cl.feed("To ALL de DL1AAA: hello"); cl.feed("DX de garbage")
assert [(x["call"], x["freq_hz"], x["time"]) for x in cl.spots("20m")] == \
    [("K1ABC", 14027300, "2156")] and len(cl.spots("40m")) == 1 and not cl.spots("2m")
cl._spots[("K1ABC", "20m")]["seen"] -= 31 * 60; assert not cl.spots("20m")   # expired
now_utc = datetime.now(timezone.utc)
cl.feed(f"  14010.0  W1AW        {now_utc:%d-%b-%Y %H%M}Z  sh/dx line                 <DL4DDD>")
cl.feed("  14011.0  W2OLD       01-Jan-2020 1200Z  long gone                  <DL4DDD>")
assert [x["call"] for x in cl.spots("20m")] == ["W1AW"]
app = m.App.__new__(m.App)                      # panel logic without a terminal
c = configparser.ConfigParser(); c.read(os.path.join(EX, "wna.ini"))
app.scoring = m.score_rules(c); app.freq = lambda: V
app.score = lambda: {"seen": {("2m", "WN"), ("70cm", "DVN"), ("2m", "N01")}}
panel = app.mult_panel(30)
flat = [item for row in panel for item in row]
assert flat[0] == ("Mults 2m (2 worked):", False), flat
assert ("WN", True) in flat and ("DVN", False) in flat and len(panel) > 1
assert flat[-1] == ("N01", True) and len(flat) == 10      # 8 named + worked N01
print("ALL OK")
