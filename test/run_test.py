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
        if b"DX " in data:           # the node refuses the spot
            conn.sendall(b"Sorry DA2IT-1, you are not registered\r\n"
                         b"DA2IT-1 de TEST-2 22-Nov-2026 2200Z dxspider >\r\n")


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
    .replace("specific =", "specific = N99")
    .replace("locator =", "locator = JO31AA"))

pid, fd = pty.fork()
if pid == 0:
    os.environ["TERM"] = "xterm"; os.environ["LINES"] = "30"; os.environ["COLUMNS"] = "160"
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
send("dk2xyz\t"); send("57\t"); send("55\t"); send("nm\t"); send("jo40\r")   # QSO 2 with RSTs and locator
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
send("\x1b[17~"); send("tnx Qso\r", 1.2)                       # F6 again, comment, send
assert b"you are not registered" in screen and b"dxspider >" not in screen
send("dl8zz\n"); send("dl8yy"); send("\x1b[20~")               # Ctrl-J and F9: store for later
send("\x1b[18~")                                              # F7: band map
send("\x1bm"); send("t", 1.5)                                  # Alt-M, T: ATU tune
send("\x1bm"); send("b")                                       # Alt-M, B: backup now
send("\x1bm"); send("r"); send("\x1b"); send("\x1b")           # Radio submenu, back, close
send("\x1bm"); send("\x1b")                                   # Alt-M, Esc: menu closed again
send("\x1bm"); send("\r")                                     # Alt-M, Enter: ADIF
send("\x1bm"); send("\x1bOB"); send("\r")                     # Alt-M, Down, Enter: Cabrillo                            # F2, F3 exports
send("\x1b[21~")                                              # F10 no longer quits
send("\x1bm"); send("q", 1.0)                                  # Alt-M, Q: quit
os.waitpid(pid, 0)

assert [f for f in os.listdir(os.path.join(work, "backups")) if f.startswith("t-")]  # on quit
rows = sqlite3.connect(db).execute(
    "SELECT call,freq_hz,mode,rst_s,rst_r,exch_s,exch_r FROM qso").fetchall()
print(rows)
assert rows == [("DL1ABC", 14060000, "CW", "599", "599", "N99", "O05"),
                ("DK2XYZ", 7040000, "CW", "57", "55", "N99", "O05"),
                ("DB1XX", 432200000, "FM", "59", "59", "N99", ""),
                ("DK3AA", 7030500, "SSB", "59", "59", "N99", ""),
                ("DJ5ZZ", 7012000, "SSB", "59", "59", "N99", "")], rows
assert "F 7012000" in state["cmds"]
assert sorted(sqlite3.connect(db).execute("SELECT call, freq_hz FROM later").fetchall()) == \
    [("DL8YY", 7012000), ("DL8ZZ", 7012000)]
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
assert b"| KX2 |" in screen                    # radio name from rigctld in the header

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
app3 = m.App(m.load_config(work, "wsa"), m.Log(os.path.join(work, "c.db")))
app3.manual_freq["A"] = 14060000
app3.cluster.feed("DX de DL1AAA:     14025.0  K1ABC        CQ TEST                        2153Z")
assert app3.panel_rows()[0]["text"] == " 14025.0 K1ABC        2153 CQ TEST"
# band map: spots, stored and worked stations by frequency, with our position
app3.cluster.feed("DX de DL1AAA:     14010.0  W1AW         cq                             2154Z")
app3.cluster.feed("DX de DL1AAA:      7010.0  DK9ZZ        other band                     2154Z")
app3.log.add(ts="2026-12-01 18:10:00", mycall="DA2IT", call="DL1WKD", freq_hz=14040000,
             mode="SSB", rst_s="59", rst_r="59", exch_s="N99", exch_r="N01")
app3.log.add(ts="2026-12-01 18:11:00", mycall="DA2IT", call="W1AW", freq_hz=14010500,
             mode="SSB", rst_s="59", rst_r="59", exch_s="N99", exch_r="")
