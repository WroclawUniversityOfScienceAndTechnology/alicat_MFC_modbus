"""
skaner.py - wyszukiwanie Alicatów na portach COM i odczyt ich danych.
"""
import logging
import re
import string

from alicat_driver import AlicatBlad, PortBlad, parsuj

log = logging.getLogger("skaner")

LITERY = string.ascii_uppercase
BAUDRATY = (19200, 9600)
TIMEOUT_SONDY = 0.15
TIMEOUT_INFO = 0.4
IGNORUJ_OPIS = ("bluetooth",)

JEDNOSTKI_NA_SCCM = {"SCCM": 1.0, "CCM": 1.0, "SLPM": 1000.0, "SLM": 1000.0, "LPM": 1000.0}


def lista_portow():
    try:
        from serial.tools import list_ports
    except ImportError:
        if not getattr(lista_portow, "ostrzezono", False):
            lista_portow.ostrzezono = True
            log.warning("Brak serial.tools.list_ports (pyserial): nie można wyszukać portów COM")
        return []
    nazwy = []
    for p in list_ports.comports():
        opis = f"{p.description or ''} {p.hwid or ''}".lower()
        if any(slowo in opis for slowo in IGNORUJ_OPIS):
            continue
        nazwy.append(p.device)
    return sorted(nazwy, key=klucz_portu)


def klucz_portu(nazwa):
    m = re.search(r"(\d+)\s*$", nazwa)
    return (re.sub(r"\d+\s*$", "", nazwa), int(m.group(1)) if m else 0)


def sonduj(port, litery=LITERY, timeout=TIMEOUT_SONDY):
    znalezione = []
    for litera in litery:
        try:
            parsuj(port.wyslij(litera, "", timeout=timeout), litera)
        except PortBlad:
            raise
        except AlicatBlad:
            continue
        znalezione.append(litera)
    return znalezione


def zakres_z_modelu(model):
    if not model:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(SCCM|CCM|SLPM|SLM|LPM)\b", model, re.IGNORECASE)
    if not m:
        m = re.search(r"(\d+(?:[.,]\d+)?)(SCCM|CCM|SLPM|SLM|LPM)", model, re.IGNORECASE)
    if not m:
        return None
    return float(m.group(1).replace(",", ".")) * JEDNOSTKI_NA_SCCM[m.group(2).upper()]


_ETYKIETY = (
    ("model", re.compile(r"^model(\s*(number|no\.?|nr))?\s*[:=]?\s*", re.IGNORECASE)),
    ("numer_seryjny", re.compile(r"^serial(\s*(number|no\.?|nr))?\s*[:=]?\s*", re.IGNORECASE)),
    ("firmware", re.compile(r"^(software|firmware)(\s*version)?\s*[:=]?\s*", re.IGNORECASE)),
)


def parsuj_info(linie):
    info = {"model": None, "numer_seryjny": None, "firmware": None,
            "zakres_sccm": None, "surowe": list(linie)}
    for linia in linie:
        tekst = re.sub(r"^\s*[A-Za-z]\s+M\d+\s+", "", linia).strip()
        for klucz, wzor in _ETYKIETY:
            if info[klucz] is None and wzor.match(tekst):
                wartosc = wzor.sub("", tekst).strip()
                if wartosc:
                    info[klucz] = wartosc
    if info["model"] is None:
        for linia in linie:
            m = re.search(r"\b[A-Z]{1,4}-\d+(?:[.,]\d+)?[A-Z]+[-/\w]*", linia)
            if m:
                info["model"] = m.group(0)
                break
    info["zakres_sccm"] = zakres_z_modelu(info["model"])
    return info


def identyfikuj(port, unit):
    try:
        linie = port.wyslij_wiele(unit, "??M*", timeout=TIMEOUT_INFO)
    except PortBlad:
        raise
    except AlicatBlad:
        linie = []
    info = parsuj_info(linie)
    log.info("Alicat %s: model=%s, nr seryjny=%s, firmware=%s (odpowiedź ??M*: %s)",
             unit, info["model"], info["numer_seryjny"], info["firmware"], linie)
    return info
