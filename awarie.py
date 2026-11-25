"""
awarie.py - wszystkie scenariusze awarii komunikacji PC <-> Alicat w jednym miejscu.
"""
import logging
from collections import deque

from alicat_driver import BrakOdpowiedzi, PortBlad

log = logging.getLogger("awarie")

POZIOM_OSTRZEZENIE = "ostrzezenie"
POZIOM_BLAD = "blad"
POZIOM_KRYTYCZNY = "krytyczny"
_WAGA = {POZIOM_KRYTYCZNY: 0, POZIOM_BLAD: 1, POZIOM_OSTRZEZENIE: 2}

PROGI = {
    "odczyt_z_rzedu": 2,
    "odczyt_niestabilny": (3, 10),
    "rozlaczenie_z_rzedu": 5,
    "port_z_rzedu": 2,
    "zapis_z_rzedu": 2,
    "nastawa_rozna": 3,
}

WYLACZAJA_REGULACJE = {"BRAK_POLACZENIA"}

SCENARIUSZE = []


def scenariusz(kod, poziom, tytul, opis, wskazowka, tlumi=()):
    def dekorator(funkcja):
        SCENARIUSZE.append({"kod": kod, "poziom": poziom, "tytul": tytul, "opis": opis,
                            "wskazowka": wskazowka, "tlumi": tuple(tlumi), "warunek": funkcja})
        return funkcja
    return dekorator


class Monitor:
    def __init__(self, progi=None):
        self.progi = {**PROGI, **(progi or {})}
        self.aktywne_kody = []
        self.reset()

    def reset(self):
        self.odczyt_z_rzedu = 0
        self.odczyt_ostatnie = deque(maxlen=self.progi["odczyt_niestabilny"][1])
        self.cisza_z_rzedu = 0
        self.port_z_rzedu = 0
        self.zapis_z_rzedu = 0
        self.nastawa_rozna = 0
        self.zadana = None
        self.ostatni_blad = ""

    def odczyt(self, wyjatek=None):
        if wyjatek is None:
            self.odczyt_z_rzedu = 0
            self.cisza_z_rzedu = 0
            self.port_z_rzedu = 0
            self.odczyt_ostatnie.append(True)
            return
        self.odczyt_z_rzedu += 1
        self.odczyt_ostatnie.append(False)
        self.ostatni_blad = str(wyjatek)
        self.port_z_rzedu = self.port_z_rzedu + 1 if isinstance(wyjatek, PortBlad) else 0
        self.cisza_z_rzedu = self.cisza_z_rzedu + 1 if isinstance(wyjatek, (BrakOdpowiedzi, PortBlad)) else 0

    def zapis(self, wyjatek=None, zadana=None):
        if wyjatek is None:
            self.zapis_z_rzedu = 0
            self.zadana = zadana
        else:
            self.zapis_z_rzedu += 1
            self.ostatni_blad = str(wyjatek)

    def porownaj_nastawe(self, odczytana, tolerancja):
        if self.zadana is None:
            self.nastawa_rozna = 0
        elif abs(odczytana - self.zadana) > tolerancja:
            self.nastawa_rozna += 1
        else:
            self.nastawa_rozna = 0

    def bez_regulacji(self):
        self.zadana = None
        self.nastawa_rozna = 0
        self.zapis_z_rzedu = 0

    def nieudane_w_oknie(self):
        return sum(1 for ok in self.odczyt_ostatnie if not ok)

    def aktywne(self):
        wynik, stlumione = [], set()
        for sc in SCENARIUSZE:
            if sc["warunek"](self):
                wynik.append({"kod": sc["kod"], "poziom": sc["poziom"], "tytul": sc["tytul"],
                              "opis": sc["opis"], "wskazowka": sc["wskazowka"],
                              "szczegoly": self.ostatni_blad})
                stlumione.update(sc["tlumi"])
        wynik = [w for w in wynik if w["kod"] not in stlumione]
        wynik.sort(key=lambda w: _WAGA[w["poziom"]])
        self.aktywne_kody = [w["kod"] for w in wynik]
        return wynik


def wylacza_regulacje(awarie):
    return any(a["kod"] in WYLACZAJA_REGULACJE for a in awarie)


@scenariusz("BRAK_POLACZENIA", POZIOM_KRYTYCZNY,
            "Brak połączenia z Alicatem",
            "Komputer nie ma łączności z Alicatem w żadną stronę: port nie działa albo Alicat w ogóle nie odpowiada. "
            "Regulacja została wyłączona.",
            "Sprawdź kabel USB i RS232, zasilanie Alicata, czy port nie jest zajęty przez inny program "
            "i czy Unit ID oraz baudrate się zgadzają. Po przywróceniu łączności Alicat wróci sam.",
            tlumi=("BRAK_ODCZYTU", "NASTAWA_NIEPRZYJETA"))
def _brak_polaczenia(m):
    return (m.cisza_z_rzedu >= m.progi["rozlaczenie_z_rzedu"]
            or m.port_z_rzedu >= m.progi["port_z_rzedu"])


@scenariusz("BRAK_ODCZYTU", POZIOM_BLAD,
            "Brak odczytu z Alicata",
            "Komputer wysyła polecenia, ale nie dostaje poprawnej odpowiedzi z wartościami (krótka cisza albo uszkodzona ramka). "
            "Dane na ekranie są nieaktualne.",
            "Alicat może nadal utrzymywać ostatnią nastawę, więc nie zakładaj, że przepływ stoi. "
            "Sprawdź przewód odbiorczy (pin 3 przejściówki), zakłócenia i baudrate. Gdy cisza potrwa, program uzna to za brak połączenia.")
def _brak_odczytu(m):
    zle, okno = m.progi["odczyt_niestabilny"]
    return m.odczyt_z_rzedu >= m.progi["odczyt_z_rzedu"] or m.nieudane_w_oknie() >= zle


@scenariusz("NASTAWA_NIEPRZYJETA", POZIOM_BLAD,
            "Alicat nie przyjmuje nastawy",
            "Odczyt działa, ale nastawa w Alicacie jest inna niż wysłana z programu (albo wysłanie polecenia kończy się błędem).",
            "Sprawdź kabel nadawczy (pin 2 przejściówki), czy Alicat ma nastawę z portu szeregowego (nie z panelu ani z wejścia analogowego), "
            "czy ktoś nie zmienia nastawy ręcznie na panelu i czy Alicat nie jest zablokowany. Przepływ może być inny niż wpisany.")
def _nastawa_nieprzyjeta(m):
    return (m.odczyt_z_rzedu == 0
            and (m.zapis_z_rzedu >= m.progi["zapis_z_rzedu"] or m.nastawa_rozna >= m.progi["nastawa_rozna"]))