app3.val["call"] = "DL8ZZ"; app3.manual_freq["A"] = 14050000; app3.store_for_later()
assert app3.val["call"] == "" and app3.log.later_rows("20m")[0]["call"] == "DL8ZZ"
app3.manual_freq["A"] = 14030000
view = lambda: [(r["kind"], r["call"], r["worked"]) for r in app3.panel_rows()]
assert view() == [("stored", "DL8ZZ", False), ("spot", "W1AW", True), ("spot", "K1ABC", False)]
app3.toggle_view()
assert view() == [("spot", "W1AW", True), ("spot", "K1ABC", False), ("here", "", False),
                  ("worked", "DL1WKD", True), ("stored", "DL8ZZ", False)], view()
assert app3.panel_rows()[4]["text"].startswith("*14050.0 DL8ZZ") and \
    app3.panel_rows()[2]["text"].startswith(">14030.0")
app3.manual_freq["A"] = 14040100                             # tuned onto DL1WKD
rows3 = app3.panel_rows()
assert [r["kind"] for r in rows3] == ["spot", "spot", "worked", "stored"]   # no extra line
assert rows3[2]["text"].startswith(">14040.0 DL1WKD") and not rows3[1].get("here")
app3.manual_freq["A"] = 14030000
app3.spot_step(+1); assert app3.spot == "DL1WKD"            # next above our frequency
app3.spot_step(+1); assert app3.spot == "DL8ZZ"
app3.spot = ""; app3.spot_step(-1); assert app3.spot == "K1ABC"   # next below
app3.spot = "DL8ZZ"; assert app3.spot_take() and app3.val["call"] == "DL8ZZ"
assert app3.manual_freq["A"] == 14050000
app3.enter()                                                 # logging removes "stored"
assert not app3.log.later_rows("20m") and app3.log.count() == 3
app3.val["call"] = "DL7YY"; app3.store_for_later(); app3.spot = "DL7YY"
app3.spot_delete(); assert not app3.log.later_rows("20m")
app3.toggle_view(); assert app3.view == "newest"
# a multiplier inside a longer exchange ("001 N13")
mr = {"mults": ["N[0-9][0-9]", "DVN"], "mult_per": "band"}
F2M = 144300000
assert m.mult_key(mr, "001 N13", F2M) == ("2m", "N13") and m.mult_key(mr, "n13", F2M) == ("2m", "N13")
assert m.mult_key(mr, "dvn 5", F2M) == ("2m", "DVN") and m.mult_key(mr, "001 NM", F2M) is None
# worked-before information from an ADIF file
adi = os.path.join(work, "logbook.adi")
open(adi, "w").write("""Exported logbook <ADIF_VER:5>3.1.4 <EOH>
<CALL:5>DK2NI <QSO_DATE:8>20261103 <TIME_ON:4>1905 <BAND:2>2m <MODE:2>FM <NAME:5>Horst <GRIDSQUARE:6>JO31SW <DARC_DOK:3>N13 <EOR>
<call:5>dk2ni <qso_date:8>20251007 <time_on:6>190000 <freq:8>432.2000 <mode:3>SSB <eor>
<CALL:5>W1AW <QSO_DATE:8>20240101 <BAND:3>20M <MODE:2>CW <COMMENT:12>with <odd> x <SRX_STRING:5>05 CT <EOR>
<QSO_DATE:8>20240101 <EOR>
""")
recs = m.parse_adif(open(adi).read())
assert len(recs) == 4 and recs[2]["COMMENT"] == "with <odd> x" and recs[1]["CALL"] == "dk2ni"
hist = m.CallHistory(adi)
e = hist.lookup("DK2NI/P")
assert (e["count"], e["last"], e["band"], e["mode"], e["bands"]) == (2, "20261103", "2m", "FM", ["70cm", "2m"])
assert hist.text(e) == ("Worked before: 2 QSOs, last 2026-11-03 2m FM  |  bands 70cm 2m  |  "
                        "N13  |  Horst  |  JO31SW")
