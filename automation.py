"""
منطق خالص (بدون Kivy):
- برنامه‌های زمان‌بندی‌شده (events) و اعتبارسنجی برنامه‌ای که AI می‌سازد
- ضبط (Recorder)، کتابخانه ذخیره (Library) و پخش (ProgramRunner)
- ثبت مسیر (PathTracker) با تخمین از روی دستورها (dead reckoning)

هر event یک tuple است: (زمان_ثانیه, نوع, مقدار)
  ("drive", "f"/"fl"/.../"sf")   حرکت
  ("cmd",   "h"/"s3"/"st")       بوق / چرخش ۳۶۰ / ترمز
  ("light", 0 یا 1)              چراغ
  ("speed", 0..255)              سرعت
"""
import json
import math
import re
import time
from collections import OrderedDict

DIRS = {
    "forward": "f", "back": "b", "left": "l", "right": "r",
    "forward_left": "fl", "forward_right": "fr", "back_left": "bl", "back_right": "br",
}

# تقریب حرکت هر کد: (ضریب سرعت خطی، ضریب سرعت چرخش). چرخش مثبت = چپ (خلاف عقربه‌های ساعت)
MOTION = {
    "f": (1.0, 0.0), "b": (-1.0, 0.0), "l": (0.0, 1.0), "r": (0.0, -1.0),
    "fl": (0.7, 0.5), "fr": (0.7, -0.5), "bl": (-0.7, -0.5), "br": (-0.7, 0.5),
}
VALID_CMDS = {"h", "s3", "st"}

MIN_STEP_SEC = 0.1
MAX_STEP_SEC = 10.0
MAX_TOTAL_SEC = 60.0     # سقف طول هر برنامه AI
MAX_REPEAT = 50
MAX_EVENTS = 400         # سقف تعداد مرحله‌های AI
SPIN_SEC = 2.0           # مدتی که برای چرخش ۳۶۰ درجه صبر می‌کنیم
MAX_STORED_EVENTS = 5000


def _num(value, default, lo, hi):
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = default
    if v != v:   # NaN
        v = default
    return max(lo, min(hi, v))


def _truthy(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "on", "yes")
    return bool(value)


def _flatten(steps, depth, out, warnings):
    """مرحله‌ها را به مرحله‌های ساده (kind, arg, مدت) تبدیل می‌کند. False = برش خورد."""
    for step in steps:
        if len(out) >= MAX_EVENTS:
            warnings.append("too many steps, plan was cut")
            return False
        if not isinstance(step, dict):
            warnings.append("bad step skipped")
            continue
        do = str(step.get("do", "")).strip().lower()
        if do == "drive":
            code = DIRS.get(str(step.get("dir", "")).strip().lower())
            if not code:
                warnings.append("drive step with unknown direction skipped")
                continue
            out.append(("drive", code, _num(step.get("sec"), 1.0, MIN_STEP_SEC, MAX_STEP_SEC)))
        elif do == "wait":
            out.append(("wait", None, _num(step.get("sec"), 1.0, MIN_STEP_SEC, MAX_STEP_SEC)))
        elif do == "light":
            out.append(("light", 1 if _truthy(step.get("value", True)) else 0, 0.0))
        elif do == "horn":
            out.append(("cmd", "h", 0.0))
        elif do == "spin":
            out.append(("cmd", "s3", SPIN_SEC))
        elif do == "stop":
            out.append(("drive", "sf", 0.0))
        elif do == "repeat":
            if depth >= 1:
                warnings.append("nested repeat skipped")
                continue
            inner = step.get("steps")
            if not isinstance(inner, list):
                warnings.append("repeat without steps skipped")
                continue
            times = int(_num(step.get("times"), 1, 1, MAX_REPEAT))
            for _ in range(times):
                if not _flatten(inner, depth + 1, out, warnings):
                    return False
        else:
            warnings.append(f"unknown step '{do[:20]}' skipped")
    return True


