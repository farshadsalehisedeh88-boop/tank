"""کنترل حرکت با شیب گوشی و خواندن شتاب‌سنج (اندروید / شبیه‌ساز)."""
import math
import os
import time

IS_ANDROID = "ANDROID_ARGUMENT" in os.environ

if IS_ANDROID:
    try:
        from jnius import autoclass

        _Activity = autoclass("org.kivy.android.PythonActivity").mActivity
        _Context = autoclass("android.content.Context")
        _Sensor = autoclass("android.hardware.Sensor")
        _SensorManager = autoclass("android.hardware.SensorManager")
        _Surface = autoclass("android.view.Surface")
    except Exception:
        IS_ANDROID = False


def screen_rotation():
    """چرخش فعلی صفحه به درجه (0, 90, 180, 270)."""
    if not IS_ANDROID:
        return 0
    try:
        wm = _Activity.getSystemService(_Context.WINDOW_SERVICE)
        rot = wm.getDefaultDisplay().getRotation()
        mapping = {
            _Surface.ROTATION_0: 0,
            _Surface.ROTATION_90: 90,
            _Surface.ROTATION_180: 180,
            _Surface.ROTATION_270: 270,
        }
        return mapping.get(rot, 0)
    except Exception:
        return 0


class AccelReader:
    """خواندن شتاب‌سنج (x, y, z) به‌صورت ایمن در اندروید."""

    def __init__(self):
        self._sm = None
        self._sensor = None
        self._listener = None
        self._latest = None

    @staticmethod
    def available():
        return IS_ANDROID

    def start(self):
        if not IS_ANDROID:
            raise RuntimeError("accelerometer is only available on Android")
        if self._sm is None:
            self._sm = _Activity.getSystemService(_Context.SENSOR_SERVICE)
            self._sensor = self._sm.getDefaultSensor(_Sensor.TYPE_ACCELEROMETER)
            if self._sensor is None:
                raise RuntimeError("no accelerometer sensor found")

        from jnius import PythonJavaClass, java_method

        owner = self

        class _AccelListener(PythonJavaClass):
            __javainterfaces__ = ["android/hardware/SensorEventListener"]
            __javacontext__ = "app"

            @java_method("(Landroid/hardware/SensorEvent;)V")
            def onSensorChanged(self, event):
                if event is not None and event.values is not None and len(event.values) >= 3:
                    owner._latest = (
                        float(event.values[0]),
                        float(event.values[1]),
                        float(event.values[2]),
                    )

            @java_method("(Landroid/hardware/Sensor;I)V")
            def onAccuracyChanged(self, sensor, accuracy):
                pass

        if self._listener is None:
            self._listener = _AccelListener()
        self._sm.registerListener(self._listener, self._sensor, _SensorManager.SENSOR_DELAY_GAME)

    def read(self):
        return self._latest

    def stop(self):
        if self._sm is not None and self._listener is not None:
            try:
                self._sm.unregisterListener(self._listener)
            except Exception:
                pass
        self._listener = None
        self._latest = None


class TiltController:
    """
    تبدیل شیب گوشی به جهت حرکت ربات با کالیبراسیون خودکار اولیه.
    خروجی: (fb, lr) که fb در {"", "f", "b"} و lr در {"", "l", "r"} است.
    """

    def __init__(self, pitch_thresh=14.0, roll_thresh=14.0, cal_sec=0.8):
        self.pitch_thresh = float(pitch_thresh)
        self.roll_thresh = float(roll_thresh)
        self.cal_sec = float(cal_sec)
        self.calibrated = False
        self._cal_start = None
        self._samples = []
        self._base_pitch = 0.0
        self._base_roll = 0.0

    def recalibrate(self):
        self.calibrated = False
        self._cal_start = time.monotonic()
        self._samples = []

    @staticmethod
    def _angles(ax, ay, az, rot_deg):
        if rot_deg == 90:
            x, y = -ay, ax
        elif rot_deg == 180:
            x, y = -ax, -ay
        elif rot_deg == 270:
            x, y = ay, -ax
        else:
            x, y = ax, ay
        pitch = math.degrees(math.atan2(y, math.sqrt(x * x + az * az)))
        roll = math.degrees(math.atan2(x, math.sqrt(y * y + az * az)))
        return pitch, roll

    def update(self, ax, ay, az, rot_deg=0):
        pitch, roll = self._angles(ax, ay, az, rot_deg)
        now = time.monotonic()

        if not self.calibrated:
            if self._cal_start is None:
                self._cal_start = now
            self._samples.append((pitch, roll))
            if now - self._cal_start >= self.cal_sec:
                n = len(self._samples)
                self._base_pitch = sum(p for p, _ in self._samples) / n
                self._base_roll = sum(r for _, r in self._samples) / n
                self.calibrated = True
            return "", ""

        dp_pitch = pitch - self._base_pitch
        dp_roll = roll - self._base_roll

        fb = ""
        if dp_pitch >= self.pitch_thresh:
            fb = "f"
        elif dp_pitch <= -self.pitch_thresh:
            fb = "b"

        lr = ""
        if dp_roll <= -self.roll_thresh:
            lr = "l"
        elif dp_roll >= self.roll_thresh:
            lr = "r"

        return fb, lr