assert hist.text(hist.lookup("W1AW")) == "Worked before: 1 QSO, last 2024-01-01 20m CW  |  bands 20m  |  05 CT"
assert hist.lookup("DL1XYZ") is None and not m.CallHistory("").calls
c5 = m.load_config(work, "wsa"); c5["station"]["worked_before"] = adi
assert m.load_history(c5).lookup("DK2NI")
# country file, continent points, country and station multipliers (OK DX RTTY)
open(os.path.join(work, "cty.dat"), "w").write("""Fed. Rep. of Germany:      14:  28:  EU:   51.00:   -10.00:    -1.0:  DL:
    DA,DB,DC,DD,DE,DF,DG,DH,DI,DJ,DK,DL,DM,DN,DO,DP,DQ,DR,Y2;
Czech Republic:            15:  28:  EU:   50.00:   -16.00:    -1.0:  OK:
    OK,OL;
United States:             05:  08:  NA:   37.53:    91.67:     5.0:  K:
    AA,AB,K,N,W,=W1AW(5)[8],K7(3)[6],KH6(31)[61]<21.12/157.48>{OC}~10.0~;
Canary Islands:            33:  36:  AF:   28.32:    15.85:     0.0:  EA8:
    EA8,EH8;
Spain:                     14:  37:  EU:   40.37:     4.88:    -1.0:  EA:
    EA,EH;
African Italy:             33:  37:  AF:   35.67:   -12.67:    -1.0:  *IG9:
    IG9,IH9;
""")
cty = m.CountryFile(os.path.join(work, "cty.dat"))
look = lambda c: (lambda i: i and (i["prefix"], i["cont"], i["cq"]))(cty.lookup(c))
assert look("DA2IT") == ("DL", "EU", "14") and look("OL5Y") == ("OK", "EU", "15")
assert look("K7ABC") == ("K", "NA", "3") and look("KH6XX") == ("K", "OC", "31")
assert look("W1AW") == ("K", "NA", "5") and look("EA8/DL1XYZ") == ("EA8", "AF", "33")
assert look("DL1XYZ/EA8") == ("EA8", "AF", "33") and look("DL1ABC/P") == ("DL", "EU", "14")
assert look("EA3ABC/7") == ("EA", "EU", "14") and look("IG9ABC") == ("I", "AF", "33")
assert look("DL1ABC/MM") is None and look("ZZ9ZZ") is None
assert cty.lookup("K1ABC")["lon"] == -91.67 and cty.lookup("DL1ABC")["lon"] == 10.0   # east positive
c6 = m.load_config(work, os.path.join(EX, "ok-dx-rtty.ini"))
dr6, sr6 = m.dupe_rules(c6), m.score_rules(c6)
assert sr6["home"]["prefix"] == "DL" and sr6["has_mults"]
def rtty(*qsos):
    return [dict(id=i, ts=f"2026-12-19 10:{i:02d}:00", call=c, freq_hz=f, mode="RTTY",
                 exch_r=e) for i, (c, f, e) in enumerate(qsos, 1)]
r = m.compute_score(rtty(("OK1AAA", 14085000, "15"),      # 1 pt, mults OK + OK1AAA
                         ("OK1AAA", 3585000, "15"),       # 3 pt, mults OK + OK1AAA on 80m
                         ("K1ABC", 14086000, "05"),       # 2 pt (DX), mult K
                         ("K1ABC", 7045000, "05"),        # 6 pt, mult K on 40m
                         ("DL1ABC", 14087000, "14"),      # 1 pt, mult DL
                         ("OL5Y", 14088000, "15"),        # 1 pt, mult OL5Y (OK already)
                         ("EA8/DL1XYZ", 21085000, "33"),  # 2 pt (Africa), mult EA8
                         ("K1ABC", 14086000, "05"),       # dupe
                         ("ZZ9ZZ", 14089000, "99")),      # unknown country: 0 pt
                    dr6, sr6)
