# Sterowanie

## Uruchomienie
1. `pip install -r requirements.txt` (aiohttp, pyserial)
2. `python serwer.py` – otworzy się przeglądarka z adresem http://127.0.0.1:8765
3. Podłącz Alicata kablem USB–RS232.

## Jak program znajduje urządzenia
Co 3 sekundy serwer sprawdza listę portów COM w systemie (pyserial: `serial.tools.list_ports`).
Na każdym nowym porcie:
1. pyta po kolei `A\r`, `B\r` … `Z\r` (zwykły odczyt, nic nie ustawia). Kto odpowie ramką Alicata, ten jest. Najpierw 19200 baud, potem 9600.
   Kilka Alicatów na jednym kablu (RS-485) wychodzi z tego samo: każdy ma inny Unit ID.
2. od każdego znalezionego Alicata czyta dane producenta (`<ID>??M*`): model, numer seryjny, wersję oprogramowania.
3. z nazwy modelu wyciąga zakres (np. `MC-1SLPM-D-…` = 1 SLPM = 1000 sccm).
4. dopisuje go do ekranu. Nazwa, zakres, jednostka i limit bezpieczeństwa są zapamiętywane w `konfiguracja.json` pod numerem seryjnym,
   więc po przełożeniu kabla na inny port (inny numer COM) te same ustawienia i nastawy wracają.

Wyjęcie kabla: Alicat robi się szary („rozłączony”), regulacja jest wyłączona, a po podłączeniu (nawet pod innym COM) wraca sam.
„Szukaj ponownie” sprawdza wszystkie porty od nowa, także te, na których już coś jest (np. gdy dopiero dołożysz drugi Alicat na tym samym kablu).
Port zajęty przez inny program (np. terminal) jest zgłaszany pod paskiem połączenia.

Uwaga: skaner wysyła tekst `A\r`…`Z\r` do KAŻDEGO portu COM. Jeśli do komputera jest podłączone inne urządzenie z portem szeregowym
(np. zasilacz magnetronu), wyłącz je ze skanowania w `konfiguracja.json`: `"ignoruj_porty": ["COM9"]`
albo ogranicz szukanie do wybranych portów: `"tylko_porty": ["COM3", "COM6"]`.

## Wykres ogólny i zapis pomiarów
Wykres (dla każdego zaznaczonego Alicata osobna linia, nastawa przerywana) zaczyna się od zera przy pierwszym początku przepływu: gdy klikniesz „Start regulacji” albo „Otwórz zawór”,
albo gdy zmierzony przepływ przekroczy 0,5 % zakresu (np. gaz otwarty z panelu Alicata). Potem rośnie cały czas, także po „Stop”
i „Zamknij zawór”. Równolegle zapisuje się plik CSV w katalogu `pomiary/` (np. `A_20261119_235533.csv`).
- „Zapisz…”: otwiera okno z wyborem formatu (CSV do Excela albo obraz PNG wykresu) i nazwą pliku, potem zwykłe okno „Zapisz jako” (wybór folderu) w Chrome / Edge.
  Zapisuje to, co widać na wykresie (CSV ma kolumnę „alicat”), także po zmianie zakresów X i Y (CSV zawiera wtedy tylko wiersze z wybranego zakresu).
  Dodatkowo program na bieżąco zapisuje kopię zapasową w katalogu `pomiary/`.
- „Wyczyść”: kasuje wykres, zamyka plik CSV i czeka na NOWY początek przepływu (przepływ musi najpierw spaść poniżej 0,5 %, albo klikasz Start / Otwórz zawór).
- „Odśwież”: kończy ten pomiar i od razu zaczyna nowy wykres od zera (nowy plik CSV).
- „Zakres X / Y” ustawia przedział osi, puste = automatycznie. Do 50000 punktów (przy 0,5 s ok. 7 godzin).

## Z czego składa się program
| Plik | Zadanie |
|---|---|
| `alicat_driver.py` | rozmowa z Alicatem przez port szeregowy (jeden obiekt Port na COM, kilka Unit ID) |
| `skaner.py` | lista portów COM, szukanie Unit ID, odczyt modelu / numeru seryjnego / zakresu |
| `awarie.py` | scenariusze awarii komunikacji, progi, teksty komunikatów |
| `sterowanie.py` | obliczenia trybów skokowa / liniowa / PWM, wszystko w % zakresu |
| `system.py` | wyszukiwanie urządzeń, pętle regulacji, zapis JSON i CSV, obsługa wiadomości |
| `serwer.py` | serwer WebSocket `/ws` i strona WWW |
| `web/index.html` | cały interfejs |
| `konfiguracja.json` | zapamiętane ustawienia Alicatów oraz opcje szukania |
| `warunki.json` | nastawy trybów (tworzy się i zapisuje sam) |


## Symulator (bez sprzętu)
Jedna komenda: `python symulator.py` (uruchamia udawane Alicaty i serwer, otwiera się jedna karta przeglądarki, Ctrl+C zatrzymuje wszystko). Samo `python serwer.py` szuka prawdziwych Alicatów na portach COM.
Symulator (`symulator.py`, `sym_alicaty.py`) udaje dwa Alicaty (A: 1000 sccm, B: 5000 sccm) na adresie `socket://127.0.0.1:9100`
tym samym protokołem co prawdziwe urządzenie. Serwer znajduje je tym samym skanerem co prawdziwe (adres jest w `porty_dodatkowe`).
Zasilacza nie symuluje: U, I i moc maksymalną wpisujesz ręcznie w oknie „Zasilacz” i zaznaczasz „Używaj tych wartości jako zasilacza”.
Serwer z symulatorem używa osobnych plików: `konfiguracja_symulator.json` i `warunki_symulator.json`.

## Tryby (program liczy w % zakresu, wpisywać można w jednostkach urządzenia)
- skokowy: cel_pct – utrzymuj stałą wartość; wolna korekta całkująca (max ±5 %) dociąga zmierzony przepływ do celu, o ile nadają dane z zasilacza.
- liniowy: y = a·x + b, x = moc zasilacza [%], y = przepływ [%].
- PWM: przepływ przełącza się między poziomem wysokim i niskim (okres, wypełnienie).
Nastawa jest ograniczona do limitu bezpieczeństwa, a najmniejsza nastawa to 0,01 % zakresu (0,1 sccm przy zakresie 1000 sccm): przepływ to 0 albo co najmniej 0,01 %. Producent gwarantuje regulację od 0,5 % zakresu i dokładność ±0,6 % odczytu lub ±0,1 % zakresu (1 sccm przy 1000 sccm), więc nastawy poniżej 0,5 % są mało dokładne. Próg zmieniasz w `sterowanie.py` (`MIN_PCT`).
Nad trybami przełącznik „Wpisuj przepływ w: jednostkach / % zakresu”. W jednostkach (np. sccm) wpisujesz dokładną wartość przepływu (skokowy: wartość do utrzymania; PWM: wartość wysoka i niska; liniowy: y = a·x + b, gdzie a to sccm na 1 % mocy, a b to sccm przy mocy 0). Przeliczenie na % robi interfejs osobno dla każdego Alicata według jego zakresu, a pliki JSON nadal trzymają procenty.

## Zabezpieczenia
Zmiana trybu, Stop i zamknięcie programu: setpoint 0 + zawór zamknięty. 5 błędów komunikacji z rzędu: regulacja wyłączona, urządzenie oznaczone jako rozłączone.
Zmiana zakresu urządzenia zatrzymuje jego regulację (procenty zmieniają wtedy znaczenie).
