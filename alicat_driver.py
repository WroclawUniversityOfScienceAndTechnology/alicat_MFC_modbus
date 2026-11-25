"""
alicat_driver.py - komunikacja z Alicatem przez port szeregowy.
"""
import threading

import serial


class AlicatBlad(Exception):
    pass


class PortBlad(AlicatBlad):
    pass


class BrakOdpowiedzi(AlicatBlad):
    pass


class Port:

    def __init__(self, nazwa, baudrate=19200, timeout=0.5):
        self.nazwa = nazwa
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser = None
        self.blokada = threading.Lock()

    def _otworz(self):
        if self.ser is not None and self.ser.is_open:
            return
        try:
            self.ser = serial.serial_for_url(
                self.nazwa,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.timeout,
            )
        except (serial.SerialException, OSError) as e:
            self.ser = None
            raise PortBlad(f"Nie można otworzyć {self.nazwa}: {e}") from e
        try:
            self.ser.rts = False
            self.ser.dtr = False
        except (AttributeError, NotImplementedError, OSError):
            pass

    def zamknij(self):
        with self.blokada:
            if self.ser is not None and self.ser.is_open:
                self.ser.close()
            self.ser = None

    def _na_czas(self, timeout):
        stary = self.ser.timeout
        if timeout is not None:
            self.ser.timeout = timeout
        return stary

    def _awaria_portu(self, e):
        try:
            self.ser.close()
        except Exception:
            pass
        self.ser = None
        raise PortBlad(f"Błąd portu {self.nazwa}: {e}") from e

    def wyslij(self, unit, komenda, timeout=None):
        with self.blokada:
            self._otworz()
            stary = self._na_czas(timeout)
            try:
                self.ser.reset_input_buffer()
                self.ser.write((unit + komenda + "\r").encode("ascii"))
                surowa = self.ser.read_until(b"\r")
            except (serial.SerialException, OSError) as e:
                self._awaria_portu(e)
            finally:
                if self.ser is not None:
                    self.ser.timeout = stary
        if not surowa.endswith(b"\r"):
            raise BrakOdpowiedzi(f"Brak odpowiedzi (timeout) na {unit}{komenda}")
        tekst = surowa.decode("ascii", errors="replace").strip()
        if not tekst:
            raise BrakOdpowiedzi("Pusta odpowiedź")
        return tekst

    def wyslij_wiele(self, unit, komenda, timeout=0.3, maks_linii=60):
        linie = []
        with self.blokada:
            self._otworz()
            stary = self._na_czas(timeout)
            try:
                self.ser.reset_input_buffer()
                self.ser.write((unit + komenda + "\r").encode("ascii"))
                while len(linie) < maks_linii:
                    surowa = self.ser.read_until(b"\r")
                    if not surowa.endswith(b"\r"):
                        break
                    tekst = surowa.decode("ascii", errors="replace").strip()
                    if tekst:
                        linie.append(tekst)
            except (serial.SerialException, OSError) as e:
                self._awaria_portu(e)
            finally:
                if self.ser is not None:
                    self.ser.timeout = stary
        return linie


class MenedzerPortow:

    def __init__(self):
        self.porty = {}

    def pobierz(self, nazwa, baudrate=19200):
        if nazwa not in self.porty:
            self.porty[nazwa] = Port(nazwa, baudrate)
        return self.porty[nazwa]

    def zamknij_wszystkie(self):
        for port in self.porty.values():
            port.zamknij()


def parsuj(tekst, unit):
    czesci = tekst.split()
    if len(czesci) < 7 or czesci[0] != unit:
        raise AlicatBlad(f"Nieprawidłowa ramka: {tekst!r}")
    liczby, slowa = [], []
    for c in czesci[1:]:
        try:
            liczby.append(float(c))
        except ValueError:
            slowa.append(c)
    if len(liczby) < 5:
        raise AlicatBlad(f"Za mało wartości w ramce: {tekst!r}")
    return {
        "cisnienie": liczby[0],
        "temperatura": liczby[1],
        "przeplyw_obj": liczby[2],
        "przeplyw_mas": liczby[3],
        "setpoint": liczby[4],
        "totalizator": liczby[5] if len(liczby) > 5 else None,
        "gaz": slowa[0] if slowa else "",
        "flagi": slowa[1:],
    }


class Alicat:

    def __init__(self, port, unit):
        self.port = port
        self.unit = unit

    def _komenda(self, komenda):
        return parsuj(self.port.wyslij(self.unit, komenda), self.unit)

    def odczyt(self):
        return self._komenda("")

    def ustaw_przeplyw(self, wartosc):
        return self._komenda(f"S {wartosc:.3f}")

    def zamknij_zawor(self):
        return self._komenda("HC")

    def otworz_zawor(self):
        return self._komenda("C")

    def stop(self):
        self.ustaw_przeplyw(0.0)
        return self.zamknij_zawor()
