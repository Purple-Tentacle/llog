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
for f in os.listdir(work):
    os.remove(os.path.join(work, f))
cfg = os.path.join(work, "config.ini")
open(cfg, "w").write(f"""[station]
call = DA2IT
exchange_sent = N99
[qrz]
username =
password =
[rig]
host = 127.0.0.1
port = {port}
[contest]
contest = WSA
email = test@example.org
specific = N99
""")

pid, fd = pty.fork()
if pid == 0:
    os.environ["TERM"] = "xterm"; os.environ["LINES"] = "30"; os.environ["COLUMNS"] = "120"
    os.execv(sys.executable, [sys.executable, APP, db, "--config", cfg])

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
send("7030.5\r", 1.2)                                         # QSY via logger
send("ssb\r", 1.2)                                            # mode via logger
send("dk2xyz\t"); send("57\t"); send("55\t"); send("nm\r")    # QSO 2 with RSTs
send("oops1x\r"); send("\x1b[19~"); send("y")                 # QSO 3, F8 delete
send("\x1bOQ"); send("\x1bOR"); send("\x1bOS")                            # F2, F3 exports
send("\x1b[21~", 1.0)                                         # F10 quit
os.waitpid(pid, 0)

rows = sqlite3.connect(db).execute(
    "SELECT call,freq_hz,mode,rst_s,rst_r,exch_s,exch_r FROM qso").fetchall()
print(rows)
assert rows == [("DL1ABC/P", 14060000, "CW", "599", "599", "N99", "O05"),
                ("DK2XYZ", 7030500, "SSB", "57", "55", "N99", "NM")], rows
assert "F 7030500" in state["cmds"] and "M LSB 0" in state["cmds"], state["cmds"][-20:]
for name in sorted(os.listdir(work)):
    if name.endswith((".adi", ".cbr", ".stf")):
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
print("ALL OK")
