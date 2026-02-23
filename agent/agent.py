import os
import sys
import time
import json
import hashlib
import getpass
import threading
import subprocess
import importlib.util
from datetime import datetime

def data_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    p = os.path.join(base, "TimeTrack")
    os.makedirs(p, exist_ok=True)
    return p

def log_path():
    return os.path.join(data_dir(), "agent.log")

def log_line(msg):
    try:
        with open(log_path(), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass

def ensure_deps():
    need = {
        "requests": "requests",
        "pynput": "pynput",
        "pystray": "pystray",
        "PIL": "pillow",
    }
    missing = []
    for mod, pip_name in need.items():
        if importlib.util.find_spec(mod) is None:
            missing.append(pip_name)

    if not missing:
        return

    try:
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade"] + missing
        subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log_line(f"pip install failed: {e}")
        raise

ensure_deps()

import requests
from pynput import mouse, keyboard
import pystray
from pystray import MenuItem as item
from PIL import Image, ImageDraw

def token_path():
    return os.path.join(data_dir(), "device.dat")

def machine_guid():
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography")
        val, _ = winreg.QueryValueEx(key, "MachineGuid")
        return str(val)
    except Exception:
        return "noguid"

def device_hash():
    user = os.environ.get("USERNAME", "")
    raw = (machine_guid() + "|" + user).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

def load_token():
    p = token_path()
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            return ""
    return ""

def save_token(t):
    with open(token_path(), "w", encoding="utf-8") as f:
        f.write(t)

class ActivityState:
    def __init__(self):
        self.last = time.time()
        self.lock = threading.Lock()

    def bump(self):
        with self.lock:
            self.last = time.time()

    def active(self, idle_seconds):
        with self.lock:
            return (time.time() - self.last) < idle_seconds

def handshake(base_url):
    username = input("Логин: ").strip()
    password = getpass.getpass("Пароль: ")
    payload = {"username": username, "password": password, "device_hash": device_hash()}
    r = requests.post(base_url.rstrip("/") + "/api/agent/handshake", json=payload, timeout=10)
    r.raise_for_status()
    t = (r.json() or {}).get("token", "")
    t = (t or "").strip()
    if not t:
        raise RuntimeError("Handshake failed")
    save_token(t)
    return t

def make_icon_image():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, 58, 58), fill=(30, 144, 255, 255))
    d.rectangle((28, 18, 36, 40), fill=(255, 255, 255, 255))
    d.rectangle((28, 42, 36, 50), fill=(255, 255, 255, 255))
    return img

class AgentApp:
    def __init__(self):
        self.base_url = os.environ.get("TIMETRACK_SERVER", "http://127.0.0.1:5000")
        self.ping_url = self.base_url.rstrip("/") + "/api/agent/ping"
        self.idle_seconds = int(os.environ.get("TIMETRACK_IDLE", "60"))
        self.ping_seconds = int(os.environ.get("TIMETRACK_PING", "15"))

        self.token = load_token()
        self.state = ActivityState()

        self.running = threading.Event()
        self.running.set()

        self.stop_all = threading.Event()

        self.icon = pystray.Icon(
            "TimeTrack",
            make_icon_image(),
            "TimeTrack",
            menu=pystray.Menu(
                item("Статус", self.menu_status, enabled=False),
                item("Пауза/Продолжить", self.toggle_running),
                item("Выход", self.quit_app),
            ),
        )

        self._status_text = "Инициализация"

    def menu_status(self, icon, it):
        return

    def set_status(self, text):
        self._status_text = text
        try:
            self.icon.title = f"TimeTrack — {text}"
        except Exception:
            pass

    def toggle_running(self, icon, it):
        if self.running.is_set():
            self.running.clear()
            self.set_status("Пауза")
        else:
            self.running.set()
            self.set_status("Работает")

    def quit_app(self, icon, it):
        self.stop_all.set()
        try:
            self.icon.stop()
        except Exception:
            pass

    def start_listeners(self):
        def on_mouse_move(x, y): self.state.bump()
        def on_click(x, y, button, pressed): self.state.bump()
        def on_scroll(x, y, dx, dy): self.state.bump()
        def on_key_press(key): self.state.bump()

        mouse.Listener(on_move=on_mouse_move, on_click=on_click, on_scroll=on_scroll).start()
        keyboard.Listener(on_press=on_key_press).start()

    def ping_loop(self):
        headers = {"Content-Type": "application/json"}
        while not self.stop_all.is_set():
            if not self.running.is_set():
                time.sleep(0.5)
                continue

            if not self.token:
                try:
                    self.set_status("Ожидание входа")
                    self.token = handshake(self.base_url)
                except Exception as e:
                    log_line(f"handshake error: {e}")
                    self.set_status("Ошибка входа")
                    time.sleep(5)
                    continue

            headers["X-Device-Token"] = self.token
            headers["X-Device-Hash"] = device_hash()

            active = self.state.active(self.idle_seconds)

            try:
                requests.post(self.ping_url, headers=headers, json={"active": bool(active)}, timeout=5)
                self.set_status("Активен" if active else "Неактивен")
            except Exception as e:
                log_line(f"ping error: {e}")
                self.set_status("Нет связи")
                time.sleep(3)

            time.sleep(self.ping_seconds)

    def run(self):
        self.start_listeners()
        t = threading.Thread(target=self.ping_loop, daemon=True)
        t.start()
        self.set_status("Работает")
        self.icon.run()

def main():
    try:
        app = AgentApp()
        app.run()
    except Exception as e:
        log_line(f"fatal: {e}")

if __name__ == "__main__":
    main()