assert (r["points"], r["mults"], r["score"]) == (16, 9, 144), r
assert r["per_qso"][6] == (1, False, True) and r["per_qso"][8] == (0, True, False)
assert m.qso_mults(sr6, "OK2BBB", "15", 14085000) == [("20m", "OK"), ("20m", "OK2BBB")]
app6 = m.App(c6, m.Log(os.path.join(work, "ok.db")))
app6.manual_freq["A"] = 14085000; app6.manual_mode["A"] = "SSB"
assert app6.mode() == "RTTY" and app6.default_rst() == "599"     # mode = RTTY forces it
app6.val["call"] = "OK2BBB"; assert app6.new_mult() == "OK, OK2BBB"
app6.val["exch"] = "15"; app6.enter()
cab6 = os.path.join(work, "ok.cbr"); app6.log.export_cabrillo(cab6, c6); cab6 = open(cab6).read()
assert "CONTEST: OK-DX-RTTY" in cab6 and "CATEGORY-BAND: ALL" in cab6
assert "CATEGORY-POWER: LOW" in cab6 and "CATEGORY-MODE: RTTY" in cab6
assert "CLAIMED-SCORE: 2" in cab6 and "QSO: 14085 RY " in cab6 and " 599 14 " in cab6
# the same contest operated from OK: own callsign per contest, no station multipliers
c7 = m.load_config(work, os.path.join(EX, "ok-dx-rtty.ini"))
c7["contest"]["call"] = "ok/da2it"; open(os.path.join(work, "contests", "okp.ini"), "w").write(
    "[contest]\n" + "\n".join(f"{k} = {v}" for k, v in c7["contest"].items() if v))
c7 = m.load_config(work, "okp"); sr7 = m.score_rules(c7)
assert c7["station"]["call"] == "OK/DA2IT" and sr7["home"]["prefix"] == "OK"
assert m.qso_mults(sr7, "OK2BBB", "15", 14085000) == [("20m", "OK")]
assert m.qso_points(sr7, "OK2BBB", "RTTY", 3585000) == 3 and m.qso_points(sr7, "K1ABC", "RTTY", 3585000) == 6
# prefix and DOK multipliers (DARC CW-Ausbildungscontest)
for call, want in (("DL1ABC", "DL1"), ("DA2IT", "DA2"), ("9A2AA", "9A2"), ("3DA0RU", "3DA0"),
                   ("DL2025X", "DL2025"), ("DL1ABC/P", "DL1"), ("DL1ABC/7", "DL7"),
                   ("EA8/DL1ABC", "EA8"), ("LX/DL1ABC", "LX0"), ("DL1ABC/QRP", "DL1")):
    assert m.wpx_prefix(call) == want, (call, m.wpx_prefix(call))
c8 = m.load_config(work, os.path.join(EX, "darc-cwa.ini")); dr8, sr8 = m.dupe_rules(c8), m.score_rules(c8)
def cw(*qsos):
    return [dict(id=i, ts=f"2026-12-08 18:{i:02d}:00", call=c, freq_hz=3530000, mode="CW",
                 exch_r=e) for i, (c, e) in enumerate(qsos, 1)]
r = m.compute_score(cw(("DL1ABC", "H24"),     # prefix DL1 + DOK H24
                       ("DL1XYZ", "NM"),      # prefix known, NM is no multiplier
                       ("OK1AAA", "001"),     # prefix OK1, serial number is none
                       ("DK5ZZ", "H24"),      # prefix DK5, DOK known
                       ("DF0WN", "WN"),       # prefix DF0 + special DOK WN
                       ("DL1ABC", "H24")),    # dupe (every station once)
                    dr8, sr8)
assert (r["points"], r["mults"], r["score"]) == (5, 6, 30), r
assert [r["per_qso"][i][2] for i in range(1, 7)] == [2, 0, 1, 1, 2, 0]   # new mults per QSO
assert m.qso_mults(sr8, "DM6EE", "h33", 3530000) == [("", "H33"), ("", "pfx:DM6")]
app8 = m.App(c8, m.Log(os.path.join(work, "cwa.db"))); app8.manual_freq["A"] = 3530000
assert c8["contest"]["contest"] == "CWA" and app8.mode() == "CW" and app8.default_rst() == "599"
app8.val["call"] = "DM6EE"; app8.val["exch"] = "H33"; assert app8.new_mult() == "H33, DM6"
app8.enter(); assert [t for row in app8.mult_panel(80) for t, _ in row][1:] == ["DM6", "H33"]
# old style points still work, and no country file is needed for them
assert m.qso_points(m.score_rules(m.load_config(work, "wsa")), "DL1A", "FM", 144300000) == 2
# radio detection, config.ini editing and running rigctld from llog
tools = os.path.join(work, "bin"); os.makedirs(tools)
def script(name, body):
    path = os.path.join(tools, name)
    open(path, "w").write("#!" + sys.executable + "\n" + body); os.chmod(path, 0o755)
    return path
