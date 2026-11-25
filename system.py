"""
system.py - rdzeń serwera: wyszukiwanie urządzeń, pętle regulacji, pliki JSON, obsługa wiadomości.
"""
import asyncio
import copy
import csv
import json
import logging
import math
import os
import re
import time
from collections import deque

import skaner
import awarie
from alicat_driver import Alicat, AlicatBlad, MenedzerPortow, Port, PortBlad
from sterowanie import (TRYBY, Korekta, nowe_warunki, oblicz_nastawe,
                        popraw_warunki, uzupelnij_warunki)

log = logging.getLogger("system")

PROG_POMIARU_PCT = 0.5
OKRES = 0.5
OKRES_PWM = 0.2
OKRES_ROZLACZONY = 2.0
HISTORIA = 50000
ZYWE = 1200
HISTORIA_DLA_KLIENTA = 6000
ZASILACZ_TIMEOUT = 3.0
SKAN_OKRES = 3.0
SKAN_PUSTE_OKRES = 8.0

DOMYSLNY_ZAKRES = 1000.0
DOMYSLNY_LIMIT = 50.0

KOLUMNY_CSV = ["t", "cisnienie", "temperatura", "przeplyw_obj", "przeplyw_mas",
               "setpoint", "przeplyw_pct", "setpoint_pct", "cel_pct", "totalizator", "gaz"]

DOMYSLNA_KONFIGURACJA = {"ustawienia": {}, "porty_dodatkowe": [], "ignoruj_porty": [], "tylko_porty": []}


