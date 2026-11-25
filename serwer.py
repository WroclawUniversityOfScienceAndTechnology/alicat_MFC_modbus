"""
serwer.py - serwer WebSocket i strona WWW.
Uruchomienie:  python serwer.py
Otwiera przeglądarkę pod adresem http://127.0.0.1:8765
"""
import asyncio
import json
import logging
import pathlib
import sys
import webbrowser

from aiohttp import WSMsgType, web

from system import System

HOST = "127.0.0.1"
PORT = 8765
KATALOG = pathlib.Path(__file__).parent

PLIK_KONFIGURACJI = sys.argv[1] if len(sys.argv) > 1 else "konfiguracja.json"

klienci = set()


async def rozglos(wiadomosc):
    tekst = json.dumps(wiadomosc)
    for ws in list(klienci):
        try:
            await ws.send_str(tekst)
        except Exception:
            klienci.discard(ws)


async def strona(request):
    tresc = (KATALOG / "web" / "index.html").read_bytes()
    return web.Response(body=tresc, content_type="text/html", charset="utf-8", headers={"Cache-Control": "no-store"})


async def websocket(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    system = request.app["system"]
    klienci.add(ws)
    await ws.send_str(json.dumps(system.stan_poczatkowy()))
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                odpowiedz = await system.obsluz(json.loads(msg.data))
            except Exception as e:
                logging.exception("Błąd obsługi wiadomości")
                odpowiedz = {"typ": "blad", "komunikat": f"Błąd serwera: {e}"}
            if odpowiedz:
                await ws.send_str(json.dumps(odpowiedz))
    finally:
        klienci.discard(ws)
    return ws


async def przy_starcie(app):
    app["system"] = System(
        plik_konfiguracji=str(KATALOG / PLIK_KONFIGURACJI),
        plik_warunkow=str(KATALOG / PLIK_KONFIGURACJI.replace("konfiguracja", "warunki")),
        wysylaj=rozglos,
    )
    await app["system"].start()
    asyncio.get_running_loop().call_later(1.0, webbrowser.open, f"http://{HOST}:{PORT}")


async def przy_zamknieciu(app):
    await app["system"].zatrzymaj()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = web.Application()
    app.router.add_get("/", strona)
    app.router.add_get("/ws", websocket)
    app.on_startup.append(przy_starcie)
    app.on_cleanup.append(przy_zamknieciu)
    web.run_app(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
