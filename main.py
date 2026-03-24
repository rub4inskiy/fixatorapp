"""
main.py — ESP Line Logger (FastAPI)

Запуск:
    pip install -r requirements.txt
    python main.py

    # з демо-ESP:
    python main.py --demo

Відкрий браузер: http://localhost:8000
"""

import argparse
import asyncio
import json
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

BASE_DIR = Path(__file__).parent
from datetime import datetime, timezone

import uvicorn
from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi import Request

from database import Database
from tcp_server import TCPServer

# ── Глобальні об'єкти ──────────────────────────────────────────────────────
db         = Database()
tcp_server = None

# ── WebSocket менеджер (розсилає події всім відкритим вкладкам) ────────────
class WSManager:
    def __init__(self):
        self._clients: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._clients.append(ws)

    def disconnect(self, ws: WebSocket):
        self._clients.discard(ws) if hasattr(self._clients, 'discard') else None
        if ws in self._clients:
            self._clients.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in self._clients:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

ws_manager = WSManager()


# ── Lifespan (запуск/зупинка TCP при старті FastAPI) ──────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global tcp_server
    loop = asyncio.get_event_loop()

    def on_data(data: dict):
        db.insert_event(data)
        # Надсилаємо подію у WebSocket з основного event loop
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(ws_manager.broadcast(data), loop)

    def on_log(msg: str):
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(
                ws_manager.broadcast({"event_type": "__log__", "msg": msg}), loop
            )

    tcp_server = TCPServer(on_data=on_data, on_log=on_log)
    tcp_server.start()
    print(f"[App] TCP сервер запущено на порту {tcp_server.port}")

    yield  # ← додаток працює

    tcp_server.stop()
    print("[App] Зупинено.")


# ── FastAPI ────────────────────────────────────────────────────────────────
app = FastAPI(title="ESP Line Logger", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ── Сторінки ───────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/sim", response_class=HTMLResponse)
async def simulator(request: Request):
    return templates.TemplateResponse("simulator.html", {"request": request})


# ── REST API ───────────────────────────────────────────────────────────────
@app.get("/api/events")
async def get_events(limit: int = 200, device: str = None, type: str = None):
    return db.fetch_line_events(limit=limit, device_id=device, event_type=type)


@app.get("/api/cycles")
async def get_cycles(limit: int = 100, device: str = None, from_ts: int | None = None, to_ts: int | None = None):
    return db.fetch_cycles(device_id=device, limit=limit, from_ts=from_ts, to_ts=to_ts)


@app.get("/api/speeds")
async def get_speeds(device: str, from_ts: int, to_ts: int):
    """Get speed data points for chart (from heartbeats and cycles)."""
    if not device:
        raise HTTPException(status_code=400, detail="device is required")
    if from_ts is None or to_ts is None:
        raise HTTPException(status_code=400, detail="from_ts and to_ts are required")
    
    speeds = db.fetch_speed_data(device_id=device, from_ts=from_ts, to_ts=to_ts)
    return {"speeds": speeds}


@app.get("/api/devices")
async def get_devices():
    return db.fetch_devices()


@app.get("/api/stats")
async def get_stats(device: str = None):
    return db.fetch_stats(device_id=device)


@app.get("/api/status")
async def get_status():
    return {
        "tcp_running": tcp_server.running if tcp_server else False,
        "tcp_port":    tcp_server.port    if tcp_server else None,
        "db_count":    db.count(),
        "devices":     db.fetch_devices(),
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/api/tcp/start")
async def tcp_start():
    if tcp_server and not tcp_server.running:
        tcp_server.start()
    return {"ok": True}


@app.post("/api/tcp/stop")
async def tcp_stop():
    if tcp_server and tcp_server.running:
        tcp_server.stop()
    return {"ok": True}


def _to_int(v):
    try:
        if v is None:
            return None
        return int(v)
    except Exception:
        return None


def _to_float(v):
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


@app.post("/api/sim/send")
async def sim_send(payload: dict = Body(...)):
    """
    Простий симулятор подій для дебагу.

    Очікувані поля (мінімум):
    - event_type (str) або type
    - device_id (str) або device
    - ts (unix seconds) (можна не передавати)
    """
    event_type = payload.get("event_type") or payload.get("type") or "unknown"
    device_id = payload.get("device_id") or payload.get("device") or "sim-device"
    ts = _to_int(payload.get("ts"))
    if ts is None:
        ts = int(time.time())

    data = {
        "device_id": device_id,
        "event_type": event_type,
        "ts": ts,
        "cycle": _to_int(payload.get("cycle")),
        "dur": _to_float(payload.get("dur")),
        "speed": _to_float(payload.get("speed")),
        "buffered": bool(payload.get("buffered", False)),
        "uptime": _to_int(payload.get("uptime")),
        "rssi": _to_int(payload.get("rssi")),
        "buf": _to_int(payload.get("buf")),
        "version": payload.get("version"),
        "buf_after_reboot": _to_int(payload.get("buf_after_reboot")),
        # Для WS/логів
        "raw": payload.get("raw") or json.dumps(payload, ensure_ascii=False),
    }

    # Persist + broadcast як для TCP events
    db.insert_event(data)
    await ws_manager.broadcast(data)
    return {"ok": True}


@app.get("/api/timeline")
async def get_timeline(device: str = None, from_ts: int = None, to_ts: int = None):
    if not device:
        raise HTTPException(status_code=400, detail="device is required")
    if from_ts is None or to_ts is None:
        raise HTTPException(status_code=400, detail="from_ts and to_ts are required")
    return {"intervals": db.fetch_timeline(device_id=device, from_ts=from_ts, to_ts=to_ts)}


@app.post("/api/downtime/set")
async def downtime_set(payload: dict = Body(...)):
    device_id = payload.get("device_id")
    start_ts = _to_int(payload.get("start_ts"))
    stop_ts = _to_int(payload.get("stop_ts"))
    reason = payload.get("reason")
    comment = payload.get("comment")

    if not device_id:
        raise HTTPException(status_code=400, detail="device_id is required")
    if start_ts is None or stop_ts is None:
        raise HTTPException(status_code=400, detail="start_ts and stop_ts are required")
    if stop_ts <= start_ts:
        raise HTTPException(status_code=400, detail="stop_ts must be greater than start_ts")

    db.set_downtime(
        device_id=device_id,
        start_ts=start_ts,
        stop_ts=stop_ts,
        reason=str(reason) if reason is not None else None,
        comment=str(comment) if comment is not None else None,
    )
    await ws_manager.broadcast({
        "event_type": "__log__",
        "msg": f"[DOWNTIME] saved {device_id} {start_ts}->{stop_ts} reason={reason}"
    })
    return {"ok": True}


# ── WebSocket ──────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()  # тримаємо з'єднання живим
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)


# ── Entry point ────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo",    action="store_true", help="Запустити демо-ESP")
    parser.add_argument("--port",    type=int, default=8000, help="HTTP порт")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if args.demo:
        from demo_esp import run_demo_esp
        threading.Thread(target=run_demo_esp, daemon=True).start()
        print("[App] Демо-ESP запущено.")

    if not args.no_browser:
        def _open():
            time.sleep(1.5)
            webbrowser.open(f"http://localhost:{args.port}")
        threading.Thread(target=_open, daemon=True).start()

    uvicorn.run("main:app", host="0.0.0.0", port=args.port, reload=False)


if __name__ == "__main__":
    main()