script("rigctl", 'print("""' + """ Rig #  Mfg                    Model                   Version         Status      Macro
     1  Hamlib                 Dummy                   20240709.0      Stable      RIG_MODEL_DUMMY
  2044  Elecraft               KX2                     20240604.21     Stable      RIG_MODEL_KX2
  3085  Icom                   IC-705                  20240831.5      Stable      RIG_MODEL_IC705""" + '""")\n')
fake_daemon = script("rigctld", """import socket, sys, time
if "-m" in sys.argv and sys.argv[sys.argv.index("-m") + 1] == "666":
    print("rig_open: error = IO error"); sys.exit(2)
srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", int(sys.argv[sys.argv.index("-t") + 1]))); srv.listen()
while True:
    srv.accept()[0].close()
""")
models = m.rigctl_models(os.path.join(tools, "rigctl"))
assert models["KX2"] == "2044" and models["IC-705"] == "3085" and m.rigctl_models("/nonexistent") == {}
byid = os.path.join(work, "by-id"); os.makedirs(byid)
for name in ("usb-FTDI_FT232R_USB_UART_A1B2C3-if00-port0", "usb-Icom_Inc._IC-705_IC-705_12345678-if00",
             "usb-Icom_Inc._IC-705_IC-705_12345678-if02", "usb-Silicon_Labs_CP2102_0001-if00-port0",
             "usb-1a86_USB_Serial-if00-port0"):
    open(os.path.join(byid, name), "w").close()
def fake_probe(path):
    return (("KX2", 38400) if "FTDI" in path else (None, "Permission denied") if "CP2102" in path
            else (None, ""))
radios, notes = m.detect_radios(byid, fake_probe)
assert [(r["radio"], os.path.basename(r["device"]), r["speed"]) for r in radios] == [
    ("KX2", "usb-FTDI_FT232R_USB_UART_A1B2C3-if00-port0", "38400"),
    ("IC-705", "usb-Icom_Inc._IC-705_IC-705_12345678-if00", "")]
assert len(notes) == 1 and "Permission denied" in notes[0]
assert m.detect_radios(os.path.join(work, "missing"))[0] == []
assert m.probe_elecraft(os.path.join(work, "missing"))[0] is None       # no such port
assert m.elecraft_model("OM AP----TB-I01;") == "KX2" and m.elecraft_model("OM APF---TBXI02;") == "KX3"
assert m.elecraft_model("OM APXSDFfLV---;") == "K3" and m.elecraft_model("OM APXSDFfLVR--;") == "K3S"
ini = os.path.join(work, "edit.ini")
open(ini, "w").write("; my settings\n[station]\ncall = DA2IT\n\n[rig]\n# keep me\nhost = 127.0.0.1\nport = 4532\n\n[cluster]\nhost = x\n")
m.set_ini_values(ini, "rig", {"port": "4533", "rigctld_radio": "KX2"})
assert open(ini).read() == ("; my settings\n[station]\ncall = DA2IT\n\n[rig]\n# keep me\nhost = 127.0.0.1\n"
                            "port = 4533\nrigctld_radio = KX2\n\n[cluster]\nhost = x\n")
m.set_ini_values(ini, "backup", {"keep": "50"}); assert open(ini).read().endswith("[cluster]\nhost = x\n[backup]\nkeep = 50\n")
free = socket.socket(); free.bind(("127.0.0.1", 0)); free_port = free.getsockname()[1]; free.close()
cdir = os.path.join(work, "rcfg"); os.makedirs(os.path.join(cdir, "contests"))
open(os.path.join(cdir, "config.ini"), "w").write(
    f"[station]\ncall = DA2IT\n[rig]\nhost = 127.0.0.1\nport = {free_port}\nrigctld_command = {fake_daemon}\n")
