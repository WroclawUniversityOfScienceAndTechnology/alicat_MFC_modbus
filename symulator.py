"""
symulator.py - udaje dwa Alicaty, żeby serwer.py działał bez sprzętu.
  1. Alicaty: serwer TCP na 127.0.0.1:9100 z protokołem Alicata (sym_alicaty.py), Unit ID A (1000 sccm) i B (5000 sccm),
"""
import logging
import pathlib
import subprocess
import sys
import threading

from sym_alicaty import SerwerAlicatow, SymulowanyAlicat, petla_fizyki

KATALOG = pathlib.Path(__file__).parent
HOST = "127.0.0.1"
PORT_ALICATOW = 9100

ALICATY = [
    {"unit": "A", "full_scale": 1000},
    {"unit": "B", "full_scale": 5000},
]


def main():
    logging.basicConfig(level=logging.INFO)
    alicaty = [SymulowanyAlicat(a["unit"], a["full_scale"]) for a in ALICATY]
    stop = threading.Event()

    tcp = SerwerAlicatow((HOST, PORT_ALICATOW), alicaty)
    threading.Thread(target=tcp.serve_forever, daemon=True).start()
    threading.Thread(target=petla_fizyki, args=(alicaty, stop), daemon=True).start()
    print(f"Alicaty (symulacja): socket://{HOST}:{PORT_ALICATOW}   Unit ID: "
          + ", ".join(a["unit"] for a in ALICATY))

    serwer = subprocess.Popen([sys.executable, "serwer.py", "konfiguracja_symulator.json"], cwd=KATALOG)
    try:
        serwer.wait()
        print("Serwer zakończył działanie, zatrzymuję symulator.")
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        tcp.shutdown()
        if serwer.poll() is None:
            serwer.terminate()
            try:
                serwer.wait(timeout=10)
            except subprocess.TimeoutExpired:
                serwer.kill()


if __name__ == "__main__":
    main()
