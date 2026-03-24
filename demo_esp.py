"""
demo_esp.py — Симулятор ESP для тестування без реального залізa
"""

import json
import random
import socket
import time

from tcp_server import DEFAULT_PORT


def run_demo_esp(host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                 device_id: str = "esp-logger-scl"):
    cycle = 0
    while True:
        try:
            with socket.create_connection((host, port), timeout=5) as s:
                print(f"[DemoESP] Підключено до {host}:{port}")
                while True:
                    cycle += 1
                    ts_start = int(time.time())
                    _send(s, {
                        "type": "line_start", "device": device_id,
                        "ts": ts_start, "cycle": cycle, "buffered": False
                    })
                    # Реальна тривалість 30-300с, для демо ділимо на 20
                    real_dur = random.uniform(30, 300)
                    
                    # Симулюємо зміну швидкості протягом циклу
                    base_speed = 1.5 + random.uniform(0, 0.5)
                    num_updates = max(1, int(real_dur / (20 * 5)))  # кожні 5 секунд
                    
                    for i in range(num_updates):
                        time.sleep(real_dur / (20 * num_updates))
                        # Швидкість трохи змінюється
                        current_speed = base_speed + random.uniform(-0.2, 0.2)
                        _send(s, {
                            "type": "speed_update", "device": device_id,
                            "ts": int(time.time()), "speed": round(current_speed, 2),
                            "cycle": cycle
                        })
                    
                    time.sleep(real_dur / 20)
                    ts_stop = int(time.time())
                    _send(s, {
                        "type": "line_stop", "device": device_id,
                        "ts": ts_stop, "cycle": cycle,
                        "dur": round(real_dur, 1), "buffered": False
                    })
                    _send(s, {
                        "type": "heartbeat", "device": device_id,
                        "ts": ts_stop, "uptime": ts_stop % 86400,
                        "buf": 0, "rssi": random.randint(-80, -40)
                    })
                    time.sleep(2)
        except (ConnectionRefusedError, OSError) as e:
            print(f"[DemoESP] {e} — повтор через 3с...")
            time.sleep(3)


def _send(sock: socket.socket, data: dict):
    sock.sendall((json.dumps(data) + "\n").encode())
