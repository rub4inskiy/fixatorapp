"""
tcp_server.py — Asyncio TCP сервер для прийому даних від ESP8266
"""

import asyncio
import json
import threading
from typing import Callable

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 5555


class TCPServer:
    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 on_data: Callable[[dict], None] = None,
                 on_log:  Callable[[str],  None] = None):
        self.host    = host
        self.port    = port
        self.on_data = on_data or (lambda d: None)
        self.on_log  = on_log  or print
        self.running = False
        self._loop:   asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def start(self):
        if self.running:
            return
        self._loop   = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True, name="TCPServer")
        self._thread.start()

    def stop(self):
        if self._loop and self.running:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self.running = False

    def _run(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as e:
            self.on_log(f"[TCP] Помилка: {e}")

    async def _serve(self):
        server = await asyncio.start_server(self._handle, self.host, self.port)
        self.running = True
        addr = server.sockets[0].getsockname()
        self.on_log(f"[TCP] Слухає на {addr[0]}:{addr[1]}")
        try:
            async with server:
                await server.serve_forever()
        finally:
            self.running = False
            server.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        self.on_log(f"[TCP] Підключено: {peer[0]}:{peer[1]}")
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                await self._process(line.decode("utf-8", errors="replace").strip(), peer)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()
            self.on_log(f"[TCP] Відключено: {peer[0]}")

    async def _process(self, text: str, peer):
        if not text:
            return
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            self.on_log(f"[TCP] Невалідний JSON: {text!r}")
            return

        event_type = data.get("type", "unknown")
        device_id  = str(data.get("device", peer[0]))

        parsed = {
            "device_id":        device_id,
            "event_type":       event_type,
            "ts":               _to_int(data.get("ts")),
            "cycle":            _to_int(data.get("cycle")),
            "dur":              _to_float(data.get("dur")),
            "speed":            _to_float(data.get("speed")),
            "buffered":         bool(data.get("buffered", False)),
            "uptime":           _to_int(data.get("uptime")),
            "buf":              _to_int(data.get("buf")),
            "rssi":             _to_int(data.get("rssi")),
            "buf_after_reboot": _to_int(data.get("buf_after_reboot")),
            "version":          data.get("version"),
            "raw":              text,
        }
        self.on_log(f"[TCP] [{event_type}] {device_id}"
                    + (f" dur={parsed['dur']}s" if parsed["dur"] else "")
                    + (f" speed={parsed['speed']}" if parsed["speed"] and event_type == "speed_update" else ""))
        self.on_data(parsed)


def _to_int(v):
    try:    return int(v)
    except: return None

def _to_float(v):
    try:    return float(v)
    except: return None