open(os.path.join(cdir, "contests", "t.ini"), "w").write("[contest]\ncontest = T\n")
appr = m.App(m.load_config(cdir, "t"), m.Log(os.path.join(work, "r.db")))
assert appr.rigctld.state() == "not running" and "not set up" in appr.rigctld.describe()
assert "No radio set up yet" in appr.rigctld.start()
real_detect = m.detect_radios
m.detect_radios = lambda: (radios, notes)                       # two radios: ask which one
appr.menus = [appr.main_menu()]; appr.menu_key("r")
assert appr.menus[-1]["title"] == "Radio" and appr.menus[-1]["info"][1] == "rigctld: not running"
appr.menu_key("d"); assert appr.menus[-1]["title"] == "Which radio?" and len(appr.menus[-1]["items"]) == 2
appr.menu_key("1")
saved = configparser.ConfigParser(); saved.read(os.path.join(cdir, "config.ini"))
assert (saved["rig"]["rigctld_radio"], saved["rig"]["rigctld_model"], saved["rig"]["rigctld_speed"]) == ("KX2", "2044", "38400")
assert saved["rig"]["rigctld_device"].endswith("usb-FTDI_FT232R_USB_UART_A1B2C3-if00-port0") and not appr.menus
assert "started" in appr.rigctld.start() and appr.rigctld.ours() and appr.rigctld.reachable()
assert open(appr.rigctld.log_path).readline().split()[1:5] == ["-m", "2044", "-r", saved["rig"]["rigctld_device"]]
assert appr.rigctld.state() == "running (started by llog)" and "already running" in appr.rigctld.start()
appr.menus = [appr.radio_menu()]; appr.menu_key("d"); assert "Stop rigctld first" in appr.msg
assert appr.rigctld.stop() == "rigctld stopped." and not appr.rigctld.reachable()
appr.cfg["rig"]["rigctld_model"] = "666"; assert "stopped at once: rig_open: error = IO error" in appr.rigctld.start()
appr.menus = [appr.radio_menu()]; appr.menu_key("o")
saved.read(os.path.join(cdir, "config.ini")); assert saved["rig"]["rigctld_autostart"] == "yes"
m.detect_radios = lambda: ([], ["ttyUSB0: Permission denied"]); appr.menus = [appr.radio_menu()]; appr.menu_key("d")
assert "dialout" in appr.msg
# automatic start: no scan while the saved radio works, a scan only when it is missing
scans = []
m.detect_radios = lambda: (scans.append(1), ([radios[0]], []))[1]
appr.cfg["rig"]["rigctld_model"] = "2044"; appr.autostart()
assert appr.rigctld.ours() and not scans and "started" in appr.msg
appr.rigctld.stop()
appr.cfg["rig"]["rigctld_device"] = os.path.join(byid, "usb-unplugged-if00")   # other radio now
appr.autostart()
assert scans == [1] and appr.rigctld.ours() and "started (KX2" in appr.msg
assert appr.cfg["rig"]["rigctld_device"] == radios[0]["device"]
appr.rigctld.stop()
appr.cfg["rig"]["rigctld_device"] = ""; m.detect_radios = lambda: (radios, [])   # two radios
appr.autostart(); assert appr.menus[-1]["title"] == "Which radio?" and not appr.rigctld.ours()
appr.menu_key("1"); assert appr.rigctld.ours() and not appr.menus
appr.rigctld.stop()
appr.cfg["rig"]["rigctld_device"] = ""; m.detect_radios = lambda: ([], [])
appr.autostart(); assert "No radio found by scanning either" in appr.msg and not appr.rigctld.ours()
m.detect_radios = real_detect
# periodic backup
bdir = os.path.join(work, "bk"); c9 = m.load_config(work, "wsa"); c9["backup"]["folder"] = bdir
c9["backup"]["keep"] = "2"
app9 = m.App(c9, m.Log(os.path.join(work, "b.db"))); app9.manual_freq["A"] = 144300000
assert app9.auto_backup(force=True) is None                  # nothing changed yet
app9.val["call"] = "DL1AA"; app9.enter()
assert app9.auto_backup() is None                            # 5 minutes not over
app9.backup_at -= 301; first = app9.auto_backup()
assert first and sqlite3.connect(first).execute("SELECT call FROM qso").fetchall() == [("DL1AA",)]
app9.backup_at -= 301; assert app9.auto_backup() is None     # unchanged: no new copy
for call in ("DL1BB", "DL1CC"):
    time.sleep(1.1); app9.val["call"] = call; app9.enter(); assert app9.auto_backup(force=True)
