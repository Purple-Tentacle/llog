#!/usr/bin/env python3
"""A tiny local DX cluster for testing llog. Nothing leaves your computer.

    python3 tools/fake_cluster.py            # listens on 127.0.0.1:7300

Point llog at it in ~/.config/llog/config.ini:

    [cluster]
    host = 127.0.0.1
    port = 7300

It asks for a login, answers "sh/dx" with a few stored spots, sends a new
made-up spot every 20 seconds and accepts spots you send with Alt-S: these are
printed here and echoed back as a normal "DX de" line, as a real node does.
Log in as REFUSE to see how llog shows a refused spot.
"""
import argparse
import random
import socketserver
import threading
import time
from datetime import datetime, timezone

SAMPLES = [(14025.0, "K1ABC"), (14074.0, "JA1XYZ"), (7030.5, "DK9ZZ"),
           (7012.0, "DJ5ZZ"), (3555.0, "OK1AAA"), (21030.0, "PY2BBB"),
           (28025.0, "ZS6CCC"), (144300.0, "DL0WN"), (144320.0, "DF0MU"),
           (432200.0, "DL0EG"), (10118.0, "SM5DDD"), (18080.0, "EA8EEE")]
clients = []
lock = threading.Lock()


def utc(fmt):
    return datetime.now(timezone.utc).strftime(fmt)


def spot_line(spotter, khz, call, comment=""):
    return (f"DX de {spotter + ':':<10} {khz:>8.1f}  {call:<12} "
            f"{comment:<30.30} {utc('%H%M')}Z")


def broadcast(line):
    with lock:
        for client in list(clients):
            try:
                client.sendall((line + "\r\n").encode())
            except OSError:
                clients.remove(client)


class Session(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"Welcome to the llog test cluster\r\nlogin: ")
        login = self.rfile.readline().decode("latin-1").strip().upper()
        if not login:
            return
        print(f"login: {login}")
        self.wfile.write(f"Hello {login}\r\n".encode())
        with lock:
            clients.append(self.request)
        try:
            for raw in self.rfile:
                line = raw.decode("latin-1").strip()
                if not line:
                    continue
                print(f"{login} > {line}")
                words = line.split(None, 3)
                if line.lower().startswith("sh/dx"):
                    for khz, call in SAMPLES[:6]:
                        self.wfile.write(
                            f"{khz:>9.1f}  {call:<11} {utc('%d-%b-%Y %H%M')}Z  "
                            f"stored spot                 <DL0TEST>\r\n".encode())
                elif words[0].upper() == "DX" and len(words) >= 3:
                    if login == "REFUSE":
                        self.wfile.write(
                            b"Sorry, you are not registered to send spots\r\n")
                    else:
                        try:
                            khz = float(words[1])
                        except ValueError:
                            self.wfile.write(b"*** invalid frequency ***\r\n")
                            continue
                        comment = words[3] if len(words) > 3 else ""
                        broadcast(spot_line(login.split("-")[0], khz,
                                            words[2].upper(), comment))
                self.wfile.write(
                    f"{login} de TEST-1 {utc('%d-%b-%Y %H%M')}Z dxspider >\r\n"
                    .encode())
        finally:
            with lock:
                if self.request in clients:
                    clients.remove(self.request)
            print(f"logout: {login}")


def feeder(interval):
    while True:
        time.sleep(interval)
        khz, call = random.choice(SAMPLES)
        broadcast(spot_line("DL0TEST", khz, call, "llog test spot"))


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Local DX cluster for testing llog")
    ap.add_argument("--port", type=int, default=7300)
    ap.add_argument("--interval", type=int, default=20,
                    help="seconds between made-up spots")
    args = ap.parse_args()
    threading.Thread(target=feeder, args=(args.interval,), daemon=True).start()
    print(f"Test cluster on 127.0.0.1:{args.port} - Ctrl-C stops it")
    with Server(("127.0.0.1", args.port), Session) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
