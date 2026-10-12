"""
Text Extractor: a PowerToys Win+Shift+T replacement powered by RapidOCR.

Press Win+Shift+T, drag a rectangle over any text on screen, release,
and the recognized text is copied to your clipboard.

The app lives in the system tray. Right-click the icon for options,
left-click it to start a capture.

Dev setup:
    pip install rapidocr_onnxruntime pillow numpy pystray
    python text_extractor.py

Build (see the command in the chat), then share dist/TextExtractor.exe.

The hotkey uses a low-level keyboard hook, so it overrides Win+Shift+T even
when Windows (Snipping Tool) or PowerToys has claimed it. The hook does not
fire while an admin-level window has focus unless this app runs as admin too.
"""

import os
import sys

# A windowed (--noconsole) build has no stdout/stderr, and some libraries crash
# when they try to write to None. Send both to a log file instead.
if sys.stdout is None or sys.stderr is None:
    _log = open(os.path.join(os.getenv("APPDATA", "."), "TextExtractor.log"),
                "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stderr = _log

import ctypes
import queue
import threading
import tkinter as tk
import winreg
from ctypes import wintypes

import numpy as np
import pystray
from PIL import Image, ImageDraw, ImageEnhance, ImageGrab, ImageOps, ImageTk

# ---- Config -----------------------------------------------------------------
HOTKEY_VK = 0x54               # 'T' (combo is Win+Shift+T, handled in hotkey_loop)
DIM_FACTOR = 0.45              # how dark the overlay is (0 = black, 1 = normal)
MIN_SELECTION = 6              # ignore accidental clicks smaller than this (px)
APP_NAME = "TextExtractor"
ICON_FILE = "icon.png"         # bundled by the build command
# -----------------------------------------------------------------------------

# Make coordinates match physical pixels on HiDPI displays.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Low-level keyboard hook definitions
WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0100, 0x0101, 0x0104, 0x0105
LLKHF_INJECTED = 0x10


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE


# ---- Single instance --------------------------------------------------------
def ensure_single_instance():
    """Exit if another copy is already running. Returns the mutex handle to keep alive."""
    handle = kernel32.CreateMutexW(None, False, "TextExtractorMutex")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        user32.MessageBoxW(None, "Text Extractor is already running (check the system tray).",
                           "Text Extractor", 0x40)
        sys.exit(0)
    return handle


# ---- Autostart (HKCU Run key, no admin needed) ------------------------------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
SETTINGS_KEY = r"Software\TextExtractor"


def autostart_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    return f'"{pythonw}" "{os.path.abspath(__file__)}"'


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except FileNotFoundError:
        return False


def set_autostart(on):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


def first_run_setup():
    """Turn autostart on the first time the app runs. Returns True if this was the first run.

    If autostart is already on, rewrite the command so it still points at the
    exe after the user moves it.
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SETTINGS_KEY) as k:
            winreg.QueryValueEx(k, "FirstRunDone")
        if autostart_enabled():
            set_autostart(True)
        return False
    except FileNotFoundError:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, SETTINGS_KEY) as k:
            winreg.SetValueEx(k, "FirstRunDone", 0, winreg.REG_DWORD, 1)
        set_autostart(True)
        return True


# ---- Tray icon --------------------------------------------------------------
def resource_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def make_icon():
    try:
        return Image.open(resource_path(ICON_FILE)).convert("RGBA").resize((64, 64), Image.LANCZOS)
    except Exception:
        img = Image.new("RGBA", (64, 64), (32, 32, 32, 255))
        ImageDraw.Draw(img).rectangle((10, 10, 54, 54), outline=(76, 194, 255, 255), width=5)
        return img


# ---- OCR --------------------------------------------------------------------
def load_engine():
    try:
        from rapidocr_onnxruntime import RapidOCR
        return RapidOCR(), "legacy"
    except ImportError:
        from rapidocr import RapidOCR
        return RapidOCR(), "new"


def prepare(img: Image.Image, target_h: int) -> Image.Image:
    """Upscale small crops and pad them with the background colour.

    The detector struggles when text touches the image edge, so a margin matters
    as much as the upscale does for tiny selections.
    """
    if img.height < target_h:
        scale = min(target_h / img.height, 8.0)
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                         Image.BICUBIC)
    a = np.array(img)
    border = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])
    fill = tuple(int(v) for v in np.median(border, axis=0))
    pad = max(12, int(img.height * 0.6))
    return ImageOps.expand(img, border=pad, fill=fill)


def run_engine(engine, flavor, img: Image.Image) -> str:
    arr = np.array(img)[:, :, ::-1]  # RGB -> BGR
    items = []  # (box, text)
    if flavor == "legacy":
        result, _ = engine(arr)
        for box, text, _score in (result or []):
            items.append((box, text))
    else:
        out = engine(arr)
        if out.boxes is not None and out.txts is not None:
            for box, text in zip(out.boxes, out.txts):
                items.append((box, text))
    return assemble_lines(items)


def ocr_image(engine, flavor, img: Image.Image) -> str:
    img = img.convert("RGB")
    # Try a few scales; stop at the first one that finds text.
    for target_h in (96, 160, 64, 256):
        text = run_engine(engine, flavor, prepare(img, target_h))
        if text.strip():
            return text
    return ""


def assemble_lines(items) -> str:
    """Group detected boxes into lines by vertical position, then read left to right."""
    if not items:
        return ""
    rows = []
    for box, text in items:
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        rows.append({
            "cy": sum(ys) / len(ys),
            "h": max(ys) - min(ys),
            "x": min(xs),
            "text": text,
        })
    rows.sort(key=lambda r: r["cy"])

    lines, current = [], [rows[0]]
    for r in rows[1:]:
        ref = current[0]
        if abs(r["cy"] - ref["cy"]) < max(ref["h"], r["h"]) * 0.5:
            current.append(r)
        else:
            lines.append(current)
            current = [r]
    lines.append(current)

    return "\n".join(
        " ".join(r["text"] for r in sorted(line, key=lambda r: r["x"])) for line in lines
    )


# ---- UI ---------------------------------------------------------------------
class App:
    def __init__(self, first_run=False):
        self.root = tk.Tk()
        self.root.withdraw()
        self.events = queue.Queue()
        self.busy = False

        self.toast("Loading OCR model...", ms=1200)
        self.engine, self.flavor = load_engine()

        threading.Thread(target=self.hotkey_loop, daemon=True).start()

        menu = pystray.Menu(
            pystray.MenuItem("Capture text (Win+Shift+T)",
                             lambda i, m: self.events.put(("capture", None)),
                             default=True),
            pystray.MenuItem("Start with Windows",
                             lambda i, m: set_autostart(not autostart_enabled()),
                             checked=lambda m: autostart_enabled()),
            pystray.MenuItem("Quit", lambda i, m: self.events.put(("quit", None))),
        )
        self.tray = pystray.Icon(APP_NAME, make_icon(), "Text Extractor (Win+Shift+T)", menu)
        self.tray.run_detached()

        if first_run:
            self.root.after(1500, lambda: self.toast(
                "Text Extractor is running in the tray. Press Win+Shift+T.", ms=3500))

        self.root.after(50, self.poll)

    # -- hotkey thread
    def hotkey_loop(self):
        held = {"down": False}  # True while a swallowed T press is held (ignores auto-repeat)

        def proc(n_code, w_param, l_param):
            if n_code == 0:
                kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if kb.vkCode == HOTKEY_VK and not (kb.flags & LLKHF_INJECTED):
                    if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                        win = (user32.GetAsyncKeyState(0x5B) | user32.GetAsyncKeyState(0x5C)) & 0x8000
                        shift = user32.GetAsyncKeyState(0x10) & 0x8000
                        if win and shift:
                            if not held["down"]:
                                held["down"] = True
                                self.events.put(("capture", None))
                                # Dummy keypress so releasing Win doesn't open the Start menu
                                user32.keybd_event(0xE8, 0, 0, 0)
                                user32.keybd_event(0xE8, 0, 2, 0)
                            return 1  # swallow so Windows/Snipping Tool never sees it
                    elif w_param in (WM_KEYUP, WM_SYSKEYUP) and held["down"]:
                        held["down"] = False
                        return 1
            return user32.CallNextHookEx(None, n_code, w_param, l_param)

        callback = HOOKPROC(proc)  # must stay referenced for the life of the hook
        hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, callback, kernel32.GetModuleHandleW(None), 0)
        if not hook:
            self.events.put(("error", "Could not install the keyboard hook."))
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            pass

    def poll(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "capture" and not self.busy:
                    self.start_capture()
                elif kind == "error":
                    self.toast(payload, ms=4000)
                elif kind == "result":
                    self.finish(payload)
                elif kind == "quit":
                    self.tray.stop()
                    self.root.quit()
                    return
        except queue.Empty:
            pass
        self.root.after(50, self.poll)

    # -- capture overlay
    def start_capture(self):
        self.busy = True
        vx = user32.GetSystemMetrics(76)
        vy = user32.GetSystemMetrics(77)
        vw = user32.GetSystemMetrics(78)
        vh = user32.GetSystemMetrics(79)

        shot = ImageGrab.grab(bbox=(vx, vy, vx + vw, vy + vh), all_screens=True)
        dim = ImageEnhance.Brightness(shot).enhance(DIM_FACTOR)

        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.geometry(f"{vw}x{vh}+{vx}+{vy}")
        win.configure(cursor="crosshair")

        canvas = tk.Canvas(win, width=vw, height=vh, highlightthickness=0, bd=0)
        canvas.pack()
        dim_tk = ImageTk.PhotoImage(dim)
        canvas.create_image(0, 0, anchor="nw", image=dim_tk)

        state = {"x0": 0, "y0": 0, "bright": None, "bright_id": None, "rect": None}

        def on_press(e):
            state["x0"], state["y0"] = e.x, e.y

        def on_drag(e):
            x0, y0 = state["x0"], state["y0"]
            x1, y1 = e.x, e.y
            l, r = sorted((x0, x1))
            t, b = sorted((y0, y1))
            if r - l < 1 or b - t < 1:
                return
            state["bright"] = ImageTk.PhotoImage(shot.crop((l, t, r, b)))
            if state["bright_id"]:
                canvas.delete(state["bright_id"])
                canvas.delete(state["rect"])
            state["bright_id"] = canvas.create_image(l, t, anchor="nw", image=state["bright"])
            state["rect"] = canvas.create_rectangle(l, t, r, b, outline="#4cc2ff", width=2)

        def close():
            win.destroy()
            self.busy = False

        def on_release(e):
            l, r = sorted((state["x0"], e.x))
            t, b = sorted((state["y0"], e.y))
            win.destroy()
            if r - l < MIN_SELECTION or b - t < MIN_SELECTION:
                self.busy = False
                return
            crop = shot.crop((l, t, r, b))
            threading.Thread(target=self.run_ocr, args=(crop,), daemon=True).start()

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        win.bind("<Escape>", lambda e: close())
        win.bind("<Button-3>", lambda e: close())
        win._keep = (dim_tk, state)  # keep references alive
        win.focus_force()

    # -- OCR + result
    def run_ocr(self, crop):
        try:
            text = ocr_image(self.engine, self.flavor, crop)
        except Exception as exc:  # noqa: BLE001
            text = None
            self.events.put(("error", f"OCR failed: {exc}"))
        self.events.put(("result", text))

    def finish(self, text):
        self.busy = False
        if text is None:
            return
        if not text.strip():
            self.toast("No text found")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()
        preview = text.replace("\n", " ")
        if len(preview) > 80:
            preview = preview[:77] + "..."
        self.toast(f"Copied: {preview}")

    def toast(self, message, ms=1800):
        t = tk.Toplevel(self.root)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        t.attributes("-alpha", 0.92)
        tk.Label(t, text=message, bg="#202020", fg="white", padx=16, pady=10,
                 font=("Segoe UI", 10)).pack()
        t.update_idletasks()
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        x = (sw - t.winfo_width()) // 2
        y = sh - t.winfo_height() - 80
        t.geometry(f"+{x}+{y}")
        t.after(ms, t.destroy)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    _mutex = ensure_single_instance()  # keep the handle referenced until exit
    App(first_run=first_run_setup()).run()