def compile_plan(plan):
    """
    plan: dict {"steps": [...]} یا لیست مرحله‌ها.
    خروجی: (events, warnings). مقدارها clamp می‌شوند و کل برنامه حداکثر MAX_TOTAL_SEC است.
    """
    warnings = []
    steps = plan.get("steps") if isinstance(plan, dict) else plan
    if not isinstance(steps, list):
        return [], ["plan has no steps"]

    flat = []
    _flatten(steps, 0, flat, warnings)
    if not flat:
        return [], warnings

    events = []
    t = 0.0
    for kind, arg, dur in flat:
        if kind == "wait":
            if t + dur > MAX_TOTAL_SEC:
                warnings.append(f"plan cut at {MAX_TOTAL_SEC:.0f}s")
                break
            t += dur
            continue
        if t + dur > MAX_TOTAL_SEC:
            warnings.append(f"plan cut at {MAX_TOTAL_SEC:.0f}s")
            break
        events.append((round(t, 2), kind, arg))
        t += dur
        if kind == "drive" and arg != "sf":
            events.append((round(t, 2), "drive", "sf"))

    end = (round(t, 2), "drive", "sf")
    if not events or events[-1] != end:
        events.append(end)
    return events, warnings


def extract_json(text):
    """JSON را از جواب مدل بیرون می‌کشد (حتی اگر داخل ``` آمده باشد)."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[A-Za-z]*\s*", "", t)
        t = re.sub(r"```\s*$", "", t).strip()
    try:
        return json.loads(t)
    except ValueError:
        i, j = t.find("{"), t.rfind("}")
        if i != -1 and j > i:
            return json.loads(t[i:j + 1])
        raise


def sanitize_events(raw):
    """events خوانده‌شده از فایل را بررسی می‌کند؛ مورد نامعتبر حذف می‌شود."""
    out = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        try:
            t, kind, arg = item
            t = float(t)
        except (TypeError, ValueError):
            continue
        if t != t or t < 0:
            continue
        if kind == "drive" and isinstance(arg, str) and (arg == "sf" or arg in MOTION):
            pass
        elif kind == "cmd" and isinstance(arg, str) and arg in VALID_CMDS:
            pass
        elif kind == "light" and arg in (0, 1, True, False):
            arg = int(bool(arg))
        elif kind == "speed":
            try:
                arg = max(0, min(255, int(arg)))
            except (TypeError, ValueError):
                continue
        else:
            continue
        out.append((round(min(t, 3600.0), 2), kind, arg))
        if len(out) >= MAX_STORED_EVENTS:
            break
    out.sort(key=lambda e: e[0])
    return out


def duration(events):
    return events[-1][0] if events else 0.0


def describe_event(ev):
    t, kind, arg = ev
    if kind == "drive":
        text = "STOP" if arg == "sf" else f"DRIVE {arg.upper()}"
    elif kind == "cmd":
        text = {"h": "HORN", "s3": "SPIN", "st": "BRAKE"}.get(arg, arg)
    elif kind == "light":
        text = "LIGHT ON" if arg else "LIGHT OFF"
    else:
        text = f"SPEED {arg}"
    return f"{t:5.1f}s  {text}"


def summarize(events, limit=7):
    if not events:
        return "(empty)"
    lines = [describe_event(e) for e in events[:limit]]
    if len(events) > limit:
        lines.append(f"... +{len(events) - limit} more")
    return f"{len(events)} events, {duration(events):.1f}s\n" + "\n".join(lines)


class Recorder:
    """هر تغییر دستور حرکت، بوق، چراغ و سرعت را با زمان نسبی ضبط می‌کند."""

    def __init__(self, now=time.monotonic):
        self._now = now
        self.active = False
        self.events = []
        self._t0 = 0.0
        self._last_drive = "sf"
        self._last_speed = None

    def start(self, speed):
        self.active = True
        self.events = []
        self._t0 = self._now()
        self._last_drive = "sf"
        self._last_speed = None
        self.log("speed", int(speed))

    def elapsed(self):
        return self._now() - self._t0 if self.active else 0.0

    def log(self, kind, arg):
        if not self.active:
            return
        if kind == "drive":
            if arg == self._last_drive:
                return
            self._last_drive = arg
        elif kind == "speed":
            if arg == self._last_speed:
                return
            self._last_speed = arg
        self.events.append((round(self._now() - self._t0, 2), kind, arg))

    def stop(self):
        """ضبط را تمام می‌کند و events را برمی‌گرداند (انتها همیشه یک «توقف» است)."""
        if not self.active:
            return []
        self.log("drive", "sf")
        self.active = False
        end = (round(self._now() - self._t0, 2), "drive", "sf")
        events = list(self.events)
        if not events or events[-1] != end:
            events.append(end)
        return events

    @staticmethod
    def is_meaningful(events):
        return any(
            kind in ("drive", "cmd", "light") and not (kind == "drive" and arg == "sf")
            for _, kind, arg in events
        )


class Library:
    """ذخیره ضبط‌ها و برنامه‌های AI در یک فایل JSON."""

    def __init__(self, path):
        self.path = path
        self.data = OrderedDict()
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return
        if isinstance(raw, dict):
            for name, rec in raw.items():
                if isinstance(rec, dict):
                    events = sanitize_events(rec.get("events"))
                    if events:
                        self.data[str(name)] = {"kind": str(rec.get("kind", "rec")), "events": events}

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f)
        except OSError:
            pass

    def names(self):
        return list(self.data.keys())

    def new_name(self, prefix):
        i = 1
        while f"{prefix} {i}" in self.data:
            i += 1
        return f"{prefix} {i}"

    def add(self, prefix, events, kind):
        name = self.new_name(prefix)
        self.data[name] = {"kind": kind, "events": [list(e) for e in events]}
        self.save()
        return name

    def get(self, name):
        rec = self.data.get(name)
        return sanitize_events(rec["events"]) if rec else []

    def duration(self, name):
        return duration(self.get(name))

    def delete(self, name):
        if name in self.data:
            del self.data[name]
            self.save()


class ProgramRunner:
    """
    events را با زمان‌بندی اجرا می‌کند.
    clock: هر شیئی که schedule_once(callback, seconds) داشته باشد (در برنامه: Clock کیوی)
    on_event(kind, arg)  برای هر event؛  on_done(finished)  موقع پایان یا توقف
    """

    def __init__(self, clock, on_event, on_done):
        self.clock = clock
        self.on_event = on_event
        self.on_done = on_done
        self.running = False
        self._handles = []

    def run(self, events):
        self.stop(silent=True)
        events = list(events)
        if not events:
            return False
        self.running = True
        groups = OrderedDict()
        for t, kind, arg in events:
            groups.setdefault(t, []).append((kind, arg))
        for t, group in groups.items():
            self._handles.append(self.clock.schedule_once(lambda dt, g=group: self._fire(g), t))
        end = max(groups) + 0.05
        self._handles.append(self.clock.schedule_once(lambda dt: self._finish(), end))
        return True

    def _fire(self, group):
        if not self.running:
            return
        for kind, arg in group:
            self.on_event(kind, arg)

    def _finish(self):
        if not self.running:
            return
        self.running = False
        self._handles = []
        self.on_done(True)

    def stop(self, silent=False):
        if not self.running:
            return
        for handle in self._handles:
            handle.cancel()
        self._handles = []
        self.running = False
        if not silent:
            self.on_done(False)


class PathTracker:
    """
    مسیر را از روی «کد حرکت × سرعت × زمان» تخمین می‌زند.
    cm_per_sec : سرعت خطی ربات در سرعت 255
    deg_per_sec: سرعت چرخش ربات در سرعت 255
    """
    MIN_POINT_CM = 0.5
    MAX_POINTS = 4000

    def __init__(self, cm_per_sec=20.0, deg_per_sec=120.0):
        self.cm_per_sec = cm_per_sec
        self.deg_per_sec = deg_per_sec
        self.reset()

    def reset(self):
        self.x = 0.0
        self.y = 0.0
        self.heading = 90.0
        self.distance = 0.0
        self.points = [(0.0, 0.0)]

    def step(self, code, speed, dt):
        v_f, w_f = MOTION.get(code, (0.0, 0.0))
        if not v_f and not w_f:
            return False
        k = max(0, min(255, speed)) / 255.0
        v = self.cm_per_sec * k * v_f
        w = self.deg_per_sec * k * w_f
        mid = math.radians(self.heading + w * dt / 2.0)
        self.x += v * dt * math.cos(mid)
        self.y += v * dt * math.sin(mid)
        self.heading = (self.heading + w * dt) % 360.0
        self.distance += abs(v) * dt
        lx, ly = self.points[-1]
        if math.hypot(self.x - lx, self.y - ly) >= self.MIN_POINT_CM:
            self.points.append((self.x, self.y))
            if len(self.points) > self.MAX_POINTS:
                self.points = self.points[::2]
        return True
