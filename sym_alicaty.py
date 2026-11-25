"""
sym_alicaty.py - symulator Alicatów: udaje urządzenia na porcie szeregowym.
Mówi dokładnie tym samym protokołem tekstowym co prawdziwy Alicat (komendy zakończone \\r,
Unit ID jako pierwsza litera), ale przez gniazdo TCP. Biblioteka pyserial zna adresy
"socket://127.0.0.1:9100", więc serwer.py rozmawia z symulatorem tak samo jak z COM6.
Tylko biblioteka standardowa.
"""
import math
import random
import socketserver
import threading
import time


class SymulowanyAlicat:
    def __init__(self, unit, full_scale, gaz="Air", tau=1.0, szum=0.002,
                 zasilacz=None, wsp_mocy=-0.4):
        self.unit = unit
        self.full_scale = float(full_scale)
        self.gaz = gaz
        self.tau = tau
        self.szum = szum
        self.zasilacz = zasilacz
        self.wsp_mocy = wsp_mocy
        self.setpoint = 0.0
        self.hold = False
        self.przeplyw = 0.0
        self.licznik = 0.0
        self.blokada = threading.Lock()

    def wydajnosc(self):
        if self.zasilacz is None:
            return 1.0
        return max(0.0, 1.0 + self.wsp_mocy * (self.zasilacz.moc_pct() - 50.0) / 100.0)

    def krok(self, dt):
        with self.blokada:
            if self.hold or self.setpoint < 0.0001 * self.full_scale:
                cel = 0.0
            else:
                cel = min(self.setpoint * self.wydajnosc(), self.full_scale)
            self.przeplyw += (cel - self.przeplyw) * (1.0 - math.exp(-dt / self.tau))
            self.licznik += max(self.przeplyw, 0.0) / 60.0 * dt

    def ramka(self):
        with self.blokada:
            przeplyw = self.przeplyw * (1.0 + random.uniform(-self.szum, self.szum))
            flagi = " HLD" if self.hold else ""
            return (f"{self.unit} {14.74 + random.uniform(-0.02, 0.02):+07.2f} "
                    f"{25.3 + random.uniform(-0.05, 0.05):+07.2f} "
                    f"{przeplyw:+07.1f} {przeplyw:+07.1f} {self.setpoint:+07.1f} "
                    f"{int(self.licznik):+08d}    {self.gaz}{flagi}")

    def komenda(self, tekst):
        tekst = tekst.strip()
        if tekst == "":
            return self.ramka()
        if tekst.upper().startswith("S "):
            try:
                wartosc = float(tekst[2:])
            except ValueError:
                return "?"
            if 0.0 <= wartosc <= self.full_scale:
                with self.blokada:
                    self.setpoint = wartosc
            return self.ramka()
        if tekst.upper() == "HC":
            with self.blokada:
                self.hold = True
            return self.ramka()
        if tekst.upper() == "C":
            with self.blokada:
                self.hold = False
            return self.ramka()
        if tekst.upper() == "VE":
            return f"{self.unit} 10v23.0-R24 SYMULATOR"
        if tekst == "??M*":
            fs = self.full_scale
            model = f"MC-{fs / 1000:g}SLPM-D-SYM" if fs >= 1000 else f"MC-{fs:g}SCCM-D-SYM"
            linie = ["Alicat Scientific (SYMULATOR)", "Symulowany regulator przeplywu",
                     f"Model Number {model}", f"Serial Number SYM{ord(self.unit)}",
                     "Software Version 10v23.0-R24 SYM"]
            return "\r".join(f"{self.unit} M{i:02d} {t}" for i, t in enumerate(linie))
        return "?"


class Obsluga(socketserver.BaseRequestHandler):

    def handle(self):
        bufor = b""
        while True:
            try:
                dane = self.request.recv(1024)
            except OSError:
                return
            if not dane:
                return
            bufor += dane
            while b"\r" in bufor:
                linia, bufor = bufor.split(b"\r", 1)
                tekst = linia.decode("ascii", errors="replace").strip()
                if not tekst:
                    continue
                alicat = self.server.urzadzenia.get(tekst[0].upper())
                if alicat is None:
                    continue
                try:
                    self.request.sendall((alicat.komenda(tekst[1:]) + "\r").encode("ascii", errors="replace"))
                except OSError:
                    return


class SerwerAlicatow(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, adres, urzadzenia):
        super().__init__(adres, Obsluga)
        self.urzadzenia = {a.unit: a for a in urzadzenia}


def petla_fizyki(urzadzenia, stop, okres=0.1):
    poprzednio = time.monotonic()
    while not stop.is_set():
        time.sleep(okres)
        teraz = time.monotonic()
        for a in urzadzenia:
            a.krok(teraz - poprzednio)
        poprzednio = teraz
