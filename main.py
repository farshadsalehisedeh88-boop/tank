"""Tank Controller - کنترل ربات تانک (ESP) از طریق HTTP با Kivy."""
import ipaddress
import json
import math
import os
import re
import threading
from collections import deque

import kivy  # noqa: F401
import requests
from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.graphics import Color, Ellipse, Line, Rectangle, Triangle
from kivy.metrics import dp
from kivy.uix.behaviors import ButtonBehavior
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.slider import Slider
from kivy.uix.textinput import TextInput
from kivy.uix.widget import Widget
from kivy.utils import platform

from ai import DEFAULT_MODELS
from automation import Library, PathTracker, ProgramRunner, Recorder
from features import AIPopup, MapPopup, RecordsPopup
from tilt import AccelReader, TiltController, screen_rotation
from voice import AndroidSpeech, Command, contains_stop, describe, parse_any

if platform in ("android", "ios"):
    Window.softinput_mode = "pan"
else:
    Window.size = (960, 480)

DEFAULT_IP = "192.168.4.1"
DEFAULT_SPEED = 150
REQUEST_TIMEOUT = (0.5, 0.5)
HEARTBEAT_SEC = 0.2
PING_SEC = 2.0
SPEED_THROTTLE_SEC = 0.1
MAX_QUEUE = 32

TILT_TICK_SEC = 0.05
VOICE_LANG = "fa-IR"
VOICE_MOVE_SEC = 2.0
NOTIFY_MODES = True
TOGGLE_PATHS = {"tilt": "im", "voice": "vc", "light": "w"}
DEFAULT_CM_PER_SEC = 20.0
DEFAULT_DEG_PER_SEC = 120.0
TRACK_TICK_SEC = 0.1
AI_LISTEN_TIMEOUT_SEC = 15.0

IP_BG = (0.14, 0.14, 0.17, 1)
IP_BAD_BG = (0.45, 0.12, 0.12, 1)
RING_COLOR = (0.25, 0.85, 0.45, 1)

_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]*[A-Za-z0-9])?(:\d{1,5})?$")


def is_valid_host(value):
    if not _HOST_RE.match(value):
        return False
    host = value.split(":")[0]
    if re.fullmatch(r"[\d.]+", host):
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            return False
    return True


def load_settings(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def _cfg_float(cfg, key, default, lo, hi):
    try:
        value = float(cfg.get(key, default))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value)) if value == value else default


class RobotLink:
    def __init__(self, host, on_status=None):
        self.host = host
        self.on_status = on_status
        self._queue = deque(maxlen=MAX_QUEUE)
        self._cond = threading.Condition()
        self._session = requests.Session()
        self._running = True
        self._connected = None
        threading.Thread(target=self._run, daemon=True).start()

    def _url(self, path):
        return f"http://{self.host}/{path}"

    @property
    def connected(self):
        return self._connected

    def send(self, path, key=None):
        with self._cond:
            if key is not None:
                self._queue = deque(
                    (item for item in self._queue if item[0] != key),
                    maxlen=MAX_QUEUE,
                )
            self._queue.append((key, path))
            self._cond.notify()

    def send_now(self, path, tries=2):
        with self._cond:
            self._queue.clear()
        for _ in range(tries):
            try:
                requests.get(self._url(path), timeout=REQUEST_TIMEOUT)
                return
            except requests.RequestException:
                continue

    def set_host(self, host):
        with self._cond:
            self.host = host
            self._queue.clear()
            self._connected = None
            self._cond.notify()

    def stop(self):
        with self._cond:
            self._running = False
            self._cond.notify_all()

    def _run(self):
        while True:
            with self._cond:
                if not self._queue:
                    self._cond.wait(PING_SEC)
                if not self._running:
                    return
                item = self._queue.popleft() if self._queue else None
            path = item[1] if item else ""
            self._report(self._fetch(path))

    def _fetch(self, path):
        try:
            self._session.get(self._url(path), timeout=REQUEST_TIMEOUT)
            return True
        except requests.RequestException:
            return False

    def _report(self, ok):
        if ok != self._connected:
            self._connected = ok
            if self.on_status:
                Clock.schedule_once(lambda dt: self.on_status(ok), 0)