def wczytaj_json(plik, domyslne):
    try:
        with open(plik, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return domyslne
    except json.JSONDecodeError as e:
        raise ValueError(f"Plik {plik} jest uszkodzony: {e}")


def zapisz_json(plik, dane):
    tymczasowy = plik + ".tmp"
    with open(tymczasowy, "w", encoding="utf-8") as f:
        json.dump(dane, f, ensure_ascii=False, indent=2)
    os.replace(tymczasowy, plik)


def wczytaj_konfiguracje(plik):
    surowa = wczytaj_json(plik, {})
    konfig = {k: (list(v) if isinstance(v, list) else dict(v)) for k, v in DOMYSLNA_KONFIGURACJA.items()}
    for k in ("ustawienia",):
        if isinstance(surowa.get(k), dict):
            konfig[k] = {i: dict(v) for i, v in surowa[k].items() if isinstance(v, dict)}
    for k in ("porty_dodatkowe", "ignoruj_porty", "tylko_porty"):
        if isinstance(surowa.get(k), list):
            konfig[k] = [str(p) for p in surowa[k]]
    for stare in surowa.get("urzadzenia", []) or []:
        try:
            dane = waliduj_urzadzenie(stare)
        except ValueError:
            continue
        konfig["ustawienia"].setdefault(stare.get("id") or dane["id"], {
            k: dane[k] for k in ("nazwa", "full_scale", "jednostka", "max_pct")})
        if "://" in dane["port"] and dane["port"] not in konfig["porty_dodatkowe"]:
            konfig["porty_dodatkowe"].append(dane["port"])
    return konfig


def waliduj_urzadzenie(d):
    port = str(d.get("port", "")).strip()
    unit = str(d.get("unit", "A")).strip().upper()
    if not port:
        raise ValueError("Podaj port (np. COM7)")
    if len(unit) != 1 or not unit.isalpha():
        raise ValueError("Unit ID to jedna litera (A-Z)")
    try:
        zakres = float(d.get("full_scale"))
        baudrate = int(d.get("baudrate", 19200))
        max_pct = float(d.get("max_pct", 100))
    except (TypeError, ValueError):
        raise ValueError("Zakres, baudrate i limit muszą być liczbami")
    if zakres <= 0:
        raise ValueError("Zakres musi być większy od 0")
    if not 0 < max_pct <= 100:
        raise ValueError("Limit musi być w przedziale 0...100 %")
    return {
        "id": f"{port}_{unit}",
        "nazwa": str(d.get("nazwa") or unit).strip()[:20],
        "port": port,
        "baudrate": baudrate,
        "unit": unit,
        "full_scale": zakres,
        "jednostka": str(d.get("jednostka") or "sccm").strip()[:10],
        "max_pct": max_pct,
    }


def waliduj_ustawienia(d, stare):
    wynik = {}
    if "nazwa" in d:
        nazwa = str(d["nazwa"]).strip()[:20]
        if not nazwa:
            raise ValueError("Nazwa nie może być pusta")
        wynik["nazwa"] = nazwa
    if "full_scale" in d:
        try:
            zakres = float(d["full_scale"])
        except (TypeError, ValueError):
            raise ValueError("Zakres musi być liczbą")
        if not math.isfinite(zakres) or zakres <= 0:
            raise ValueError("Zakres musi być większy od 0")
        wynik["full_scale"] = zakres
    if "jednostka" in d:
        jednostka = str(d["jednostka"]).strip()[:10]
        if not jednostka:
            raise ValueError("Jednostka nie może być pusta")
        wynik["jednostka"] = jednostka
    if "max_pct" in d:
        try:
            limit = float(d["max_pct"])
        except (TypeError, ValueError):
            raise ValueError("Limit musi być liczbą")
        if not math.isfinite(limit) or not 0 < limit <= 100:
            raise ValueError("Limit musi być w przedziale 0...100 %")
        wynik["max_pct"] = limit
    return wynik


def id_urzadzenia(port, unit, info):
    sn = re.sub(r"[^A-Za-z0-9_-]", "", (info or {}).get("numer_seryjny") or "")
    return f"SN{sn}" if sn else f"{port}_{unit}"


def odchudz(lista, maks):
    if len(lista) <= maks:
        return list(lista)
    krok = math.ceil(len(lista) / maks)
    wynik = lista[::krok]
    if wynik[-1] is not lista[-1]:
        wynik.append(lista[-1])
    return wynik


class Urzadzenie:

    def __init__(self, konfig, port):
        self.konfig = konfig
        self.id = konfig["id"]
        self.alicat = Alicat(port, konfig["unit"])
        self.blokada = asyncio.Lock()
        self.historia = deque(maxlen=HISTORIA)
        self.zywe = deque(maxlen=ZYWE)
        self.korekta = Korekta(max_pct=float(konfig.get("korekta_max_pct", 5.0)))
        self.polaczony = True
        self.aktywny = False
        self.stosowane = None
        self.nastawa_zadana = 0.0
        self.ograniczony = False
        self.t_start = 0.0
        self.t_poprz = None
        self.bledy = 0
        self.monitor = awarie.Monitor()
        self.awarie_kody = []
        self.zadanie = None
        self.nagrywa = False
        self.uzbrojony = True
        self.plik_csv = None
        self.zapis_csv = None
        self.plik_nazwa = None


class System:
    def __init__(self, plik_konfiguracji="konfiguracja.json",
                 plik_warunkow="warunki.json", wysylaj=None, katalog_pomiarow="pomiary",
                 lista_portow=None, nowy_port=None, skan_okres=None):
        self.plik_konfiguracji = plik_konfiguracji
        self.plik_warunkow = plik_warunkow
        self.katalog_pomiarow = katalog_pomiarow
        self.wysylaj = wysylaj
        self.porty = MenedzerPortow()
        self.lista_portow = lista_portow or skaner.lista_portow
        self.nowy_port = nowy_port or Port
        self.skan_okres = SKAN_OKRES if skan_okres is None else skan_okres
        self.konfig = {k: (list(v) if isinstance(v, list) else dict(v))
                       for k, v in DOMYSLNA_KONFIGURACJA.items()}
        self.urzadzenia = {}
        self.warunki = {"tryb": None, "tryby": {}, "urzadzenia": {}, "limit_sumy": {"wlacz": False, "max": None}}
        self.zasilacz_moc = None
        self.zasilacz_t = None
        self.zasilacz_dane = {}
        self._zasilacz_widoczny = False
        self.zadanie_zasilacza = None
        self.zadanie_skanera = None
        self.skanowanie = False
        self.bledy_portow = {}
        self._puste = {}
        self._skan_blokada = asyncio.Lock()
        self._skan_zdarzenie = asyncio.Event()

    def zasilacz_swiezy(self):
        return (self.zasilacz_t is not None
                and time.monotonic() - self.zasilacz_t <= ZASILACZ_TIMEOUT)

    def stan_zasilacza(self):
        swiezy = self.zasilacz_swiezy()
        return {"typ": "zasilacz", "polaczony": swiezy,
                "moc_pct": self.zasilacz_moc if swiezy else None,
                **(self.zasilacz_dane if swiezy else {})}

    async def _petla_zasilacza(self):
        while True:
            await asyncio.sleep(0.5)
            swiezy = self.zasilacz_swiezy()
            if swiezy != self._zasilacz_widoczny:
                self._zasilacz_widoczny = swiezy
                await self._rozglos(self.stan_zasilacza())

    async def _zasilacz(self, msg):
        def liczba(nazwa):
            w = msg.get(nazwa)
            if w is None or w == "":
                return None
            try:
                w = float(w)
            except (TypeError, ValueError):
                raise ValueError(f"{nazwa}: to nie jest liczba")
            if w != w or w in (float("inf"), float("-inf")):
                raise ValueError(f"{nazwa}: to nie jest liczba")
            return w
        moc, u, i, p_max = liczba("moc_pct"), liczba("napiecie"), liczba("prad"), liczba("p_max")
        if moc is None:
            if u is None or i is None or p_max is None:
                raise ValueError("zasilacz: podaj moc_pct albo napiecie, prad i p_max")
            if u < 0 or i < 0:
                raise ValueError("napiecie i prad nie mogą być ujemne")
            if p_max <= 0:
                raise ValueError("p_max musi być większe od 0")
            moc = min(100.0, u * i / p_max * 100.0)
        if not 0 <= moc <= 100:
            raise ValueError("moc_pct musi być w przedziale 0...100")
        dane = {"napiecie": u, "prad": i, "p_max": p_max,
                "p_w": (u * i if u is not None and i is not None else (moc / 100 * p_max if p_max else None)),
                "zrodlo": str(msg.get("zrodlo") or "")[:30]}
        zmiana = (not self.zasilacz_swiezy()) or moc != self.zasilacz_moc or dane != self.zasilacz_dane
        self.zasilacz_moc = moc
        self.zasilacz_dane = dane
        self.zasilacz_t = time.monotonic()
        if zmiana:
            self._zasilacz_widoczny = True
            await self._rozglos(self.stan_zasilacza())

    def tryb_u(self, u):
        return self.warunki["tryby"].get(u.id)

    async def start(self):
        self.konfig = wczytaj_konfiguracje(self.plik_konfiguracji)
        zapisane = wczytaj_json(self.plik_warunkow, {"tryb": None, "urzadzenia": {}})
        self.warunki["tryb"] = zapisane.get("tryb") if zapisane.get("tryb") in TRYBY else None
        zapisane_tryby = zapisane.get("tryby") or {}
        for id_, w in (zapisane.get("urzadzenia") or {}).items():
            self.warunki["urzadzenia"][id_] = uzupelnij_warunki(w)
            tryb = zapisane_tryby.get(id_)
            self.warunki["tryby"][id_] = tryb if tryb in TRYBY else self.warunki["tryb"]
        ls = zapisane.get("limit_sumy") or {}
        try:
            maks = float(ls["max"]) if ls.get("max") is not None else None
        except (TypeError, ValueError):
            maks = None
        self.warunki["limit_sumy"] = {"wlacz": bool(ls.get("wlacz")) and bool(maks and maks > 0), "max": maks}
        self.zapisz_konfiguracje()
        self.zapisz_warunki()
        self.zadanie_zasilacza = asyncio.create_task(self._petla_zasilacza())
        self.zadanie_skanera = asyncio.create_task(self._petla_skanera())

    async def zatrzymaj(self):
        for zadanie in (self.zadanie_skanera, self.zadanie_zasilacza):
            if zadanie:
                zadanie.cancel()
        await asyncio.gather(*[z for z in (self.zadanie_skanera, self.zadanie_zasilacza) if z],
                             return_exceptions=True)
        for u in self.urzadzenia.values():
            self._koniec_zapisu(u)
        for u in self.urzadzenia.values():
            if u.zadanie:
                u.zadanie.cancel()
        await asyncio.gather(*[u.zadanie for u in self.urzadzenia.values() if u.zadanie],
                             return_exceptions=True)
        for u in self.urzadzenia.values():
            if not u.polaczony:
                continue
            try:
                await asyncio.to_thread(u.alicat.stop)
            except AlicatBlad as e:
                log.warning("Nie udało się zatrzymać %s: %s", u.konfig["nazwa"], e)
        self.porty.zamknij_wszystkie()

    def _dodaj(self, dane):
        dane.setdefault("korekta", True)
        dane.setdefault("korekta_max_pct", 5.0)
        port = self.porty.pobierz(dane["port"], dane["baudrate"])
        u = Urzadzenie(dane, port)
        self.urzadzenia[u.id] = u
        self.warunki["urzadzenia"].setdefault(u.id, nowe_warunki())
        self.warunki["tryby"].setdefault(u.id, self.warunki["tryb"])
        u.zadanie = asyncio.create_task(self._petla(u))
        return u

    def _filtruj_porty(self, nazwy):
        ignoruj = {p.upper() for p in self.konfig["ignoruj_porty"]}
        tylko = {p.upper() for p in self.konfig["tylko_porty"]}
        wynik = []
        for n in list(nazwy) + self.konfig["porty_dodatkowe"]:
            if n in wynik or n.upper() in ignoruj or (tylko and n.upper() not in tylko):
                continue
            wynik.append(n)
        return wynik

    def _sonduj_port(self, nazwa, znane):
        litery = [l for l in skaner.LITERY if l not in znane]
        istniejacy = self.porty.porty.get(nazwa) if hasattr(self.porty, "porty") else None
        if istniejacy is not None:
            bauds, tymczasowy = [getattr(istniejacy, "baudrate", skaner.BAUDRATY[0])], False
        else:
            bauds = skaner.BAUDRATY[:1] if "://" in nazwa else skaner.BAUDRATY
            tymczasowy = True
        try:
            for baud in bauds:
                port = self.nowy_port(nazwa, baud) if tymczasowy else istniejacy
                try:
                    znalezione = skaner.sonduj(port, litery)
                    if znalezione:
                        return [(baud, l, skaner.identyfikuj(port, l)) for l in znalezione], None
                finally:
                    if tymczasowy:
                        port.zamknij()
        except PortBlad as e:
            return [], str(e)
        return [], None

    async def _petla_skanera(self):
        while True:
            try:
                pelny = self._skan_zdarzenie.is_set()
                self._skan_zdarzenie.clear()
                await self._skan(pelny)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Błąd wyszukiwania urządzeń")
            try:
                await asyncio.wait_for(self._skan_zdarzenie.wait(), self.skan_okres)
            except asyncio.TimeoutError:
                pass

    async def _skan(self, pelny=False):
        async with self._skan_blokada:
            nazwy = self._filtruj_porty(await asyncio.to_thread(self.lista_portow))
            for u in list(self.urzadzenia.values()):
                if u.polaczony and u.konfig["port"] not in nazwy:
                    await self._rozlacz(u, "port zniknął z systemu")
            for nazwa in nazwy:
                zajete = [u for u in self.urzadzenia.values()
                          if u.polaczony and u.konfig["port"] == nazwa]
                if zajete and not pelny:
                    continue
                ostatnio = self._puste.get(nazwa)
                if (not zajete and not pelny and ostatnio is not None
                        and time.monotonic() - ostatnio < SKAN_PUSTE_OKRES):
                    continue
                await self._ustaw_skanowanie(True)
                znalezione, blad = await asyncio.to_thread(
                    self._sonduj_port, nazwa, {u.konfig["unit"] for u in zajete})
                if blad:
                    self.bledy_portow[nazwa] = blad
                else:
                    self.bledy_portow.pop(nazwa, None)
                if not znalezione and not zajete:
                    self._puste[nazwa] = time.monotonic()
                for baud, unit, info in znalezione:
                    self._puste.pop(nazwa, None)
                    await self._znaleziono(nazwa, baud, unit, info)
            for p in list(self.bledy_portow):
                if p not in nazwy:
                    del self.bledy_portow[p]
            await self._ustaw_skanowanie(False)

    async def _ustaw_skanowanie(self, trwa):
        self.skanowanie = trwa
        await self._rozglos(self.stan_skanowania())

    def stan_skanowania(self):
        return {"typ": "skanowanie", "trwa": self.skanowanie, "bledy": dict(self.bledy_portow)}

    async def _znaleziono(self, port, baud, unit, info):
        id_ = id_urzadzenia(port, unit, info)
        u = self.urzadzenia.get(id_)
        if u is not None:
            if u.polaczony and u.konfig["port"] == port and u.konfig["unit"] == unit:
                return
            async with u.blokada:
                u.konfig.update(port=port, baudrate=baud, unit=unit)
                u.alicat = Alicat(self.porty.pobierz(port, baud), unit)
                u.polaczony = True
                u.bledy = 0
                u.monitor.reset()
            await self._rozglos({"typ": "urzadzenie_stan", "id": u.id, "polaczony": True,
                                 "port": port, "baudrate": baud, "unit": unit})
            return
        ust = self.konfig["ustawienia"].get(id_, {})
        if ust.get("full_scale"):
            zakres, zrodlo = float(ust["full_scale"]), ust.get("zakres_zrodlo", "zapamiętany")
        elif info.get("zakres_sccm"):
            zakres, zrodlo = float(info["zakres_sccm"]), "model"
        else:
            zakres, zrodlo = DOMYSLNY_ZAKRES, "domyślny"
        nazwa = str(ust.get("nazwa") or unit)
        if any(x.konfig["nazwa"] == nazwa for x in self.urzadzenia.values()):
            nazwa = f"{nazwa} ({port})"[:20]
        dane = {
            "id": id_, "nazwa": nazwa, "port": port, "baudrate": baud, "unit": unit,
            "full_scale": zakres, "jednostka": str(ust.get("jednostka") or "sccm"),
            "max_pct": float(ust.get("max_pct") or DOMYSLNY_LIMIT),
            "model": info.get("model"), "numer_seryjny": info.get("numer_seryjny"),
            "firmware": info.get("firmware"), "zakres_zrodlo": zrodlo,
        }
        u = self._dodaj(dane)
        self.zapisz_konfiguracje()
        self.zapisz_warunki()
        await self._rozglos({"typ": "urzadzenie_dodane", "urzadzenie": self.opis(u)})
        log.info("Znaleziono Alicata %s na %s (Unit ID %s, %g %s)", dane["nazwa"], port, unit,
                 zakres, dane["jednostka"])

    async def _rozlacz(self, u, powod=""):
        if not u.polaczony:
            return
        u.polaczony = False
        if u.aktywny:
            u.aktywny = False
            u.korekta.reset()
            await self._rozglos({"typ": "regulacja", "id": u.id, "aktywny": False})
        await self._rozglos({"typ": "urzadzenie_stan", "id": u.id, "polaczony": False,
                             "port": u.konfig["port"], "powod": powod})
        log.warning("Alicat %s rozłączony: %s", u.konfig["nazwa"], powod)

    async def _poczatek_zapisu(self, u):
        if u.nagrywa:
            return
        u.historia.clear()
        u.nagrywa = True
        u.uzbrojony = False
        os.makedirs(self.katalog_pomiarow, exist_ok=True)
        nazwa = "".join(c if c.isalnum() else "_" for c in u.konfig["nazwa"])
        u.plik_nazwa = f"{nazwa}_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        if os.path.exists(os.path.join(self.katalog_pomiarow, u.plik_nazwa)):
            u.plik_nazwa = u.plik_nazwa[:-4] + f"_{int(time.monotonic() * 1000) % 1000}.csv"
        u.plik_csv = open(os.path.join(self.katalog_pomiarow, u.plik_nazwa),
                          "w", newline="", encoding="utf-8")
        u.zapis_csv = csv.writer(u.plik_csv)
        u.zapis_csv.writerow(KOLUMNY_CSV)
        await self._rozglos({"typ": "historia_reset", "id": u.id})
        await self._rozglos({"typ": "nagrywanie", "id": u.id, "nagrywa": True, "plik": u.plik_nazwa})

    def _koniec_zapisu(self, u):
        zmiana = u.nagrywa
        u.nagrywa = False
        if u.plik_csv:
            u.plik_csv.close()
        u.plik_csv = u.zapis_csv = None
        return zmiana

    async def _wyczysc(self, u):
        async with u.blokada:
            self._koniec_zapisu(u)
            u.historia.clear()
            u.uzbrojony = False
        await self._rozglos({"typ": "historia_reset", "id": u.id})
        await self._rozglos({"typ": "nagrywanie", "id": u.id, "nagrywa": False, "plik": None})

    async def _odswiez(self, u):
        async with u.blokada:
            self._koniec_zapisu(u)
            await self._poczatek_zapisu(u)

    def zapisz_konfiguracje(self):
        for u in self.urzadzenia.values():
            self.konfig["ustawienia"][u.id] = {
                k: u.konfig[k] for k in ("nazwa", "full_scale", "jednostka", "max_pct", "zakres_zrodlo")
                if k in u.konfig}
        zapisz_json(self.plik_konfiguracji, self.konfig)

    def zapisz_warunki(self):
        zapisz_json(self.plik_warunkow, self.warunki)

    def opis(self, u):
        opis = {k: v for k, v in u.konfig.items() if k not in ("korekta_max_pct", "korekta")}
        opis["tryb"] = self.tryb_u(u)
        opis["warunki"] = self.warunki["urzadzenia"][u.id]
        opis["aktywny"] = u.aktywny
        opis["nagrywa"] = u.nagrywa
        opis["plik"] = u.plik_nazwa if u.nagrywa else None
        opis["polaczony"] = u.polaczony
        return opis

    def stan_poczatkowy(self):
        return {
            "typ": "stan_poczatkowy",
            "tryb": self.warunki["tryb"],
            "limit_sumy": self.warunki["limit_sumy"],
            "zasilacz": self.stan_zasilacza(),
            "skanowanie": self.stan_skanowania(),
            "urzadzenia": [self.opis(u) for u in self.urzadzenia.values()],
            "historia": {u.id: odchudz(list(u.historia), HISTORIA_DLA_KLIENTA)
                         for u in self.urzadzenia.values()},
            "zywe": {u.id: list(u.zywe)[-300:] for u in self.urzadzenia.values()},
        }

    async def _rozglos(self, dane):
        if self.wysylaj:
            await self.wysylaj(dane)

    async def _petla(self, u):
        while True:
            poczatek = time.monotonic()
            try:
                async with u.blokada:
                    pomiar = await self._cykl(u)
                await self._rozglos(pomiar)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Błąd w pętli %s", u.id)
            pwm = u.aktywny and self.tryb_u(u) == "pwm"
            okres = OKRES_PWM if pwm else (OKRES if u.polaczony else OKRES_ROZLACZONY)
            await asyncio.sleep(max(0.05, okres - (time.monotonic() - poczatek)))

    async def _awarie(self, u):
        lista = u.monitor.aktywne()
        kody = [a["kod"] for a in lista]
        if kody != u.awarie_kody:
            nowe = [a for a in lista if a["kod"] not in u.awarie_kody]
            for a in nowe:
                log.warning("AWARIA %s (%s): %s [%s]", a["kod"], u.konfig["nazwa"], a["tytul"], a["szczegoly"])
                await self._rozglos({"typ": "blad", "komunikat": f"{u.konfig['nazwa']}: {a['tytul']}"})
            if u.awarie_kody and not kody:
                log.info("Awarie ustąpiły (%s)", u.konfig["nazwa"])
            u.awarie_kody = kody
        return lista

    async def _cykl(self, u):
        zakres = u.konfig["full_scale"]
        cel_pct = None
        tryb = self.tryb_u(u)
        x_info = None
        korekta_on = False
        try:
            stan = await asyncio.to_thread(u.alicat.odczyt)
        except AlicatBlad as e:
            u.bledy += 1
            u.monitor.odczyt(e)
            lista = await self._awarie(u)
            if awarie.wylacza_regulacje(lista) and u.polaczony:
                if u.aktywny:
                    await self._rozglos({"typ": "blad", "komunikat":
                                         f"{u.konfig['nazwa']}: utrata komunikacji, regulacja wyłączona"})
                await self._rozlacz(u, str(e))
            return {"typ": "pomiar", "id": u.id, "dane": None, "flagi": [],
                    "blad": str(e), "bledy": u.bledy, "aktywny": u.aktywny, "polaczony": u.polaczony,
                    "awarie": lista}
        u.bledy = 0
        u.monitor.odczyt()
        if not u.polaczony:
            u.polaczony = True
            await self._rozglos({"typ": "urzadzenie_stan", "id": u.id, "polaczony": True,
                                 "port": u.konfig["port"]})
        zmierzone_pct = stan["przeplyw_mas"] / zakres * 100
        tolerancja = max(0.06, 0.0001 * zakres)
        u.monitor.porownaj_nastawe(stan["setpoint"], tolerancja)
        if not u.aktywny:
            u.monitor.bez_regulacji()
            u.nastawa_zadana = 0.0
            u.ograniczony = False
        else:
            teraz = time.monotonic()
            dt = teraz - u.t_poprz if u.t_poprz else OKRES
            u.t_poprz = teraz
            warunki = u.stosowane or self.warunki["urzadzenia"][u.id]
            swiezy = self.zasilacz_swiezy()
            if tryb == "liniowa":
                if swiezy:
                    x_info = {"pct": self.zasilacz_moc, "zrodlo": "zasilacz"}
                    warunki = {**warunki, "liniowa": {**warunki["liniowa"],
                                                      "x_pct": self.zasilacz_moc}}
                else:
                    x_info = {"pct": warunki["liniowa"]["x_pct"], "zrodlo": "reczne"}
            korekta_on = swiezy and u.konfig.get("korekta", True) and tryb != "pwm"
            cel_pct, nastawa_pct = oblicz_nastawe(
                tryb, warunki, teraz - u.t_start, zmierzone_pct, dt, u.korekta,
                u.konfig["max_pct"], korekta_aktywna=korekta_on)
            nastawa = nastawa_pct / 100 * zakres
            u.nastawa_zadana = nastawa
            nastawa = self._ogranicz_sume(u, nastawa)
            try:
                if "HLD" in stan["flagi"]:
                    await asyncio.to_thread(u.alicat.otworz_zawor)
                if abs(stan["setpoint"] - nastawa) > tolerancja:
                    odp = await asyncio.to_thread(u.alicat.ustaw_przeplyw, nastawa)
                    u.monitor.zapis(None, zadana=nastawa)
                    stan["setpoint"] = odp["setpoint"]
            except AlicatBlad as e:
                u.monitor.zapis(e, zadana=nastawa)
        lista = await self._awarie(u)

        dane = {
            "t": time.time(),
            "cisnienie": stan["cisnienie"],
            "temperatura": stan["temperatura"],
            "przeplyw_obj": stan["przeplyw_obj"],
            "przeplyw_mas": stan["przeplyw_mas"],
            "setpoint": stan["setpoint"],
            "przeplyw_pct": round(zmierzone_pct, 3),
            "setpoint_pct": round(stan["setpoint"] / zakres * 100, 3),
            "cel_pct": None if cel_pct is None else round(cel_pct, 3),
            "totalizator": stan.get("totalizator"),
            "gaz": stan["gaz"],
            "flagi": stan["flagi"],
        }
        u.zywe.append(dane)
        if not u.nagrywa:
            if zmierzone_pct < PROG_POMIARU_PCT:
                u.uzbrojony = True
            elif u.uzbrojony:
                await self._poczatek_zapisu(u)
        if u.nagrywa:
            u.historia.append(dane)
            u.zapis_csv.writerow([dane[k] for k in KOLUMNY_CSV])
            u.plik_csv.flush()
        return {"typ": "pomiar", "id": u.id, "dane": dane, "gaz": stan["gaz"],
                "flagi": stan["flagi"], "blad": None, "bledy": 0, "aktywny": u.aktywny,
                "polaczony": True, "awarie": lista, "x": x_info, "korekta": korekta_on if u.aktywny else None,
                "ograniczony": u.ograniczony}

    def _ogranicz_sume(self, u, nastawa):
        ls = self.warunki["limit_sumy"]
        u.ograniczony = False
        if not ls["wlacz"] or not ls["max"]:
            return nastawa
        suma = sum(x.nastawa_zadana for x in self.urzadzenia.values() if x.polaczony and x.aktywny)
        if suma <= ls["max"] or suma <= 0:
            return nastawa
        reszta, wartosci = ls["max"], sorted(x.nastawa_zadana for x in self.urzadzenia.values() if x.polaczony and x.aktywny)
        poziom = nastawa
        for i, x in enumerate(wartosci):
            ile = len(wartosci) - i
            if x * ile <= reszta:
                reszta -= x
            else:
                poziom = reszta / ile
                break
        u.ograniczony = nastawa > poziom
        return min(nastawa, poziom)

    async def _limit_sumy(self, msg):
        maks = msg.get("max")
        try:
            maks = None if maks in (None, "") else float(maks)
        except (TypeError, ValueError):
            raise ValueError("Limit sumy przepływu musi być liczbą")
        if maks is not None and not (maks > 0 and maks < 1e9):
            raise ValueError("Limit sumy przepływu musi być większy od 0")
        wlacz = bool(msg.get("wlacz"))
        if wlacz and maks is None:
            raise ValueError("Wpisz maksymalną sumę przepływu, żeby włączyć limit")
        self.warunki["limit_sumy"] = {"wlacz": wlacz, "max": maks}
        self.zapisz_warunki()
        await self._rozglos({"typ": "limit_sumy", **self.warunki["limit_sumy"]})

    async def obsluz(self, msg):
        typ = msg.get("typ")
        try:
            if typ == "zasilacz":
                return await self._zasilacz(msg)
            if typ == "limit_sumy":
                return await self._limit_sumy(msg)
            if typ == "ustaw_tryb":
                return await self._ustaw_tryb(msg)
            if typ == "ustaw_warunki":
                return await self._ustaw_warunki(msg)
            if typ == "ustaw_urzadzenie":
                return await self._ustaw_urzadzenie(msg)
            if typ == "start":
                return await self._start(msg)
            if typ == "stop":
                return await self._stop(msg)
            if typ == "zawor":
                return await self._zawor(msg)
            if typ == "wyczysc":
                return await self._wyczysc(self._u(msg))
            if typ == "odswiez":
                return await self._odswiez(self._u(msg))
            if typ == "skanuj":
                self._skan_zdarzenie.set()
                return None
            if typ == "dodaj_urzadzenie":
                return await self._dodaj_urzadzenie(msg)
            if typ == "usun_urzadzenie":
                return await self._usun_urzadzenie(msg)
            raise ValueError(f"Nieznany typ wiadomości: {typ}")
        except ValueError as e:
            return {"typ": "blad", "komunikat": str(e)}
        except AlicatBlad as e:
            return {"typ": "blad", "komunikat": f"Alicat: {e}"}

    def _u(self, msg):
        u = self.urzadzenia.get(msg.get("id"))
        if u is None:
            raise ValueError("Nieznane urządzenie")
        return u

    def _polaczony(self, msg):
        u = self._u(msg)
        if not u.polaczony:
            raise ValueError(f"Alicat {u.konfig['nazwa']} jest rozłączony")
        return u

    async def _ustaw_tryb(self, msg):
        tryb = msg.get("tryb")
        if tryb not in TRYBY:
            raise ValueError("Nieznany tryb pracy")
        pojedynczy = msg.get("id") is not None
        cele = [self._u(msg)] if pojedynczy else [u for u in self.urzadzenia.values() if not u.aktywny]
        bledy = []
        for u in cele:
            if self.tryb_u(u) != tryb:
                blad = await self._zatrzymaj_regulacje(u)
                if blad:
                    bledy.append(blad)
                self.warunki["tryby"][u.id] = tryb
                await self._rozglos({"typ": "tryb", "id": u.id, "tryb": tryb})
        if not pojedynczy:
            self.warunki["tryb"] = tryb
            await self._rozglos({"typ": "tryb", "tryb": tryb})
        self.zapisz_warunki()
        if bledy:
            return {"typ": "blad", "komunikat": "; ".join(bledy)}

    async def _ustaw_warunki(self, msg):
        u = self._u(msg)
        tryb = msg.get("tryb") or self.tryb_u(u)
        if tryb is None:
            raise ValueError("Najpierw wybierz tryb pracy")
        nowe = popraw_warunki(tryb, self.warunki["urzadzenia"][u.id].get(tryb, {}),
                              msg.get("dane") or {})
        self.warunki["urzadzenia"][u.id][tryb] = nowe
        self.zapisz_warunki()
        await self._rozglos({"typ": "warunki", "id": u.id, "tryb": tryb, "warunki": nowe})

    async def _ustaw_urzadzenie(self, msg):
        u = self._u(msg)
        zmiany = waliduj_ustawienia(msg.get("dane") or {}, u.konfig)
        blad = None
        if "full_scale" in zmiany and zmiany["full_scale"] != u.konfig["full_scale"]:
            if u.aktywny and u.polaczony:
                blad = await self._zatrzymaj_regulacje(u)
            u.konfig["zakres_zrodlo"] = "ręczny"
        if "jednostka" in zmiany and zmiany["jednostka"] != u.konfig["jednostka"] and u.aktywny:
            blad = await self._zatrzymaj_regulacje(u)
        u.konfig.update(zmiany)
        self.zapisz_konfiguracje()
        await self._rozglos({"typ": "urzadzenie_zmienione", "urzadzenie": self.opis(u)})
        if blad:
            return {"typ": "blad", "komunikat": blad}

    async def _start(self, msg):
        u = self._polaczony(msg)
        if self.tryb_u(u) is None:
            raise ValueError("Najpierw wybierz tryb pracy")
        async with u.blokada:
            await asyncio.to_thread(u.alicat.otworz_zawor)
            u.korekta.reset()
            u.stosowane = copy.deepcopy(self.warunki["urzadzenia"][u.id])
            u.t_start = time.monotonic()
            u.t_poprz = None
            u.bledy = 0
            u.aktywny = True
            await self._poczatek_zapisu(u)
        await self._rozglos({"typ": "regulacja", "id": u.id, "aktywny": True})

    async def _zatrzymaj_regulacje(self, u):
        async with u.blokada:
            u.aktywny = False
            u.korekta.reset()
            blad = None
            if u.polaczony:
                try:
                    await asyncio.to_thread(u.alicat.stop)
                except AlicatBlad as e:
                    blad = f"{u.konfig['nazwa']}: {e}"
        await self._rozglos({"typ": "regulacja", "id": u.id, "aktywny": False})
        return blad

    async def _stop(self, msg):
        blad = await self._zatrzymaj_regulacje(self._u(msg))
        if blad:
            return {"typ": "blad", "komunikat": blad}

    async def _zawor(self, msg):
        u = self._polaczony(msg)
        akcja = msg.get("akcja")
        if akcja not in ("otworz", "zamknij"):
            raise ValueError("Nieznana akcja zaworu")
        async with u.blokada:
            if akcja == "otworz":
                await asyncio.to_thread(u.alicat.otworz_zawor)
                await self._poczatek_zapisu(u)
            else:
                u.aktywny = False
                u.korekta.reset()
                await asyncio.to_thread(u.alicat.zamknij_zawor)
        if akcja == "zamknij":
            await self._rozglos({"typ": "regulacja", "id": u.id, "aktywny": False})

    async def _dodaj_urzadzenie(self, msg):
        dane = waliduj_urzadzenie(msg.get("urzadzenie") or {})
        if dane["id"] in self.urzadzenia:
            raise ValueError(f"Alicat {dane['unit']} na porcie {dane['port']} już jest w układzie")
        dane.update(model=None, numer_seryjny=None, firmware=None, zakres_zrodlo="ręczny")
        u = self._dodaj(dane)
        self.zapisz_konfiguracje()
        self.zapisz_warunki()
        await self._rozglos({"typ": "urzadzenie_dodane", "urzadzenie": self.opis(u)})

    async def _usun_urzadzenie(self, msg):
        u = self._u(msg)
        blad = await self._zatrzymaj_regulacje(u)
        self._koniec_zapisu(u)
        u.zadanie.cancel()
        await asyncio.gather(u.zadanie, return_exceptions=True)
        del self.urzadzenia[u.id]
        self.warunki["urzadzenia"].pop(u.id, None)
        self.warunki["tryby"].pop(u.id, None)
        self.konfig["ustawienia"].pop(u.id, None)
        self.zapisz_konfiguracje()
        self.zapisz_warunki()
        await self._rozglos({"typ": "urzadzenie_usuniete", "id": u.id})
        if blad:
            return {"typ": "blad", "komunikat": blad}