assert len(os.listdir(bdir)) == 2 and os.path.basename(first) not in os.listdir(bdir)   # keep = 2
time.sleep(1.1); before = set(os.listdir(bdir)); app9.menus = [app9.main_menu()]; app9.menu_key("b")
assert app9.msg.startswith("Backup written: ") and len(os.listdir(bdir)) == 2
assert set(os.listdir(bdir)) != before and app9.auto_backup(force=True) is None
# running number in the sent exchange
assert m.next_exchange("N99", ["N99"]) == "N99"
assert m.next_exchange("#", []) == "001" and m.next_exchange("#", [], start=100) == "100"
assert m.next_exchange("#", ["001", "002", "N99"]) == "003"
assert m.next_exchange("#", ["001", "003"]) == "004"            # 002 deleted: no repeat
assert m.next_exchange("# N99", ["001 N99", "002 N99", "17"]) == "003 N99"
assert m.next_exchange("N99-#", ["N99-999"]) == "N99-1000"
c4 = m.load_config(work, "wsa"); c4["contest"]["exchange_sent"] = "# N99"
app4 = m.App(c4, m.Log(os.path.join(work, "s.db"))); app4.manual_freq["A"] = 144300000
for call in ("DL1AA", "DL1BB"):
    app4.val["call"] = call; app4.enter()
assert [q["exch_s"] for q in app4.log.rows()] == ["001 N99", "002 N99"]
assert app4.exch_sent() == "003 N99"
# locators and distance
assert m.is_locator("jo31") and m.is_locator("JO31AA") and m.is_locator("JO31AA55")
assert not m.is_locator("JO3") and not m.is_locator("ZZ99") and not m.is_locator("")
lat, lon = m.locator_position("JO31AA")
assert abs(lat - 51.0208) < 0.001 and abs(lon - 6.0417) < 0.001
assert m.distance_km("JO31AA", "JO31AA") == 0 and m.distance_km("JO31AA", "") is None
assert 138 <= m.distance_km("JO31AA", "JO41AA") <= 142       # 2 degrees east at 51N
assert m.bearing_deg("JO31AA", "JO41AA") in (89, 90) and m.bearing_deg("JO41AA", "JO31AA") in (270, 271)
assert m.bearing_deg("JO31AA", "JO32AA") == 0 and m.bearing_deg("JO32AA", "JO31AA") == 180
assert m.bearing_deg("JO31AA", "JO31AA") is None and m.bearing_deg("JO31AA", "") is None
assert m.paths(105) == "SP 105° / LP 285°" and m.paths(296) == "SP 296° / LP 116°"
assert 285 <= m.bearing_deg("JO31TV", "FN42NX") <= 300       # Germany to New England
assert m.great_circle(51.0, 6.0, 37.53, -91.67)[1] in range(295, 310)   # to mid USA
assert sqlite3.connect(db).execute(
    "SELECT grid FROM qso WHERE call='DK2XYZ'").fetchone()[0] == "JO40"
