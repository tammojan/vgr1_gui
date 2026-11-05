#!/usr/bin/env python3
"""
Relay spectra published by `vrt_spectrum --pub-port` to browsers (websocket), and serve the page.

    vrt_spectrum ... --pub-port 50500 > /dev/null
    python3 vgr_relay.py --zmq tcp://localhost:50500 --http-port 8090
    open http://localhost:8090/            (or add it as an OBS browser source)

Each websocket message is binary:
    uint32 LE  header length H
    H bytes    JSON header from vrt_spectrum (padded with spaces to a multiple of 4)
    float32[N] averaged spectrum (linear power)
    float32[N] instantaneous spectrum (linear power)

Slow clients only ever get the newest spectrum; nothing queues up.

Requires: pyzmq, aiohttp
"""

import argparse
import asyncio
import json
import pathlib
import struct
import time

import zmq
import zmq.asyncio
from aiohttp import web, WSMsgType

HERE = pathlib.Path(__file__).resolve().parent


class Hub:
    def __init__(self):
        self.clients = {}          # ws -> asyncio.Event
        self.latest = None         # newest packed message
        self.last_rx = 0.0
        self.count = 0

    def publish(self, msg):
        self.latest = msg
        self.last_rx = time.time()
        self.count += 1
        for ev in self.clients.values():
            ev.set()


async def zmq_loop(hub, address):
    ctx = zmq.asyncio.Context()
    sock = ctx.socket(zmq.SUB)
    sock.setsockopt(zmq.RCVHWM, 4)
    sock.setsockopt(zmq.SUBSCRIBE, b"")
    sock.connect(address)
    print(f"subscribed to {address}")
    while True:
        parts = await sock.recv_multipart()
        if len(parts) != 3:
            continue
        hdr, avg, inst = parts
        try:
            json.loads(hdr)
        except ValueError:
            continue
        hdr = hdr + b" " * (-len(hdr) % 4)
        hub.publish(struct.pack("<I", len(hdr)) + hdr + avg + inst)


async def ws_handler(request):
    hub = request.app["hub"]
    ws = web.WebSocketResponse(heartbeat=10, max_msg_size=0)
    await ws.prepare(request)
    ev = asyncio.Event()
    if hub.latest is not None:
        ev.set()
    hub.clients[ws] = ev
    print(f"client connected ({len(hub.clients)}): {request.remote}")

    async def reader():  # only to notice disconnects
        async for m in ws:
            if m.type == WSMsgType.ERROR:
                break

    rtask = asyncio.create_task(reader())
    try:
        while not ws.closed and not rtask.done():
            try:
                await asyncio.wait_for(ev.wait(), timeout=2)
            except asyncio.TimeoutError:
                continue
            ev.clear()
            await ws.send_bytes(hub.latest)
    except (ConnectionResetError, RuntimeError):
        pass
    finally:
        rtask.cancel()
        hub.clients.pop(ws, None)
        print(f"client disconnected ({len(hub.clients)})")
    return ws


async def index(request):
    return web.FileResponse(request.app["html"], headers={"Cache-Control": "no-store"})


async def status(request):
    hub = request.app["hub"]
    return web.json_response({"messages": hub.count, "clients": len(hub.clients),
                              "seconds_since_data": time.time() - hub.last_rx if hub.count else None})


async def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--zmq", default="tcp://localhost:50500", help="vrt_spectrum --pub-port address")
    ap.add_argument("--http-port", type=int, default=8090)
    ap.add_argument("--bind", default="127.0.0.1", help="use 0.0.0.0 to allow other machines")
    ap.add_argument("--html", default=str(HERE / "index.html"))
    a = ap.parse_args()

    hub = Hub()
    app = web.Application()
    app["hub"], app["html"] = hub, a.html
    app.add_routes([web.get("/", index), web.get("/ws", ws_handler), web.get("/status", status)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, a.bind, a.http_port).start()
    print(f"serving http://{'localhost' if a.bind == '127.0.0.1' else a.bind}:{a.http_port}/")
    await zmq_loop(hub, a.zmq)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
