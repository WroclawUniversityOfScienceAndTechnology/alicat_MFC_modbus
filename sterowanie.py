"""
sterowanie.py - logika trybów pracy. Same obliczenia: bez portu, bez sieci.
"""
import copy
import math

TRYBY = ("skokowa", "liniowa", "pwm")

MIN_PCT = 0.01
POLA_PRZEPLYWU = ("cel_pct", "wysoki_pct", "niski_pct")

DOMYSLNE_WARUNKI = {
    "skokowa": {"cel_pct": 0.0},
    "liniowa": {"a": 1.0, "b": 0.0, "x_pct": 0.0},
    "pwm": {"okres_s": 10.0, "wypelnienie_pct": 50.0, "wysoki_pct": 50.0, "niski_pct": 0.0},
}

LIMITY = {
    "skokowa": {"cel_pct": (0, 100)},
    "liniowa": {"a": (-1000, 1000), "b": (-1000, 1000), "x_pct": (0, 100)},
    "pwm": {"okres_s": (1, 3600), "wypelnienie_pct": (0, 100),
            "wysoki_pct": (0, 100), "niski_pct": (0, 100)},
}


def nowe_warunki():
    return copy.deepcopy(DOMYSLNE_WARUNKI)


def uzupelnij_warunki(zapisane):
    wynik = nowe_warunki()
    for tryb in TRYBY:
        for klucz in wynik[tryb]:
            wartosc = (zapisane or {}).get(tryb, {}).get(klucz)
            if isinstance(wartosc, (int, float)) and math.isfinite(wartosc):
                wynik[tryb][klucz] = float(wartosc)
    return wynik


def popraw_warunki(tryb, stare, nowe):
    if tryb not in LIMITY:
        raise ValueError(f"Nieznany tryb: {tryb}")
    wynik = dict(stare)
    for klucz, wartosc in nowe.items():
        if klucz not in LIMITY[tryb]:
            raise ValueError(f"Nieznany parametr trybu {tryb}: {klucz}")
        try:
            liczba = float(wartosc)
        except (TypeError, ValueError):
            raise ValueError(f"{klucz}: to nie jest liczba")
        if not math.isfinite(liczba):
            raise ValueError(f"{klucz}: to nie jest liczba")
        dol, gora = LIMITY[tryb][klucz]
        if not dol <= liczba <= gora:
            raise ValueError(f"{klucz}: wartość {liczba:g} poza zakresem {dol}...{gora}")
        if klucz in POLA_PRZEPLYWU and 0 < liczba < MIN_PCT:
            raise ValueError(f"{klucz}: przepływ to 0 albo co najmniej {MIN_PCT:g} % zakresu (najmniejsza nastawa)")
        wynik[klucz] = liczba
    return wynik


def ogranicz(x, dol, gora):
    return max(dol, min(gora, x))


class Korekta:

    def __init__(self, ki=0.1, strefa_pct=0.2, max_pct=5.0):
        self.ki = ki
        self.strefa_pct = strefa_pct
        self.max_pct = max_pct
        self.calka = 0.0

    def reset(self):
        self.calka = 0.0

    def krok(self, cel_pct, zmierzone_pct, dt):
        if cel_pct <= 0:
            self.calka = 0.0
            return 0.0
        blad = cel_pct - zmierzone_pct
        if abs(blad) > self.strefa_pct:
            self.calka += self.ki * blad * dt
            self.calka = ogranicz(self.calka, -self.max_pct, self.max_pct)
        return self.calka


def oblicz_nastawe(tryb, warunki, t_od_startu, zmierzone_pct, dt, korekta, max_pct,
                   korekta_aktywna=True):
    w = warunki[tryb]
    if tryb == "skokowa":
        cel = w["cel_pct"]
    elif tryb == "liniowa":
        cel = w["a"] * w["x_pct"] + w["b"]
    elif tryb == "pwm":
        faza = (t_od_startu % w["okres_s"]) / w["okres_s"]
        cel = w["wysoki_pct"] if faza < w["wypelnienie_pct"] / 100 else w["niski_pct"]
    else:
        raise ValueError(f"Nieznany tryb: {tryb}")

    cel = ogranicz(cel, 0.0, max_pct)
    if cel < MIN_PCT:
        cel = 0.0
    if tryb == "pwm" or not korekta_aktywna:
        korekta.reset()
        nastawa = cel
    else:
        nastawa = cel + korekta.krok(cel, zmierzone_pct, dt)
    if cel > 0:
        nastawa = ogranicz(nastawa, MIN_PCT, max_pct)
    else:
        nastawa = 0.0
    return cel, nastawa