def arc_points(cx, cy, r, start_deg, end_deg, step=8):
    pts = []
    n = max(2, int(abs(end_deg - start_deg) / step))
    for i in range(n + 1):
        a = math.radians(start_deg + (end_deg - start_deg) * i / n)
        pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
    return pts


class StatusDot(Widget):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.ok = None
        self.bind(pos=self._redraw, size=self._redraw)

    def set_status(self, ok):
        self.ok = ok
        self._redraw()

    def _redraw(self, *args):
        self.canvas.clear()
        d = min(self.width, self.height) * 0.5
        if d <= 2:
            return
        with self.canvas:
            if self.ok is None:
                Color(0.5, 0.5, 0.55, 1)
            elif self.ok:
                Color(0.2, 0.85, 0.4, 1)
            else:
                Color(0.9, 0.25, 0.25, 1)
            Ellipse(pos=(self.center_x - d / 2, self.center_y - d / 2), size=(d, d))


class CircleIconButton(ButtonBehavior, Widget):
    def __init__(
        self,
        icon_type,
        base_color=(0.18, 0.18, 0.21, 1),
        icon_color=(1, 1, 1, 1),
        active=False,
        is_toggle=False,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.always_release = True
        self.icon_type = icon_type
        self.base_color = list(base_color)
        self.icon_color = list(icon_color)
        self.active = active
        self.is_toggle = is_toggle
        self.dim = False
        self.ring_color = RING_COLOR
        self.bind(pos=self._redraw, size=self._redraw, state=self._redraw)

    def set_active(self, is_active):
        self.active = is_active
        self._redraw()

    def set_dim(self, dim):
        self.dim = dim
        self._redraw()

    def collide_point(self, x, y):
        r = min(self.width, self.height) / 2.0
        cx, cy = self.center
        return (x - cx) ** 2 + (y - cy) ** 2 <= r * r

    def _current_bg(self):
        r, g, b, a = self.base_color
        if self.state == "down":
            k = 0.32
        elif self.active:
            k = 0.16
        else:
            return (r, g, b, a)
        return (min(1.0, r + k), min(1.0, g + k), min(1.0, b + k), a)

    def _redraw(self, *args):
        self.canvas.clear()
        d = min(self.width, self.height)
        if d <= 4:
            return
        cx = self.x + self.width / 2.0
        cy = self.y + self.height / 2.0
        rad = d / 2.0 - 2.0

        with self.canvas:
            Color(*self._current_bg())
            Ellipse(pos=(cx - rad, cy - rad), size=(rad * 2.0, rad * 2.0))
            if self.is_toggle and self.active:
                Color(*self.ring_color)
                Line(circle=(cx, cy, rad), width=2.6)
            elif self.icon_type == "ai":
                Color(0.68, 0.45, 0.95, 1)
                Line(circle=(cx, cy, rad), width=1.8)
            else:
                Color(0.45, 0.45, 0.50, 1)
                Line(circle=(cx, cy, rad), width=1.2)
            self._draw_icon(cx, cy, rad * 0.48)

    def _draw_icon(self, cx, cy, s):
        t = self.icon_type
        r, g, b, a = self.icon_color
        Color(r, g, b, a * (0.35 if self.dim else 1.0))

        if t == "left":
            Triangle(points=[cx - s * 0.8, cy, cx + s * 0.55, cy + s * 0.75, cx + s * 0.55, cy - s * 0.75])
        elif t == "right":
            Triangle(points=[cx + s * 0.8, cy, cx - s * 0.55, cy + s * 0.75, cx - s * 0.55, cy - s * 0.75])
        elif t == "up":
            Triangle(points=[cx, cy + s * 0.8, cx - s * 0.75, cy - s * 0.55, cx + s * 0.75, cy - s * 0.55])
        elif t == "down":
            Triangle(points=[cx, cy - s * 0.8, cx - s * 0.75, cy + s * 0.55, cx + s * 0.75, cy + s * 0.55])
        elif t == "stop":
            sq = s * 1.15
            Rectangle(pos=(cx - sq / 2, cy - sq / 2), size=(sq, sq))
        elif t == "check":
            Line(points=[cx - s * 0.6, cy, cx - s * 0.15, cy - s * 0.45, cx + s * 0.65, cy + s * 0.5], width=2.4)
        elif t == "spin360":
            rr = s * 0.78
            Line(points=arc_points(cx, cy, rr, 40, 330), width=2.0)
            end = math.radians(330)
            ex, ey = cx + rr * math.cos(end), cy + rr * math.sin(end)
            tx, ty = -math.sin(end), math.cos(end)
            nx, ny = math.cos(end), math.sin(end)
            Triangle(
                points=[
                    ex + tx * s * 0.38, ey + ty * s * 0.38,
                    ex + nx * s * 0.25, ey + ny * s * 0.25,
                    ex - nx * s * 0.25, ey - ny * s * 0.25,
                ]
            )
        elif t == "horn":
            Line(
                points=[
                    cx - s * 0.65, cy - s * 0.22,
                    cx - s * 0.15, cy - s * 0.22,
                    cx + s * 0.45, cy - s * 0.62,
                    cx + s * 0.45, cy + s * 0.62,
                    cx - s * 0.15, cy + s * 0.22,
                    cx - s * 0.65, cy + s * 0.22,
                ],
                close=True,
                width=1.8,
            )
            Line(points=arc_points(cx + s * 0.45, cy, s * 0.38, -50, 50), width=1.6)
        elif t == "light":
            if self.active:
                Color(1.0, 0.85, 0.2, 1)
            else:
                Color(0.75, 0.75, 0.78, 1)
            Line(circle=(cx, cy + s * 0.15, s * 0.52), width=1.8)
            Line(
                points=[
                    cx - s * 0.25, cy - s * 0.45,
                    cx + s * 0.25, cy - s * 0.45,
                    cx + s * 0.20, cy - s * 0.75,
                    cx - s * 0.20, cy - s * 0.75,
                ],
                close=True,
                width=1.6,
            )
            if self.active:
                yc = cy + s * 0.15
                for deg in range(0, 360, 60):
                    ang = math.radians(deg)
                    Line(
                        points=[
                            cx + s * 0.72 * math.cos(ang), yc + s * 0.72 * math.sin(ang),
                            cx + s * 0.96 * math.cos(ang), yc + s * 0.96 * math.sin(ang),
                        ],
                        width=1.6,
                    )
        elif t == "tilt":
            pw, ph = s * 0.78, s * 1.25
            Line(rectangle=(cx - pw / 2, cy - ph / 2, pw, ph), width=2.2)
            Line(points=[cx - pw * 0.22, cy - ph * 0.35, cx + pw * 0.22, cy - ph * 0.35], width=1.8)
        elif t == "mic":
            Line(rounded_rectangle=(cx - s * 0.25, cy - s * 0.05, s * 0.5, s * 0.9, s * 0.25), width=1.8)
            Line(points=arc_points(cx, cy + s * 0.1, s * 0.55, 200, 340), width=1.8)
            Line(points=[cx, cy - s * 0.45, cx, cy - s * 0.75], width=1.8)
            Line(points=[cx - s * 0.35, cy - s * 0.75, cx + s * 0.35, cy - s * 0.75], width=1.8)
        elif t == "map":
            Line(
                points=[cx - s * 0.8, cy - s * 0.5, cx - s * 0.25, cy + s * 0.15,
                        cx + s * 0.2, cy - s * 0.35, cx + s * 0.8, cy + s * 0.55],
                width=2.0,
            )
            rr = s * 0.16
            Ellipse(pos=(cx - s * 0.8 - rr, cy - s * 0.5 - rr), size=(rr * 2, rr * 2))
            Ellipse(pos=(cx + s * 0.8 - rr, cy + s * 0.55 - rr), size=(rr * 2, rr * 2))
        elif t == "rec":
            Color(0.92, 0.22, 0.22, 1)
            Ellipse(pos=(cx - s * 0.7, cy - s * 0.7), size=(s * 1.4, s * 1.4))
        elif t == "play":
            Triangle(points=[cx - s * 0.5, cy + s * 0.8, cx - s * 0.5, cy - s * 0.8, cx + s * 0.85, cy])
        elif t == "ai":
            a, b = s * 0.95, s * 0.28
            Triangle(points=[cx, cy + a, cx - b, cy, cx + b, cy])
            Triangle(points=[cx, cy - a, cx - b, cy, cx + b, cy])
            Triangle(points=[cx + a, cy, cx, cy - b, cx, cy + b])
            Triangle(points=[cx - a, cy, cx, cy - b, cx, cy + b])


class TankPanel(BoxLayout):
    def __init__(self, settings_path, **kwargs):
        super().__init__(orientation="horizontal", padding=dp(10), spacing=dp(10), **kwargs)
        self.settings_path = settings_path

        cfg = load_settings(settings_path)
        host = str(cfg.get("ip", DEFAULT_IP))
        if not is_valid_host(host):
            host = DEFAULT_IP
        try:
            self.speed = max(0, min(255, int(cfg.get("speed", DEFAULT_SPEED))))
        except (TypeError, ValueError):
            self.speed = DEFAULT_SPEED

        self.held = {"fb": [], "lr": []}
        self.voice_dir = {"fb": "", "lr": ""}
        self.tilt_dir = {"fb": "", "lr": ""}
        self.last_drive = None

        self.toggles = {"tilt": False, "voice": False, "light": False}
        self.toggle_btns = {}
        self.hold_buttons = []
        self.prog_dir = {"fb": "", "lr": ""}

        self.accel = AccelReader()
        self.tilt = TiltController()
        self._tilt_ev = None
        self.speech = AndroidSpeech(
            on_text=self._on_voice_text,
            on_info=self._set_info,
            on_stopped=self._on_voice_stopped,
            lang=VOICE_LANG,
        )

        self.status = StatusDot(size_hint_x=None, width=dp(24))
        self.link = RobotLink(host, on_status=self._on_status)
        self._speed_trigger = Clock.create_trigger(self._send_speed, SPEED_THROTTLE_SEC)

        self.api_key = str(cfg.get("api_key", ""))
        model = str(cfg.get("gemini_model", "")).strip()
        self.gemini_model = model
        self.gemini_models = ([model] if model else []) + [m for m in DEFAULT_MODELS if m != model]
        self.tracker = PathTracker(
            _cfg_float(cfg, "cm_per_sec", DEFAULT_CM_PER_SEC, 1.0, 500.0),
            _cfg_float(cfg, "deg_per_sec", DEFAULT_DEG_PER_SEC, 10.0, 720.0),
        )
        self.recorder = Recorder()
        self.library = Library(os.path.join(os.path.dirname(settings_path), "library.json"))
        self.runner = ProgramRunner(Clock, self._on_program_event, self._on_program_done)
        self._ai_cb = None
        self._ai_timeout_ev = None
        self._ai_popup = None

        self.add_widget(self._build_left())
        self.add_widget(self._build_center(host))
        self.add_widget(self._build_right())

        Clock.schedule_interval(self._heartbeat, HEARTBEAT_SEC)
        Clock.schedule_interval(self._track_tick, TRACK_TICK_SEC)

    def _clear_ai_listen(self, stop_speech_if_needed=True):
        self._ai_cb = None
        if self._ai_timeout_ev is not None:
            self._ai_timeout_ev.cancel()
            self._ai_timeout_ev = None
        if stop_speech_if_needed and not self.toggles["voice"]:
            self.speech.stop()

    def _axis_dir(self, axis):
        stack = self.held[axis]
        if stack:
            return stack[-1]
        return self.prog_dir[axis] or self.voice_dir[axis] or self.tilt_dir[axis]

    def _drive_code(self):
        return (self._axis_dir("fb") + self._axis_dir("lr")) or "sf"

    def _refresh_drive(self):
        code = self._drive_code()
        if code != self.last_drive:
            self.last_drive = code
            self.link.send(code, key="drive")
            self.recorder.log("drive", code)

    def _set_axis(self, axis, direction, pressed):
        if pressed and self.runner.running:
            self.runner.stop()
        stack = self.held[axis]
        if direction in stack:
            stack.remove(direction)
        if pressed:
            stack.append(direction)
        self._refresh_drive()

    def _heartbeat(self, dt):
        if self.last_drive and self.last_drive != "sf":
            self.link.send(self.last_drive, key="drive")

    def emergency_stop(self):
        self._clear_ai_listen(stop_speech_if_needed=False)
        if self.runner.running:
            self.runner.stop()
        if self.recorder.active:
            self.toggle_recording()
        for name in ("tilt", "voice"):
            if self.toggles[name]:
                self._set_toggle(name, False, notify=False)
        for stack in self.held.values():
            stack.clear()
        self.tilt_dir = {"fb": "", "lr": ""}
        self.voice_dir = {"fb": "", "lr": ""}
        Clock.unschedule(self._voice_timeout)
        for btn in self.hold_buttons:
            btn.state = "normal"
        self.last_drive = "sf"
        self.link.send_now("sf")

    def _set_toggle(self, name, value, notify=True):
        if name == "tilt":
            value = self._enable_tilt(value)
        elif name == "voice":
            value = self._enable_voice(value)
        self.toggles[name] = value
        self.toggle_btns[name].set_active(value)
        if name == "light":
            self.recorder.log("light", int(value))
        if notify and (name == "light" or NOTIFY_MODES):
            self.link.send(f"{TOGGLE_PATHS[name]}?v={int(value)}", key=name)

    def _enable_tilt(self, on):
        if not on:
            if self._tilt_ev is not None:
                self._tilt_ev.cancel()
                self._tilt_ev = None
            self.accel.stop()
            self.tilt_dir = {"fb": "", "lr": ""}
            self._refresh_drive()
            return False
        if not self.accel.available():
            self._set_info("TILT: Android only")
            return False
        try:
            self.accel.start()
        except Exception:
            self._set_info("TILT: no accelerometer")
            return False
        self.tilt.recalibrate()
        self._tilt_ev = Clock.schedule_interval(self._tilt_tick, TILT_TICK_SEC)
        self._set_info("TILT: hold level...")
        return True

    def _tilt_tick(self, dt):
        vals = self.accel.read()
        if vals is None:
            return
        was_ready = self.tilt.calibrated
        fb, lr = self.tilt.update(vals[0], vals[1], vals[2], screen_rotation())
        if self.tilt.calibrated and not was_ready:
            self._set_info("TILT: ready")
        if (fb, lr) != (self.tilt_dir["fb"], self.tilt_dir["lr"]):
            self.tilt_dir = {"fb": fb, "lr": lr}
            self._refresh_drive()

    def _enable_voice(self, on):
        if not on:
            self._clear_ai_listen(stop_speech_if_needed=False)
            self.speech.stop()
            self._clear_voice_move()
            return False
        if not self.speech.available():
            self._set_info("VOICE: Android only")
            return False
        try:
            self.speech.start()
        except Exception as exc:
            self._set_info(f"VOICE: {exc}")
            return False
        self._set_info("VOICE: starting...")
        return True

    def _on_voice_stopped(self, message):
        self._clear_ai_listen(stop_speech_if_needed=False)
        self._set_info(message)
        if self.toggles["voice"]:
            self._set_toggle("voice", False)

    def _on_voice_text(self, texts, final):
        if self._ai_cb is not None:
            if final and texts:
                callback = self._ai_cb
                self._clear_ai_listen(stop_speech_if_needed=True)
                callback(texts[0])
            return
        if contains_stop(texts):
            self._apply_voice(Command(stop=True))
            return
        if not final:
            return
        cmd = parse_any(texts)
        if cmd is None:
            self._set_info("VOICE: ?")
            return
        self._apply_voice(cmd)

    def _apply_voice(self, cmd):
        self._set_info(f"VOICE: {describe(cmd)}")
        if cmd.stop:
            self._clear_voice_move()
            self._cmd("st")
            return
        if cmd.fb or cmd.lr:
            self.voice_dir = {"fb": cmd.fb, "lr": cmd.lr}
            Clock.unschedule(self._voice_timeout)
            Clock.schedule_once(self._voice_timeout, VOICE_MOVE_SEC)
            self._refresh_drive()
        if cmd.spin:
            self._cmd("s3")
        if cmd.horn:
            self._cmd("h")
        if cmd.light:
            target = (not self.toggles["light"]) if cmd.light == "toggle" else cmd.light == "on"
            self._set_toggle("light", target)

    def _voice_timeout(self, dt):
        self._clear_voice_move()

    def _clear_voice_move(self):
        Clock.unschedule(self._voice_timeout)
        self.voice_dir = {"fb": "", "lr": ""}
        self._refresh_drive()

    def _on_status(self, ok):
        self.status.set_status(ok)
        if ok:
            self._sync_state()

    def _sync_state(self):
        self._send_speed()
        for name, path in TOGGLE_PATHS.items():
            if name == "light" or NOTIFY_MODES:
                self.link.send(f"{path}?v={int(self.toggles[name])}", key=name)

    def _set_info(self, text):
        self.info_lbl.text = text

    def _hold_button(self, icon, axis, direction):
        btn = CircleIconButton(icon, base_color=(0.17, 0.17, 0.20, 1))
        btn.bind(on_press=lambda _: self._set_axis(axis, direction, True))
        btn.bind(on_release=lambda _: self._set_axis(axis, direction, False))
        self.hold_buttons.append(btn)
        return btn

    def _toggle_button(self, icon, name, supported=True, **style):
        btn = CircleIconButton(icon, is_toggle=True, **style)
        btn.set_dim(not supported)
        btn.bind(on_press=lambda _: self._set_toggle(name, not self.toggles[name]))
        self.toggle_btns[name] = btn
        return btn

    def _build_left(self):
        col = BoxLayout(orientation="vertical", size_hint_x=0.30, spacing=dp(8))
        top = BoxLayout(size_hint_y=0.45, spacing=dp(6), padding=[dp(4), dp(4)])
        btn_map = CircleIconButton("map", base_color=(0.16, 0.22, 0.30, 1))
        btn_map.bind(on_release=lambda _: MapPopup(self).open())
        self.rec_btn = CircleIconButton("rec", is_toggle=True, base_color=(0.28, 0.12, 0.12, 1))
        self.rec_btn.ring_color = (0.95, 0.25, 0.25, 1)
        self.rec_btn.bind(on_press=lambda _: self.toggle_recording())
        btn_play = CircleIconButton("play", base_color=(0.16, 0.28, 0.22, 1))
        btn_play.bind(on_release=lambda _: RecordsPopup(self).open())
        btn_ai = CircleIconButton("ai", base_color=(0.30, 0.18, 0.44, 1))
        btn_ai.bind(on_release=lambda _: self._open_ai())
        for b in (btn_map, self.rec_btn, btn_play, btn_ai):
            top.add_widget(b)
        col.add_widget(top)
        row = BoxLayout(size_hint_y=0.55, spacing=dp(12), padding=[dp(8), dp(4)])
        row.add_widget(self._hold_button("left", "lr", "l"))
        row.add_widget(self._hold_button("right", "lr", "r"))
        col.add_widget(row)
        return col

    def _build_center(self, host):
        col = BoxLayout(orientation="vertical", size_hint_x=0.34, spacing=dp(6), padding=[dp(6), dp(4)])

        ip_row = BoxLayout(size_hint_y=0.18, spacing=dp(6))
        self.ip_input = TextInput(
            text=host,
            multiline=False,
            font_size="14sp",
            background_color=IP_BG,
            foreground_color=(1, 1, 1, 1),
        )
        self.ip_input.bind(on_text_validate=self._apply_ip)
        btn_ip = CircleIconButton(
            "check", base_color=(0.12, 0.30, 0.18, 1), size_hint_x=None, width=dp(48)
        )
        btn_ip.bind(on_release=self._apply_ip)
        ip_row.add_widget(self.status)
        ip_row.add_widget(self.ip_input)
        ip_row.add_widget(btn_ip)

        self.sp_lbl = Label(
            text=f"SP: {self.speed}", font_size="14sp", size_hint_y=0.13, color=(0.9, 0.9, 0.9, 1)
        )
        self.slider = Slider(min=0, max=255, value=self.speed, step=1, size_hint_y=0.20)
        self.slider.bind(value=self._on_speed)

        self.info_lbl = Label(text="", font_size="13sp", size_hint_y=0.15, color=(0.7, 0.9, 0.75, 1))

        mid_row = BoxLayout(size_hint_y=0.34, spacing=dp(12), padding=[dp(16), dp(2)])
        btn_st = CircleIconButton("stop", base_color=(0.10, 0.10, 0.12, 1))
        btn_st.bind(on_press=lambda _: self._brake())
        btn_s3 = CircleIconButton("spin360", base_color=(0.18, 0.18, 0.22, 1))
        btn_s3.bind(on_press=lambda _: self._cmd("s3"))
        mid_row.add_widget(btn_st)
        mid_row.add_widget(btn_s3)

        col.add_widget(ip_row)
        col.add_widget(self.sp_lbl)
        col.add_widget(self.slider)
        col.add_widget(self.info_lbl)
        col.add_widget(mid_row)
        return col

    def _build_right(self):
        col = BoxLayout(orientation="vertical", size_hint_x=0.36, spacing=dp(8))

        tools = BoxLayout(size_hint_y=0.45, spacing=dp(8), padding=[dp(4), dp(4)])
        tools.add_widget(
            self._toggle_button(
                "tilt", "tilt", supported=self.accel.available(),
                base_color=(0.75, 0.75, 0.78, 1), icon_color=(0.05, 0.05, 0.06, 1),
            )
        )
        tools.add_widget(
            self._toggle_button(
                "mic", "voice", supported=self.speech.available(),
                base_color=(0.16, 0.28, 0.22, 1),
            )
        )
        tools.add_widget(self._toggle_button("light", "light", base_color=(0.20, 0.20, 0.24, 1)))
        btn_horn = CircleIconButton("horn", base_color=(0.20, 0.20, 0.24, 1))
        btn_horn.bind(on_press=lambda _: self._cmd("h"))
        tools.add_widget(btn_horn)

        fb_row = BoxLayout(size_hint_y=0.55, spacing=dp(12), padding=[dp(8), dp(4)])
        fb_row.add_widget(self._hold_button("up", "fb", "f"))
        fb_row.add_widget(self._hold_button("down", "fb", "b"))

        col.add_widget(tools)
        col.add_widget(fb_row)
        return col

    def _on_speed(self, _, val):
        self.speed = int(val)
        self.sp_lbl.text = f"SP: {self.speed}"
        self._speed_trigger()

    def _send_speed(self, *args):
        self.link.send(f"sp?v={self.speed}", key="sp")
        self.recorder.log("speed", self.speed)

    def _apply_ip(self, *args):
        host = self.ip_input.text.strip()
        if is_valid_host(host):
            self.link.set_host(host)
            self.status.set_status(None)
            self.save()
        else:
            self.ip_input.background_color = IP_BAD_BG
            Clock.schedule_once(
                lambda dt: setattr(self.ip_input, "background_color", IP_BG), 1.0
            )

    def _cmd(self, path):
        self.link.send(path)
        self.recorder.log("cmd", path)

    def _brake(self):
        self.runner.stop()
        self._cmd("st")

    def _track_tick(self, dt):
        if self.link.connected is False:
            return
        self.tracker.step(self.last_drive or "sf", self.speed, min(dt, 0.5))

    def set_calibration(self, cm_text, deg_text):
        try:
            cm = max(1.0, min(500.0, float(cm_text)))
            deg = max(10.0, min(720.0, float(deg_text)))
        except ValueError:
            return
        self.tracker.cm_per_sec = cm
        self.tracker.deg_per_sec = deg
        self.save()

    def toggle_recording(self):
        if self.recorder.active:
            events = self.recorder.stop()
            self.rec_btn.set_active(False)
            if self.recorder.is_meaningful(events):
                name = self.library.add("REC", events, "rec")
                self._set_info(f"REC saved: {name}")
            else:
                self._set_info("REC: nothing recorded")
            return
        if self.runner.running:
            self.runner.stop()
        self.recorder.start(self.speed)
        if self.last_drive and self.last_drive != "sf":
            self.recorder.log("drive", self.last_drive)
        self.rec_btn.set_active(True)
        self._set_info("REC: recording...")

    def run_events(self, events):
        if self.recorder.active:
            self.toggle_recording()
        if self.runner.run(events):
            self._set_info("PROG: running (touch a drive button to stop)")

    def stop_program(self):
        self.runner.stop()

    @staticmethod
    def _split(code):
        if not code or code == "sf":
            return {"fb": "", "lr": ""}
        return {
            "fb": code[0] if code[0] in "fb" else "",
            "lr": code[-1] if code[-1] in "lr" else "",
        }

    def _on_program_event(self, kind, arg):
        if kind == "drive":
            self.prog_dir = self._split(arg)
            self._refresh_drive()
        elif kind == "cmd":
            self._cmd(arg)
        elif kind == "light":
            self._set_toggle("light", bool(arg))
        elif kind == "speed":
            self._apply_speed(arg)

    def _on_program_done(self, finished):
        self.prog_dir = {"fb": "", "lr": ""}
        self._refresh_drive()
        self._set_info("PROG: done" if finished else "PROG: stopped")

    def _apply_speed(self, value):
        self.speed = max(0, min(255, int(value)))
        self.slider.value = self.speed
        self.sp_lbl.text = f"SP: {self.speed}"
        self._send_speed()

    def _open_ai(self):
        if self._ai_popup is None:
            self._ai_popup = AIPopup(self)
        self._ai_popup.open()

    def ai_listen(self, callback):
        if not self.speech.available():
            self._set_info("VOICE: Android only")
            return False
        self._clear_ai_listen(stop_speech_if_needed=False)
        self._ai_cb = callback
        if not self.toggles["voice"]:
            try:
                self.speech.start()
            except Exception as exc:
                self._ai_cb = None
                self._set_info(f"VOICE: {exc}")
                return False
        self._ai_timeout_ev = Clock.schedule_once(self._ai_listen_timeout, AI_LISTEN_TIMEOUT_SEC)
        self._set_info("AI: listening...")
        return True

    def _ai_listen_timeout(self, dt):
        if self._ai_cb is not None:
            self._clear_ai_listen(stop_speech_if_needed=True)
            self._set_info("AI: no speech heard")

    def save(self):
        save_settings(self.settings_path, {
            "ip": self.link.host,
            "speed": self.speed,
            "api_key": self.api_key,
            "gemini_model": self.gemini_model,
            "cm_per_sec": self.tracker.cm_per_sec,
            "deg_per_sec": self.tracker.deg_per_sec,
        })

    def shutdown(self):
        self._clear_ai_listen(stop_speech_if_needed=False)
        Clock.unschedule(self._heartbeat)
        Clock.unschedule(self._track_tick)
        self.speech.stop()
        self.accel.stop()
        self.link.stop()


class TankApp(App):
    title = "Tank Controller"

    def build(self):
        path = os.path.join(self.user_data_dir, "settings.json")
        self.panel = TankPanel(path)
        return self.panel

    def on_start(self):
        if platform == "android":
            self._android_setup()

    @staticmethod
    def _android_setup():
        try:
            from android.permissions import Permission, request_permissions

            request_permissions([Permission.RECORD_AUDIO])
        except Exception:
            pass
        try:
            from android.runnable import run_on_ui_thread
            from jnius import autoclass

            @run_on_ui_thread
            def keep_screen_on():
                activity = autoclass("org.kivy.android.PythonActivity").mActivity
                params = autoclass("android.view.WindowManager$LayoutParams")
                activity.getWindow().addFlags(params.FLAG_KEEP_SCREEN_ON)

            keep_screen_on()
        except Exception:
            pass

    def on_pause(self):
        self.panel.emergency_stop()
        self.panel.save()
        return True

    def on_resume(self):
        pass

    def on_stop(self):
        self.panel.emergency_stop()
        self.panel.save()
        self.panel.shutdown()


if __name__ == "__main__":
    TankApp().run()