adif = open(os.path.join(work, "t.adi")).read()
assert "<MY_GRIDSQUARE:6>JO31AA" in adif and "<GRIDSQUARE:4>JO40" in adif
assert f"<DISTANCE:3>{m.distance_km('JO31AA', 'JO40')}" in adif
# locator pre-fill from the QRZ lookup
tcfg = m.load_config(work, "wsa")
app2 = m.App(tcfg, m.Log(os.path.join(work, "l.db")))
app2.qrz.cache["DL5ABC"] = {"name": "A", "qth": "", "country": "", "grid": "JO40ab"}
app2.val["call"] = "DL5ABC"; app2.sync_prefill()
assert app2.val["loc"] == "JO40AB" and app2.my_loc == "JO31AA"
app2.val["call"] = "DL5ABD"; app2.sync_prefill(); assert app2.val["loc"] == ""
app2.val["call"] = "DL5ABC"; app2.sync_prefill(); app2.focus = 4
for _ in range(6): app2.entry_key("\x7f")                     # user empties the field
app2.sync_prefill(); assert app2.val["loc"] == ""             # stays empty
for ch in "jn58": app2.entry_key(ch)
app2.sync_prefill(); assert app2.val["loc"] == "JN58"         # typed: not overwritten
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
    c = m.load_config(work, os.path.join(EX, name))
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
assert b"Detect radio and save settings" in txt and b"not set up yet" in txt
assert b"Newest" in txt and b"K1ABC" in txt and b"2153 CQ TEST" in txt
assert b"Band map 40m" in txt and b"stored" in txt and state["cluster_login"].startswith(b"DA2IT-1\r\n")
cl = m.Cluster("", 0, "DA2IT")
cl.feed("DX de DL1AAA:     14025.0  K1ABC        CQ TEST                        2153Z")
cl.feed("DX de DL2BBB-#:    7030.5  DK9ZZ        cq                             2154Z")
cl.feed("DX de DL1AAA:     14027.3  K1ABC        QSY                            2156Z")
cl.feed("To ALL de DL1AAA: hello"); cl.feed("DX de garbage")
assert [(x["call"], x["freq_hz"], x["time"]) for x in cl.spots("20m")] == \
    [("K1ABC", 14027300, "2156")] and len(cl.spots("40m")) == 1 and not cl.spots("2m")
cl._spots[("K1ABC", "20m")]["seen"] -= 31 * 60; assert not cl.spots("20m")   # expired
cl.call = "DA2IT-1"; cl._sent = (time.time(), "K9XX")
cl.feed("DA2IT-1 de TEST-2 22-Nov-2026 2200Z dxspider >"); assert cl.reply == ""
cl.feed("DX de DA2IT:      14030.0  K9XX         tnx                            2201Z")
assert cl.reply == "Cluster accepted the spot for K9XX."
cl.reply = ""; cl.feed("To ALL de DL1AAA: unrelated chatter"); assert cl.reply == ""
now_utc = datetime.now(timezone.utc)
cl.feed(f"  14010.0  W1AW        {now_utc:%d-%b-%Y %H%M}Z  sh/dx line                 <DL4DDD>")
cl.feed("  14011.0  W2OLD       01-Jan-2020 1200Z  long gone                  <DL4DDD>")
assert {x["call"] for x in cl.spots("20m")} == {"W1AW", "K9XX"}   # W2OLD expired
app = m.App.__new__(m.App)                      # panel logic without a terminal
c = m.load_config(work, os.path.join(EX, "wna.ini"))
app.scoring = m.score_rules(c); app.freq = lambda: V
app.score = lambda: {"seen": {("2m", "WN"), ("70cm", "DVN"), ("2m", "N01")}}
panel = app.mult_panel(30)
flat = [item for row in panel for item in row]
assert flat[0] == ("Mults 2m (2 worked):", False), flat
assert ("WN", True) in flat and ("DVN", False) in flat and len(panel) > 1
assert flat[-1] == ("N01", True) and len(flat) == 10      # 8 named + worked N01
app.freq = lambda: 0                            # no rig, no frequency typed yet
flat = [item for row in app.mult_panel(200) for item in row]
assert flat[0] == ("Mults all bands (3 worked):", False), flat
assert ("WN", True) in flat and ("DVN", True) in flat and ("N01", True) in flat
app.freq = lambda: V
print("ALL OK")
