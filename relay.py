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

Pacing: vrt_tuner sends 10000-sample packets (1 s at 10 kHz), so with --overlap
vrt_spectrum emits its spectra in bursts, one burst per packet. The relay releases
each spectrum at (its data timestamp + a constant delay), with the delay set to the
largest lag seen in the last few seconds. Updates are therefore evenly spaced, about
one packet late. Use --no-pace to forward immediately.

Slow clients only ever get the newest spectrum; nothing queues up.

Requires: pyzmq, aiohttp
"""

import argparse
import asyncio
import collections
import heapq
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


class Pacer:
    """Release messages at data time + max recent lag, so bursts come out evenly spaced."""

    WINDOW = 5.0      # s of arrivals over which the max lag is taken
    MARGIN = 0.02     # s
    MAX_QUEUE = 64    # e.g. faster-than-real-time replay: give up pacing rather than lag behind

    def __init__(self, hub):
        self.hub, self.heap, self.lags, self.seq = hub, [], collections.deque(), 0
        self.wake = asyncio.Event()

    def put(self, t_data, msg):
        now = time.monotonic()
        wall_lag = time.time() - t_data                    # data clock vs wall clock, any offset
        self.lags.append((now, wall_lag))
        while self.lags and self.lags[0][0] < now - self.WINDOW:
            self.lags.popleft()
        delay = max(l for _, l in self.lags) + self.MARGIN
        due = now + (delay - wall_lag)                     # = arrival + (max lag - own lag)
        self.seq += 1
        heapq.heappush(self.heap, (due, self.seq, msg))
        while len(self.heap) > self.MAX_QUEUE:
            self.hub.publish(heapq.heappop(self.heap)[2])
        self.wake.set()

    async def run(self):
        while True:
            if not self.heap:
                self.wake.clear()
                await self.wake.wait()
                continue
            due = self.heap[0][0]
            dt = due - time.monotonic()
            if dt > 0:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=dt)
                except asyncio.TimeoutError:
                    pass
                continue
            self.hub.publish(heapq.heappop(self.heap)[2])


async def zmq_loop(hub, address, pacer=None):
    ctx = zmq.asyncio.Context()
    sock = ctx.socket(zmq.SUB)
    sock.setsockopt(zmq.RCVHWM, 1000)
    sock.setsockopt(zmq.SUBSCRIBE, b"")
    sock.connect(address)
    print(f"subscribed to {address}")
    while True:
        parts = await sock.recv_multipart()
        if len(parts) != 3:
            continue
        hdr, avg, inst = parts
        try:
            t_data = float(json.loads(hdr)["t"])
        except (ValueError, KeyError, TypeError):
            continue
        hdr = hdr + b" " * (-len(hdr) % 4)
        msg = struct.pack("<I", len(hdr)) + hdr + avg + inst
        if pacer:
            pacer.put(t_data, msg)
        else:
            hub.publish(msg)


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
    ap.add_argument("--no-pace", action="store_true", help="forward spectra as they arrive (bursty)")
    a = ap.parse_args()

    hub = Hub()
    app = web.Application()
    app["hub"], app["html"] = hub, a.html
    app.add_routes([web.get("/", index), web.get("/ws", ws_handler), web.get("/status", status)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, a.bind, a.http_port).start()
    print(f"serving http://{'localhost' if a.bind == '127.0.0.1' else a.bind}:{a.http_port}/")
    pacer = None if a.no_pace else Pacer(hub)
    if pacer:
        asyncio.create_task(pacer.run())
    await zmq_loop(hub, a.zmq, pacer)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
