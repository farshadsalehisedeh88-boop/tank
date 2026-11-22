"""فرمان صوتی: تشخیص گفتار اندروید + تبدیل متن به دستور ربات."""
import os
import re
from typing import NamedTuple

IS_ANDROID = "ANDROID_ARGUMENT" in os.environ


class Command(NamedTuple):
    stop: bool = False
    fb: str = ""
    lr: str = ""
    spin: bool = False
    horn: bool = False
    light: str = ""


_FIX = str.maketrans({
    "ي": "ی", "ك": "ک", "ة": "ه", "أ": "ا", "إ": "ا", "ؤ": "و",
    "\u200c": " ",
    "ـ": "",
})


def normalize(text):
    text = text.lower().translate(_FIX)
    text = re.sub(r"[\u064B-\u065F\u0670]", "", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


_RAW_WORDS = {
    "stop":  ["stop", "halt", "brake", "ایست", "بایست", "وایسا", "توقف", "بس", "نگه دار", "نگهدار"],
    "fwd":   ["forward", "forwards", "ahead", "straight", "جلو", "جلوتر", "مستقیم"],
    "back":  ["back", "backward", "backwards", "reverse", "عقب", "عقبتر", "پشت", "برگرد"],
    "left":  ["left", "چپ"],
    "right": ["right", "راست"],
    "spin":  ["spin", "rotate", "turn around", "بچرخ", "چرخش", "چرخ"],
    "horn":  ["horn", "beep", "honk", "بوق"],
    "light": ["light", "lights", "lamp", "چراغ", "نور", "لامپ"],
    "on":    ["on", "روشن"],
    "off":   ["off", "خاموش"],
}
WORDS = {k: [normalize(w) for w in v] for k, v in _RAW_WORDS.items()}


def _pos(padded, key):
    best = -1
    for w in WORDS[key]:
        i = padded.find(f" {w} ")
        if i != -1 and (best == -1 or i < best):
            best = i
    return best


def _pick(padded, first, second, a, b):
    p1, p2 = _pos(padded, first), _pos(padded, second)
    if p1 >= 0 and (p2 < 0 or p1 < p2):
        return a
    if p2 >= 0:
        return b
    return ""


def parse_command(text):
    t = normalize(text)
    if not t:
        return None
    padded = f" {t} "

    if _pos(padded, "stop") >= 0:
        return Command(stop=True)

    light = ""
    if _pos(padded, "light") >= 0:
        on, off = _pos(padded, "on"), _pos(padded, "off")
        if on >= 0 and (off < 0 or on < off):
            light = "on"
        elif off >= 0:
            light = "off"
        else:
            light = "toggle"

    cmd = Command(
        fb=_pick(padded, "fwd", "back", "f", "b"),
        lr=_pick(padded, "left", "right", "l", "r"),
        spin=_pos(padded, "spin") >= 0,
        horn=_pos(padded, "horn") >= 0,
        light=light,
    )
    return cmd if cmd != Command() else None


def parse_any(texts):
    for t in texts:
        cmd = parse_command(t)
        if cmd:
            return cmd
    return None


def contains_stop(texts):
    return any(_pos(f" {normalize(t)} ", "stop") >= 0 for t in texts)


def describe(cmd):
    if cmd.stop:
        return "STOP"
    names = {"f": "FWD", "b": "BACK", "l": "LEFT", "r": "RIGHT"}
    parts = [names[x] for x in (cmd.fb, cmd.lr) if x]
    if cmd.spin:
        parts.append("SPIN")
    if cmd.horn:
        parts.append("HORN")
    if cmd.light:
        parts.append(f"LIGHT {cmd.light.upper()}")
    return " ".join(parts)


if IS_ANDROID:
    try:
        from android.runnable import run_on_ui_thread
        from jnius import PythonJavaClass, autoclass, java_method

        _Activity = autoclass("org.kivy.android.PythonActivity").mActivity
        _SpeechRecognizer = autoclass("android.speech.SpeechRecognizer")
        _RecognizerIntent = autoclass("android.speech.RecognizerIntent")
        _Intent = autoclass("android.content.Intent")
    except Exception:
        IS_ANDROID = False

if not IS_ANDROID:
    def run_on_ui_thread(func):
        return func

    PythonJavaClass = object

    def java_method(signature):
        return lambda func: func


def _texts(bundle):
    try:
        lst = bundle.getStringArrayList(_SpeechRecognizer.RESULTS_RECOGNITION)
        if lst is None:
            return []
        return [str(lst.get(i)) for i in range(lst.size())]
    except Exception:
        return []


class _Listener(PythonJavaClass):
    __javainterfaces__ = ["android/speech/RecognitionListener"]
    __javacontext__ = "app"

    def __init__(self, owner):
        super().__init__()
        self.owner = owner

    @java_method("(Landroid/os/Bundle;)V")
    def onReadyForSpeech(self, params):
        self.owner._post(self.owner._on_ready)

    @java_method("()V")
    def onBeginningOfSpeech(self):
        pass

    @java_method("(F)V")
    def onRmsChanged(self, rms):
        pass

    @java_method("([B)V")
    def onBufferReceived(self, buf):
        pass

    @java_method("()V")
    def onEndOfSpeech(self):
        pass

    @java_method("(I)V")
    def onError(self, error):
        code = int(error)
        self.owner._post(lambda: self.owner._on_error(code))

    @java_method("(Landroid/os/Bundle;)V")
    def onResults(self, results):
        texts = _texts(results)
        self.owner._post(lambda: self.owner._on_results(texts, True))

    @java_method("(Landroid/os/Bundle;)V")
    def onPartialResults(self, results):
        texts = _texts(results)
        self.owner._post(lambda: self.owner._on_results(texts, False))

    @java_method("(ILandroid/os/Bundle;)V")
    def onEvent(self, event_type, params):
        pass


class AndroidSpeech:
    FATAL = {9: "no mic permission", 12: "language unsupported", 13: "language unavailable"}
    BENIGN = {6, 7}
    MAX_ERRORS = 6

    def __init__(self, on_text, on_info, on_stopped, lang="fa-IR"):
        from kivy.clock import Clock
        self._clock = Clock
        self.on_text = on_text
        self.on_info = on_info
        self.on_stopped = on_stopped
        self.lang = lang
        self._want = False
        self._rec = None
        self._listener = None
        self._restart_ev = None
        self._errors = 0

    @staticmethod
    def available():
        return IS_ANDROID

    def start(self):
        self._want = True
        from android.permissions import Permission, check_permission, request_permissions
        if check_permission(Permission.RECORD_AUDIO):
            self._begin()
        else:
            self.on_info("VOICE: allow microphone")
            request_permissions([Permission.RECORD_AUDIO], self._on_permission)

    def stop(self):
        self._want = False
        if self._restart_ev is not None:
            self._restart_ev.cancel()
            self._restart_ev = None
        if IS_ANDROID:
            self._ui_destroy()

    def _post(self, func):
        self._clock.schedule_once(lambda dt: func(), 0)

    def _on_permission(self, permissions, grants):
        granted = bool(grants) and all(grants)
        self._post(lambda: self._permission_done(granted))

    def _permission_done(self, granted):
        if not self._want:
            return
        if granted:
            self._begin()
        else:
            self._fail("VOICE: mic permission denied")

    def _begin(self):
        self._errors = 0
        self._ui_start()

    def _schedule_restart(self, delay):
        if self._restart_ev is not None:
            self._restart_ev.cancel()
        self._restart_ev = self._clock.schedule_once(self._restart, delay)

    def _restart(self, dt):
        if self._want:
            self._ui_start()

    def _fail(self, message):
        self._want = False
        if self._restart_ev is not None:
            self._restart_ev.cancel()
            self._restart_ev = None
        if IS_ANDROID:
            self._ui_destroy()
        self.on_stopped(message)

    def _on_ready(self):
        if self._want:
            self.on_info("VOICE: listening...")

    def _on_results(self, texts, final):
        if not self._want:
            return
        if texts:
            self._errors = 0
            self.on_text(texts, final)
        if final:
            self._schedule_restart(0.2)

    def _on_error(self, code):
        if not self._want:
            return
        if code in self.FATAL:
            self._fail(f"VOICE: {self.FATAL[code]}")
        elif code in self.BENIGN:
            self._schedule_restart(0.3)
        else:
            self._errors += 1
            if self._errors >= self.MAX_ERRORS:
                self._fail(f"VOICE: error {code}")
            else:
                self.on_info(f"VOICE: error {code}, retrying")
                self._schedule_restart(1.5)

    def _make_intent(self):
        intent = _Intent(_RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
        intent.putExtra(_RecognizerIntent.EXTRA_LANGUAGE_MODEL, _RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
        intent.putExtra(_RecognizerIntent.EXTRA_LANGUAGE, self.lang)
        intent.putExtra(_RecognizerIntent.EXTRA_CALLING_PACKAGE, _Activity.getPackageName())
        intent.putExtra(_RecognizerIntent.EXTRA_PARTIAL_RESULTS, True)
        intent.putExtra(_RecognizerIntent.EXTRA_MAX_RESULTS, 5)
        return intent

    @run_on_ui_thread
    def _ui_start(self):
        try:
            if self._rec is None:
                if not _SpeechRecognizer.isRecognitionAvailable(_Activity):
                    self._post(lambda: self._fail("VOICE: no speech service"))
                    return
                self._rec = _SpeechRecognizer.createSpeechRecognizer(_Activity)
                self._listener = _Listener(self)
                self._rec.setRecognitionListener(self._listener)
            self._rec.startListening(self._make_intent())
        except Exception as exc:
            message = f"VOICE: {exc}"
            self._post(lambda: self._fail(message))

    @run_on_ui_thread
    def _ui_destroy(self):
        rec, self._rec = self._rec, None
        if rec is not None:
            try:
                rec.cancel()
                rec.destroy()
            except Exception:
                pass
