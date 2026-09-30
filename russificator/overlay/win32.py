"""Windows API для оверлея (ctypes): окна, захват экрана, прозрачное окно, трей, горячие клавиши.

Всё, что касается окон, живёт в одном GUI-потоке (:class:`Gui`) с циклом
сообщений; другие потоки общаются с ним через ``PostMessage`` — так Windows
требует для окон, трея и глобальных горячих клавиш.

Окно оверлея — многослойное (per-pixel alpha), поверх всех, прозрачное для
мыши и не забирающее фокус.

Кадр игры снимается с самого окна игры (:func:`capture_window`, ``PrintWindow``
с ``PW_RENDERFULLCONTENT``): в него не попадают ни наши плашки, ни окна поверх
игры. Запасной путь — снимок области экрана (:func:`capture`); в нём плашки
видны, если Windows не умеет исключать окно из захвата (``WDA_EXCLUDEFROMCAPTURE``
не работает для per-pixel alpha окон в Windows 10), поэтому служба закрашивает
их место перед распознаванием.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

log = logging.getLogger("russificator.overlay.win32")

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
HCURSOR = HICON = HBRUSH = HMENU = HBITMAP = HGDIOBJ = ctypes.c_void_p
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM) \
    if IS_WINDOWS else None

WM_DESTROY, WM_CLOSE, WM_HOTKEY, WM_NULL = 0x0002, 0x0010, 0x0312, 0x0000
WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MOUSEMOVE, WM_RBUTTONUP, WM_CONTEXTMENU = 0x0201, 0x0202, 0x0200, 0x0205, 0x007B
WM_LBUTTONDBLCLK, WM_KEYDOWN, WM_SETCURSOR = 0x0203, 0x0100, 0x0020
WM_APP = 0x8000
MSG_RENDER, MSG_TRAY, MSG_HIDE, MSG_QUIT, MSG_HOTKEYS, MSG_SELECT, MSG_BALLOON, MSG_TIP, MSG_AFFINITY = \
    range(WM_APP + 1, WM_APP + 10)

WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = \
    0x00080000, 0x00000020, 0x00000008, 0x00000080, 0x08000000
GWL_EXSTYLE = -20
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x1, 0x2, 0x10, 0x40
ULW_ALPHA = 0x2
AC_SRC_OVER, AC_SRC_ALPHA = 0x0, 0x1
SRCCOPY, CAPTUREBLT = 0x00CC0020, 0x40000000
PW_CLIENTONLY, PW_RENDERFULLCONTENT = 0x1, 0x2
DIB_RGB_COLORS, BI_RGB = 0, 0
WDA_NONE, WDA_EXCLUDEFROMCAPTURE = 0x0, 0x11
LWA_COLORKEY, LWA_ALPHA = 0x1, 0x2
#: прозрачный цвет окна оверлея (COLORREF 0x00BBGGRR): R=254, G=0, B=255 — в картинках почти не
#: встречается, а совпавший пиксель картинки сдвигается на единицу (render.to_colorkey)
COLOR_KEY = 0x00FF00FE
WM_PAINT, WM_ERASEBKGND = 0x000F, 0x0014
#: непрозрачность затемнения при выборе области (окно с цветовым ключом)
SELECT_ALPHA = 150
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
VK_ESCAPE = 0x1B
IDC_CROSS, IDC_ARROW = 32515, 32512

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
MF_STRING, MF_SEPARATOR, MF_CHECKED, MF_GRAYED = 0x0, 0x800, 0x8, 0x1
TPM_RETURNCMD, TPM_NONOTIFY, TPM_RIGHTBUTTON = 0x100, 0x80, 0x2
IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x10, 0x40


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte), ("SourceConstantAlpha", ctypes.c_ubyte),
                ("AlphaFormat", ctypes.c_ubyte)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", HICON),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD), ("guidItem", GUID), ("hBalloonIcon", HICON)]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", HICON), ("hCursor", HCURSOR), ("hbrBackground", HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", HICON)]


def _proto(dll, name, res, *args):
    fn = getattr(dll, name)
    fn.restype = res
    fn.argtypes = list(args)
    return fn


if IS_WINDOWS:
    H = wintypes.HANDLE
    GetForegroundWindow = _proto(user32, "GetForegroundWindow", wintypes.HWND)
    GetWindowThreadProcessId = _proto(user32, "GetWindowThreadProcessId", wintypes.DWORD, wintypes.HWND,
                                      ctypes.POINTER(wintypes.DWORD))
    GetWindowTextW = _proto(user32, "GetWindowTextW", ctypes.c_int, wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    GetWindowRect = _proto(user32, "GetWindowRect", wintypes.BOOL, wintypes.HWND, ctypes.POINTER(RECT))
    GetClientRect = _proto(user32, "GetClientRect", wintypes.BOOL, wintypes.HWND, ctypes.POINTER(RECT))
    ClientToScreen = _proto(user32, "ClientToScreen", wintypes.BOOL, wintypes.HWND, ctypes.POINTER(POINT))
    IsIconic = _proto(user32, "IsIconic", wintypes.BOOL, wintypes.HWND)
    IsWindowVisible = _proto(user32, "IsWindowVisible", wintypes.BOOL, wintypes.HWND)
    MonitorFromWindow = _proto(user32, "MonitorFromWindow", H, wintypes.HWND, wintypes.DWORD)
    GetMonitorInfoW = _proto(user32, "GetMonitorInfoW", wintypes.BOOL, H, ctypes.POINTER(MONITORINFO))
    GetDC = _proto(user32, "GetDC", wintypes.HDC, wintypes.HWND)
    ReleaseDC = _proto(user32, "ReleaseDC", ctypes.c_int, wintypes.HWND, wintypes.HDC)
    CreateCompatibleDC = _proto(gdi32, "CreateCompatibleDC", wintypes.HDC, wintypes.HDC)
    DeleteDC = _proto(gdi32, "DeleteDC", wintypes.BOOL, wintypes.HDC)
    CreateCompatibleBitmap = _proto(gdi32, "CreateCompatibleBitmap", HBITMAP, wintypes.HDC, ctypes.c_int, ctypes.c_int)
    CreateDIBSection = _proto(gdi32, "CreateDIBSection", HBITMAP, wintypes.HDC, ctypes.POINTER(BITMAPINFO),
                              wintypes.UINT, ctypes.POINTER(ctypes.c_void_p), H, wintypes.DWORD)
    SelectObject = _proto(gdi32, "SelectObject", HGDIOBJ, wintypes.HDC, HGDIOBJ)
    DeleteObject = _proto(gdi32, "DeleteObject", wintypes.BOOL, HGDIOBJ)
    BitBlt = _proto(gdi32, "BitBlt", wintypes.BOOL, wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                    ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD)
    GetDIBits = _proto(gdi32, "GetDIBits", ctypes.c_int, wintypes.HDC, HBITMAP, wintypes.UINT, wintypes.UINT,
                       ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), wintypes.UINT)
    OpenProcess = _proto(kernel32, "OpenProcess", H, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    CloseHandle = _proto(kernel32, "CloseHandle", wintypes.BOOL, H)
    QueryFullProcessImageNameW = _proto(kernel32, "QueryFullProcessImageNameW", wintypes.BOOL, H, wintypes.DWORD,
                                        wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    GetModuleHandleW = _proto(kernel32, "GetModuleHandleW", wintypes.HMODULE, wintypes.LPCWSTR)
    RegisterClassExW = _proto(user32, "RegisterClassExW", wintypes.ATOM, ctypes.POINTER(WNDCLASSEXW))
    UnregisterClassW = _proto(user32, "UnregisterClassW", wintypes.BOOL, wintypes.LPCWSTR, wintypes.HINSTANCE)
    CreateWindowExW = _proto(user32, "CreateWindowExW", wintypes.HWND, wintypes.DWORD, wintypes.LPCWSTR,
                             wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             wintypes.HWND, HMENU, wintypes.HINSTANCE, ctypes.c_void_p)
    DestroyWindow = _proto(user32, "DestroyWindow", wintypes.BOOL, wintypes.HWND)
    DefWindowProcW = _proto(user32, "DefWindowProcW", LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                            wintypes.LPARAM)
    GetMessageW = _proto(user32, "GetMessageW", wintypes.BOOL, ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                         wintypes.UINT, wintypes.UINT)
    TranslateMessage = _proto(user32, "TranslateMessage", wintypes.BOOL, ctypes.POINTER(wintypes.MSG))
    DispatchMessageW = _proto(user32, "DispatchMessageW", LRESULT, ctypes.POINTER(wintypes.MSG))
    PostMessageW = _proto(user32, "PostMessageW", wintypes.BOOL, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                          wintypes.LPARAM)
    PostQuitMessage = _proto(user32, "PostQuitMessage", None, ctypes.c_int)
    ShowWindow = _proto(user32, "ShowWindow", wintypes.BOOL, wintypes.HWND, ctypes.c_int)
    SetWindowPos = _proto(user32, "SetWindowPos", wintypes.BOOL, wintypes.HWND, wintypes.HWND, ctypes.c_int,
                          ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT)
    UpdateLayeredWindow = _proto(user32, "UpdateLayeredWindow", wintypes.BOOL, wintypes.HWND, wintypes.HDC,
                                 ctypes.POINTER(POINT), ctypes.POINTER(SIZE), wintypes.HDC, ctypes.POINTER(POINT),
                                 wintypes.COLORREF, ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD)
    GetWindowLongW = _proto(user32, "GetWindowLongW", wintypes.LONG, wintypes.HWND, ctypes.c_int)
    SetWindowLongW = _proto(user32, "SetWindowLongW", wintypes.LONG, wintypes.HWND, ctypes.c_int, wintypes.LONG)
    RegisterHotKey = _proto(user32, "RegisterHotKey", wintypes.BOOL, wintypes.HWND, ctypes.c_int, wintypes.UINT,
                            wintypes.UINT)
    UnregisterHotKey = _proto(user32, "UnregisterHotKey", wintypes.BOOL, wintypes.HWND, ctypes.c_int)
    Shell_NotifyIconW = _proto(shell32, "Shell_NotifyIconW", wintypes.BOOL, wintypes.DWORD,
                               ctypes.POINTER(NOTIFYICONDATAW))
    LoadImageW = _proto(user32, "LoadImageW", H, wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int,
                        ctypes.c_int, wintypes.UINT)
    LoadCursorW = _proto(user32, "LoadCursorW", HCURSOR, wintypes.HINSTANCE, ctypes.c_void_p)
    SetCursor = _proto(user32, "SetCursor", HCURSOR, HCURSOR)
    CreatePopupMenu = _proto(user32, "CreatePopupMenu", HMENU)
    AppendMenuW = _proto(user32, "AppendMenuW", wintypes.BOOL, HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR)
    TrackPopupMenu = _proto(user32, "TrackPopupMenu", wintypes.BOOL, HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                            ctypes.c_int, wintypes.HWND, ctypes.c_void_p)
    DestroyMenu = _proto(user32, "DestroyMenu", wintypes.BOOL, HMENU)
    GetCursorPos = _proto(user32, "GetCursorPos", wintypes.BOOL, ctypes.POINTER(POINT))
    SetForegroundWindow = _proto(user32, "SetForegroundWindow", wintypes.BOOL, wintypes.HWND)
    SetCapture = _proto(user32, "SetCapture", wintypes.HWND, wintypes.HWND)
    ReleaseCapture = _proto(user32, "ReleaseCapture", wintypes.BOOL)
    RegisterWindowMessageW = _proto(user32, "RegisterWindowMessageW", wintypes.UINT, wintypes.LPCWSTR)
    PrintWindow = _proto(user32, "PrintWindow", wintypes.BOOL, wintypes.HWND, wintypes.HDC, wintypes.UINT)
    SetLayeredWindowAttributes = _proto(user32, "SetLayeredWindowAttributes", wintypes.BOOL, wintypes.HWND,
                                        wintypes.COLORREF, ctypes.c_ubyte, wintypes.DWORD)
    GdiFlush = _proto(gdi32, "GdiFlush", wintypes.BOOL)
    SetDIBitsToDevice = _proto(gdi32, "SetDIBitsToDevice", ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int,
                               wintypes.DWORD, wintypes.DWORD, ctypes.c_int, ctypes.c_int, wintypes.UINT,
                               wintypes.UINT, ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), wintypes.UINT)
    ValidateRect = _proto(user32, "ValidateRect", wintypes.BOOL, wintypes.HWND, ctypes.c_void_p)
    try:
        SetWindowDisplayAffinity = _proto(user32, "SetWindowDisplayAffinity", wintypes.BOOL, wintypes.HWND,
                                          wintypes.DWORD)
    except AttributeError:
        SetWindowDisplayAffinity = None


def set_dpi_aware() -> None:
    """Физические пиксели: координаты окон, захват и оверлей в одной системе."""
    if not IS_WINDOWS:
        return
    try:
        ctx = ctypes.c_void_p(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if user32.SetProcessDpiAwarenessContext(ctx):
            return
    except AttributeError:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except (AttributeError, OSError):
        pass
    try:
        user32.SetProcessDPIAware()
    except AttributeError:
        pass


# ------------------------------------------------------------------ окна

@dataclass
class WindowInfo:
    hwnd: int
    pid: int
    exe: str
    title: str
    client: Tuple[int, int, int, int]      # x, y, w, h клиентской области на экране
    window: Tuple[int, int, int, int]      # left, top, right, bottom окна
    monitor: Tuple[int, int, int, int]     # left, top, right, bottom монитора
    minimized: bool

    @property
    def fullscreen(self) -> bool:
        """Окно на весь монитор: полный экран или «окно без рамки»."""
        ml, mt, mr, mb = self.monitor
        x, y, w, h = self.client
        wl, wt, wr, wb = self.window
        covers_client = abs(x - ml) <= 2 and abs(y - mt) <= 2 and abs(x + w - mr) <= 2 and abs(y + h - mb) <= 2
        covers_window = wl <= ml + 1 and wt <= mt + 1 and wr >= mr - 1 and wb >= mb - 1
        return covers_client or covers_window


def process_exe(pid: int) -> str:
    if not IS_WINDOWS or not pid:
        return ""
    h = OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
    finally:
        CloseHandle(h)
    return ""


def window_info(hwnd: int) -> Optional[WindowInfo]:
    if not IS_WINDOWS or not hwnd:
        return None
    pid = wintypes.DWORD()
    GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    title = ctypes.create_unicode_buffer(256)
    GetWindowTextW(hwnd, title, 256)
    wr, cr = RECT(), RECT()
    if not GetWindowRect(hwnd, ctypes.byref(wr)) or not GetClientRect(hwnd, ctypes.byref(cr)):
        return None
    pt = POINT(0, 0)
    ClientToScreen(hwnd, ctypes.byref(pt))
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    mon = MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    if mon and GetMonitorInfoW(mon, ctypes.byref(mi)):
        m = (mi.rcMonitor.left, mi.rcMonitor.top, mi.rcMonitor.right, mi.rcMonitor.bottom)
    else:
        m = (0, 0, 0, 0)
    return WindowInfo(hwnd=int(hwnd), pid=pid.value, exe=process_exe(pid.value), title=title.value,
                      client=(pt.x, pt.y, cr.right - cr.left, cr.bottom - cr.top),
                      window=(wr.left, wr.top, wr.right, wr.bottom), monitor=m, minimized=bool(IsIconic(hwnd)))


def foreground() -> Optional[WindowInfo]:
    if not IS_WINDOWS:
        return None
    return window_info(GetForegroundWindow())


def own_exes() -> List[str]:
    """Пути exe нашего процесса: запущенный из исходников через .venv Python на самом деле работает
    как базовый pythonw.exe (venv лишь перенаправляет), поэтому годятся оба."""
    out = {os.path.normcase(os.path.abspath(sys.executable))}
    base = getattr(sys, "_base_executable", "")
    if base:
        out.add(os.path.normcase(os.path.abspath(base)))
    if IS_WINDOWS:
        real = process_exe(os.getpid())
        if real:
            out.add(os.path.normcase(os.path.abspath(real)))
    for p in list(out):                       # python.exe и pythonw.exe — одна и та же программа
        d, name = os.path.split(p)
        if name in ("python.exe", "pythonw.exe"):
            out.update({os.path.join(d, "python.exe"), os.path.join(d, "pythonw.exe")})
    return sorted(out)


def find_program_window(title_prefix: str) -> Optional[int]:
    """Видимое окно нашей же программы (тот же exe) с заголовком, начинающимся с ``title_prefix``.

    Проверка exe нужна, чтобы не спутать с папкой «Русификатор игр», открытой в Проводнике."""
    if not IS_WINDOWS:
        return None
    own = set(own_exes())
    found: List[int] = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        try:
            buf = ctypes.create_unicode_buffer(256)
            GetWindowTextW(hwnd, buf, 256)
            if buf.value.startswith(title_prefix) and IsWindowVisible(hwnd):
                pid = wintypes.DWORD()
                GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                exe = process_exe(pid.value)
                if exe and os.path.normcase(os.path.abspath(exe)) in own:
                    found.append(int(hwnd))
                    return False
        except Exception:  # noqa: BLE001
            pass
        return True
    user32.EnumWindows(proto(cb), 0)
    return found[0] if found else None


def activate(hwnd: int) -> None:
    """Развернуть (если свёрнуто) и вывести окно на передний план."""
    if not IS_WINDOWS or not hwnd:
        return
    if IsIconic(hwnd):
        ShowWindow(hwnd, 9)          # SW_RESTORE
    else:
        ShowWindow(hwnd, 5)          # SW_SHOW
    SetForegroundWindow(hwnd)


_instance_mutex = None


def single_instance(name: str) -> bool:
    """True — мы первая копия (именованный мьютекс Windows держится до выхода из процесса)."""
    global _instance_mutex
    if not IS_WINDOWS:
        return True
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    h = k32.CreateMutexW(None, False, name)
    if not h:
        return True
    if ctypes.get_last_error() == 183:   # ERROR_ALREADY_EXISTS
        CloseHandle(h)
        return False
    _instance_mutex = h
    return True


# ------------------------------------------------------------------ захват экрана

def _read_bitmap(mem, bmp, w: int, h: int) -> Optional[bytes]:
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h          # сверху вниз
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = BI_RGB
    buf = ctypes.create_string_buffer(w * h * 4)
    if GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bmi), DIB_RGB_COLORS) != h:
        return None
    return buf.raw


def capture_window(hwnd: int, cw: int, ch: int, region: Tuple[int, int, int, int]) -> Optional[bytes]:
    """Пиксели части клиентской области окна (BGRA, сверху вниз), снятые с самого окна.

    ``region`` — (x, y, w, h) внутри клиентской области размера ``cw`` × ``ch``. Окна поверх
    (и наш оверлей) в кадр не попадают. None — окно так не снимается (тогда — :func:`capture`)."""
    if not IS_WINDOWS or cw <= 0 or ch <= 0:
        return None
    rx, ry, w, h = region
    rx, ry = max(0, rx), max(0, ry)
    w, h = min(w, cw - rx), min(h, ch - ry)
    if w <= 0 or h <= 0:
        return None
    screen = GetDC(None)
    if not screen:
        return None
    mem = CreateCompatibleDC(screen)
    bmp = CreateCompatibleBitmap(screen, cw, ch)
    old = SelectObject(mem, bmp)
    try:
        if not PrintWindow(hwnd, mem, PW_CLIENTONLY | PW_RENDERFULLCONTENT):
            return None
        SelectObject(mem, old)
        old = None
        raw = _read_bitmap(mem, bmp, cw, ch)
    finally:
        if old is not None:
            SelectObject(mem, old)
        DeleteObject(bmp)
        DeleteDC(mem)
        ReleaseDC(None, screen)
    if raw is None:
        return None
    if (rx, ry, w, h) == (0, 0, cw, ch):
        return raw
    stride = cw * 4
    return b"".join(raw[(ry + row) * stride + rx * 4:(ry + row) * stride + (rx + w) * 4] for row in range(h))


def capture(x: int, y: int, w: int, h: int) -> Optional[bytes]:
    """Пиксели области экрана (BGRA, сверху вниз). Окна, исключённые из захвата, в кадр не попадают."""
    if not IS_WINDOWS or w <= 0 or h <= 0:
        return None
    screen = GetDC(None)
    if not screen:
        return None
    mem = CreateCompatibleDC(screen)
    bmp = CreateCompatibleBitmap(screen, w, h)
    old = SelectObject(mem, bmp)
    try:
        if not BitBlt(mem, 0, 0, w, h, screen, x, y, SRCCOPY | CAPTUREBLT):
            return None
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h          # сверху вниз
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        buf = ctypes.create_string_buffer(w * h * 4)
        SelectObject(mem, old)
        old = None
        if GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bmi), DIB_RGB_COLORS) != h:
            return None
        return buf.raw
    finally:
        if old is not None:
            SelectObject(mem, old)
        DeleteObject(bmp)
        DeleteDC(mem)
        ReleaseDC(None, screen)


class Grabber:
    """Кадры в один и тот же буфер (DIB-секцию) — без выделения памяти и копирования на каждый кадр.

    Результат — массив numpy (высота, ширина, 4) BGRA поверх буфера: он действителен до следующего
    снимка (кому кадр нужен дольше — копирует). Буфер пересоздаётся при смене размера."""

    def __init__(self) -> None:
        self._mem = None
        self._dib = None
        self._old = None
        self._bits = None
        self._size = (0, 0)
        self._lock = threading.Lock()

    def _ensure(self, w: int, h: int) -> bool:
        if (w, h) == self._size and self._mem:
            return True
        self._free()
        screen = GetDC(None)
        if not screen:
            return False
        try:
            mem = CreateCompatibleDC(screen)
            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = w
            bmi.bmiHeader.biHeight = -h                 # сверху вниз
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = BI_RGB
            bits = ctypes.c_void_p()
            dib = CreateDIBSection(screen, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
            if not mem or not dib or not bits.value:
                if dib:
                    DeleteObject(dib)
                if mem:
                    DeleteDC(mem)
                return False
            self._mem, self._dib, self._bits, self._size = mem, dib, bits.value, (w, h)
            self._old = SelectObject(mem, dib)
            return True
        finally:
            ReleaseDC(None, screen)

    def _free(self) -> None:
        if self._mem:
            if self._old is not None:
                SelectObject(self._mem, self._old)
            DeleteDC(self._mem)
        if self._dib:
            DeleteObject(self._dib)
        self._mem = self._dib = self._old = self._bits = None
        self._size = (0, 0)

    def _view(self, w: int, h: int):
        import numpy as np
        GdiFlush()
        buf = (ctypes.c_ubyte * (w * h * 4)).from_address(self._bits)
        return np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)

    def window(self, hwnd: int, cw: int, ch: int):
        """Клиентская область окна (как ``capture_window``) или None."""
        if not IS_WINDOWS or cw <= 0 or ch <= 0:
            return None
        with self._lock:
            if not self._ensure(cw, ch) or not PrintWindow(hwnd, self._mem, PW_CLIENTONLY | PW_RENDERFULLCONTENT):
                return None
            return self._view(cw, ch)

    def screen(self, x: int, y: int, w: int, h: int):
        """Область экрана (как ``capture``) или None."""
        if not IS_WINDOWS or w <= 0 or h <= 0:
            return None
        with self._lock:
            if not self._ensure(w, h):
                return None
            screen = GetDC(None)
            if not screen:
                return None
            try:
                if not BitBlt(self._mem, 0, 0, w, h, screen, x, y, SRCCOPY | CAPTUREBLT):
                    return None
            finally:
                ReleaseDC(None, screen)
            return self._view(w, h)

    def close(self) -> None:
        with self._lock:
            self._free()


_grabbers: Dict[str, Grabber] = {}


def _grabber(kind: str) -> Grabber:
    g = _grabbers.get(kind)
    if g is None:
        g = _grabbers[kind] = Grabber()
    return g


def grab_window(hwnd: int, cw: int, ch: int, region: Tuple[int, int, int, int]):
    """Часть клиентской области окна массивом numpy (высота, ширина, 4), BGRA; None — не снимается.
    Массив действителен до следующего снимка."""
    img = _grabber("window").window(hwnd, cw, ch)
    if img is None:
        return None
    rx, ry, w, h = region
    rx, ry = max(0, rx), max(0, ry)
    return img[ry:ry + h, rx:rx + w]


def grab_screen(x: int, y: int, w: int, h: int):
    """Область экрана массивом numpy (высота, ширина, 4), BGRA; None — не снимается."""
    return _grabber("screen").screen(x, y, w, h)


# ------------------------------------------------------------------ автозапуск

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "RussificatorLive"


def set_autostart(command: Optional[str]) -> bool:
    """Записать (или убрать при None) автозапуск живого перевода вместе с Windows."""
    if not IS_WINDOWS:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if command:
                winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(k, RUN_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError as exc:
        log.warning("автозапуск не изменён: %s", exc)
        return False


def autostart_command() -> Optional[str]:
    if not IS_WINDOWS:
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            v, _ = winreg.QueryValueEx(k, RUN_NAME)
            return str(v)
    except OSError:
        return None


# ------------------------------------------------------------------ GUI-поток: оверлей, трей, клавиши

@dataclass
class Hotkey:
    id: int
    mods: int
    vk: int
    name: str


class Gui:
    """Окно оверлея, скрытое окно трея и горячие клавиши — всё в одном потоке с циклом сообщений.

    Колбэки (``on_hotkey(name)``, ``on_menu(cmd)``, ``on_region(rect|None)``) вызываются из
    GUI-потока — долгую работу в них делать нельзя.
    """

    CLASS = "RussificatorOverlay"

    def __init__(self, icon_path: Optional[str], on_hotkey: Callable[[str], None],
                 on_menu: Callable[[str], None], on_region: Callable[[Optional[Tuple[int, int, int, int]]], None],
                 menu_items: Callable[[], List[Tuple[str, str, bool]]], tip: str = "Русификатор — живой перевод"):
        self.icon_path = icon_path
        self.on_hotkey = on_hotkey
        self.on_menu = on_menu
        self.on_region = on_region
        self.menu_items = menu_items
        self.tip = tip
        self.overlay = None
        self.host = None
        self._proc = None
        self._icon = None
        self._frame: Optional[Tuple[int, int, int, int, bytes]] = None
        self._frame_lock = threading.Lock()
        self._hotkeys: Dict[int, Hotkey] = {}
        self._wanted: List[Hotkey] = []
        self._ready = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._select: Optional[dict] = None
        self._balloon: Tuple[str, str] = ("", "")
        self._taskbar_created = 0
        self.excluded_from_capture = False
        #: окно с цветовым ключом (картинка — непрозрачная BGRA, прозрачное — COLOR_KEY), иначе —
        #: попиксельная альфа (премультиплицированная BGRA для UpdateLayeredWindow)
        self.colorkey = False
        self.can_exclude = False                    # Windows умеет исключать окно оверлея из захвата
        self.visible = False
        self.busy_hotkeys: List[str] = []           # клавиши, которые заняты другой программой

    # --- запуск/остановка (из любого потока)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="overlay-gui", daemon=True)
        self._thread.start()
        self._ready.wait(10)

    def stop(self) -> None:
        if self.host:
            PostMessageW(self.host, MSG_QUIT, 0, 0)
        if self._thread:
            self._thread.join(5)

    def show_frame(self, x: int, y: int, w: int, h: int, bgra: bytes) -> None:
        """Показать картинку в области экрана: BGRA, премультиплицированная (окно с попиксельной
        альфой) или непрозрачная с COLOR_KEY на прозрачных местах (``colorkey``)."""
        with self._frame_lock:
            self._frame = (x, y, w, h, bgra)
        if self.overlay:
            PostMessageW(self.overlay, MSG_RENDER, 0, 0)

    def hide(self) -> None:
        if self.overlay:
            PostMessageW(self.overlay, MSG_HIDE, 0, 0)

    def set_excluded(self, on: bool) -> None:
        """Исключить окно перевода из захвата экрана (или вернуть в захват)."""
        if not self.can_exclude or on == self.excluded_from_capture:
            return
        self.excluded_from_capture = on             # сразу: следующий кадр уже решает по нему
        if self.overlay:
            PostMessageW(self.overlay, MSG_AFFINITY, 1 if on else 0, 0)

    def set_hotkeys(self, keys: List[Hotkey]) -> None:
        self._wanted = list(keys)
        if self.host:
            PostMessageW(self.host, MSG_HOTKEYS, 0, 0)

    def select_region(self, x: int, y: int, w: int, h: int) -> None:
        """Режим выбора области мышью поверх окна игры (Esc — отмена)."""
        self._select = {"area": (x, y, w, h), "start": None, "cur": None}
        if self.overlay:
            PostMessageW(self.overlay, MSG_SELECT, 0, 0)

    def balloon(self, title: str, text: str) -> None:
        self._balloon = (title, text)
        if self.host:
            PostMessageW(self.host, MSG_BALLOON, 0, 0)

    def set_tip(self, tip: str) -> None:
        self.tip = tip[:120]
        if self.host:
            PostMessageW(self.host, MSG_TIP, 0, 0)

    # --- поток окон

    def _run(self) -> None:
        try:
            self._create()
        except Exception:  # noqa: BLE001
            log.exception("окна оверлея не созданы")
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        while GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            TranslateMessage(ctypes.byref(msg))
            DispatchMessageW(ctypes.byref(msg))
        self._cleanup()
        self.host = self.overlay = None
        try:
            UnregisterClassW(self._class, self._hinst)
        except Exception:  # noqa: BLE001
            pass

    def _create(self) -> None:
        hinst = GetModuleHandleW(None)
        self._hinst = hinst
        self._proc = WNDPROC(self._wndproc)
        # своё имя класса у каждого объекта: класс с тем же именем от прошлого объекта указывал бы
        # на его (уже удалённую) оконную процедуру
        self._class = f"{self.CLASS}.{os.getpid()}.{id(self):x}"
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = ctypes.cast(self._proc, ctypes.c_void_p)
        wc.hInstance = hinst
        wc.hCursor = LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
        wc.lpszClassName = self._class
        if not RegisterClassExW(ctypes.byref(wc)):
            raise OSError(f"RegisterClassEx: {ctypes.get_last_error()}")
        self.host = CreateWindowExW(WS_EX_TOOLWINDOW, self._class, "Russificator Live", WS_POPUP,
                                    0, 0, 0, 0, None, None, hinst, None)
        self.overlay = CreateWindowExW(WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW
                                       | WS_EX_NOACTIVATE, self._class, "Russificator Overlay", WS_POPUP,
                                       0, 0, 1, 1, None, None, hinst, None)
        if not self.host or not self.overlay:
            raise OSError(f"CreateWindowEx: {ctypes.get_last_error()}")
        # Окно с цветовым ключом Windows 10 (2004+) умеет исключать из захвата экрана, а окно
        # с попиксельной альфой (UpdateLayeredWindow) — нет. Исключённый оверлей не попадает в
        # снимок экрана: снимок показывает игру под переводом — так снимаются полноэкранные игры, и
        # не нужно закрашивать свои плашки. Исключение включается, только пока игру приходится
        # снимать с экрана (:meth:`set_excluded`): иначе перевода не было бы на скриншотах и в
        # записи экрана игрока.
        if SetWindowDisplayAffinity is not None and SetLayeredWindowAttributes(self.overlay, COLOR_KEY, 255,
                                                                                LWA_COLORKEY):
            if SetWindowDisplayAffinity(self.overlay, WDA_EXCLUDEFROMCAPTURE):
                SetWindowDisplayAffinity(self.overlay, WDA_NONE)
                self.colorkey = self.can_exclude = True
            else:
                # старая Windows: цветовой ключ ничего не даёт — обычное окно с попиксельной альфой
                DestroyWindow(self.overlay)
                self.overlay = CreateWindowExW(WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW
                                               | WS_EX_NOACTIVATE, self._class, "Russificator Overlay", WS_POPUP,
                                               0, 0, 1, 1, None, None, hinst, None)
        if not self.can_exclude:
            log.info("окно оверлея нельзя исключить из захвата экрана — при съёмке экрана плашки закрашиваются")
        self._taskbar_created = RegisterWindowMessageW("TaskbarCreated")
        self._tray(NIM_ADD)

    def _cleanup(self) -> None:
        try:
            self._tray(NIM_DELETE)
        except Exception:  # noqa: BLE001
            pass
        for hid in list(self._hotkeys):
            UnregisterHotKey(self.host, hid)
        self._hotkeys.clear()

    def _tray(self, action: int) -> None:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.host
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_TIP | NIF_ICON
        nid.uCallbackMessage = MSG_TRAY
        if self._icon is None and self.icon_path and os.path.isfile(self.icon_path):
            self._icon = LoadImageW(None, self.icon_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
        nid.hIcon = self._icon
        nid.szTip = self.tip[:127]
        if action == NIM_MODIFY and self._balloon[1]:
            nid.uFlags |= NIF_INFO
            nid.szInfoTitle = self._balloon[0][:63]
            nid.szInfo = self._balloon[1][:255]
            nid.dwInfoFlags = 0x1  # NIIF_INFO
            self._balloon = ("", "")
        Shell_NotifyIconW(action, ctypes.byref(nid))

    def _apply_hotkeys(self) -> None:
        wanted = {(k.mods, k.vk): k for k in self._wanted}
        for hid, hk in list(self._hotkeys.items()):
            if (hk.mods, hk.vk) not in wanted or wanted[(hk.mods, hk.vk)].name != hk.name:
                UnregisterHotKey(self.host, hid)
                del self._hotkeys[hid]
        have = {(hk.mods, hk.vk) for hk in self._hotkeys.values()}
        busy = [n for n in self.busy_hotkeys if any(k.name == n for k in self._wanted)]
        for key, hk in wanted.items():
            if key in have:
                continue
            if RegisterHotKey(self.host, hk.id, hk.mods | MOD_NOREPEAT, hk.vk):
                self._hotkeys[hk.id] = hk
                if hk.name in busy:
                    busy.remove(hk.name)
            elif hk.name not in busy:
                busy.append(hk.name)
                log.warning("горячая клавиша %s занята другой программой", hk.name)
        self.busy_hotkeys = busy

    def _render(self) -> None:
        with self._frame_lock:
            frame = self._frame
        if frame is None:
            return
        x, y, w, h, data = frame
        if w <= 0 or h <= 0 or len(data) < w * h * 4:
            return
        if self.colorkey:
            SetWindowPos(self.overlay, wintypes.HWND(HWND_TOPMOST), x, y, w, h, SWP_NOACTIVATE | SWP_SHOWWINDOW)
            self._paint()
            self.visible = True
            return
        screen = GetDC(None)
        mem = CreateCompatibleDC(screen)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        bits = ctypes.c_void_p()
        dib = CreateDIBSection(screen, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
        try:
            if not dib or not bits.value:
                return
            ctypes.memmove(bits.value, data, w * h * 4)
            old = SelectObject(mem, dib)
            blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
            ok = UpdateLayeredWindow(self.overlay, screen, ctypes.byref(POINT(x, y)), ctypes.byref(SIZE(w, h)),
                                     mem, ctypes.byref(POINT(0, 0)), 0, ctypes.byref(blend), ULW_ALPHA)
            SelectObject(mem, old)
            if not ok:
                log.debug("UpdateLayeredWindow: %s", ctypes.get_last_error())
            SetWindowPos(self.overlay, wintypes.HWND(HWND_TOPMOST), 0, 0, 0, 0,
                         SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
            self.visible = True
        finally:
            if dib:
                DeleteObject(dib)
            DeleteDC(mem)
            ReleaseDC(None, screen)

    def _paint(self) -> None:
        """Окно с цветовым ключом: нарисовать последнюю картинку (при показе и на WM_PAINT)."""
        with self._frame_lock:
            frame = self._frame
        if frame is None or not self.overlay:
            return
        _, _, w, h, data = frame
        if len(data) < w * h * 4:
            return
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        dc = GetDC(self.overlay)
        if not dc:
            return
        try:
            buf = ctypes.create_string_buffer(data, w * h * 4) if isinstance(data, (bytes, bytearray)) else data
            SetDIBitsToDevice(dc, 0, 0, w, h, 0, 0, 0, h, buf, ctypes.byref(bmi), DIB_RGB_COLORS)
        finally:
            ReleaseDC(self.overlay, dc)
        ValidateRect(self.overlay, None)

    def _set_transparent(self, on: bool) -> None:
        style = GetWindowLongW(self.overlay, GWL_EXSTYLE)
        style = (style | WS_EX_TRANSPARENT) if on else (style & ~WS_EX_TRANSPARENT)
        SetWindowLongW(self.overlay, GWL_EXSTYLE, style)

    def _select_paint(self) -> None:
        """Затемнение и рамка выбора области (картинку рисует PIL, если есть)."""
        sel = self._select
        if not sel:
            return
        x, y, w, h = sel["area"]
        try:
            from PIL import Image, ImageDraw
            from .render import font, to_bgra_premultiplied, to_colorkey
            # окно с цветовым ключом полупрозрачно целиком (затемнение), обведённое — ключ (дыра)
            img = Image.new("RGBA", (w, h), (8, 10, 16, 255 if self.colorkey else 110))
            d = ImageDraw.Draw(img)
            if sel["start"] and sel["cur"]:
                (ax, ay), (bx, by) = sel["start"], sel["cur"]
                r = (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))
                d.rectangle(r, fill=(0, 0, 0, 0 if self.colorkey else 1), outline=(139, 123, 255, 255), width=3)
            msg = "Обведите мышью область с текстом игры  ·  Esc — отмена"
            f = font(max(14, h // 40))
            tw = f.getlength(msg)
            d.rounded_rectangle((w / 2 - tw / 2 - 16, 24, w / 2 + tw / 2 + 16, 24 + f.size + 20), radius=10,
                                fill=(14, 16, 24, 230))
            d.text((w / 2 - tw / 2, 34), msg, font=f, fill=(245, 246, 250, 255))
            data = to_colorkey(img) if self.colorkey else to_bgra_premultiplied(img)
            with self._frame_lock:
                self._frame = (x, y, w, h, data)
            if self.colorkey:
                SetLayeredWindowAttributes(self.overlay, COLOR_KEY, SELECT_ALPHA, LWA_COLORKEY | LWA_ALPHA)
            self._render()
        except Exception:  # noqa: BLE001
            log.exception("рамка выбора не нарисована")

    def _end_select(self, rect: Optional[Tuple[int, int, int, int]]) -> None:
        self._select = None
        ReleaseCapture()
        self._set_transparent(True)
        UnregisterHotKey(self.host, 0xBFF0)
        ShowWindow(self.overlay, SW_HIDE)
        self.visible = False
        if self.colorkey:
            SetLayeredWindowAttributes(self.overlay, COLOR_KEY, 255, LWA_COLORKEY)
        with self._frame_lock:
            self._frame = None
        try:
            self.on_region(rect)
        except Exception:  # noqa: BLE001
            log.exception("выбор области")

    def _menu(self) -> None:
        menu = CreatePopupMenu()
        cmds: Dict[int, str] = {}
        for i, (cmd, title, checked) in enumerate(self.menu_items(), 1):
            if cmd == "-":
                AppendMenuW(menu, MF_SEPARATOR, 0, None)
                continue
            cmds[i] = cmd
            AppendMenuW(menu, MF_STRING | (MF_CHECKED if checked else 0), i, title)
        pt = POINT()
        GetCursorPos(ctypes.byref(pt))
        SetForegroundWindow(self.host)
        chosen = TrackPopupMenu(menu, TPM_RETURNCMD | TPM_NONOTIFY | TPM_RIGHTBUTTON, pt.x, pt.y, 0, self.host, None)
        PostMessageW(self.host, WM_NULL, 0, 0)
        DestroyMenu(menu)
        if chosen in cmds:
            try:
                self.on_menu(cmds[chosen])
            except Exception:  # noqa: BLE001
                log.exception("пункт меню трея")

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == MSG_RENDER and hwnd == self.overlay:
                if not self._select:
                    self._render()
                return 0
            if msg == MSG_AFFINITY and hwnd == self.overlay:
                SetWindowDisplayAffinity(self.overlay, WDA_EXCLUDEFROMCAPTURE if wparam else WDA_NONE)
                return 0
            if hwnd == self.overlay and self.colorkey:
                if msg == WM_ERASEBKGND:
                    return 1                        # фон не стирать — картинка рисуется целиком
                if msg == WM_PAINT:
                    self._paint()
                    return 0
            if msg == MSG_HIDE and hwnd == self.overlay:
                if not self._select:
                    ShowWindow(self.overlay, SW_HIDE)
                    self.visible = False
                return 0
            if msg == MSG_SELECT and hwnd == self.overlay:
                self._set_transparent(False)
                RegisterHotKey(self.host, 0xBFF0, 0, VK_ESCAPE)
                self._select_paint()
                SetCapture(self.overlay)
                return 0
            if hwnd == self.overlay and self._select:
                if msg == WM_SETCURSOR:
                    SetCursor(LoadCursorW(None, ctypes.c_void_p(IDC_CROSS)))
                    return 1
                x, y = ctypes.c_short(lparam & 0xFFFF).value, ctypes.c_short((lparam >> 16) & 0xFFFF).value
                if msg == WM_LBUTTONDOWN:
                    self._select["start"] = self._select["cur"] = (x, y)
                    self._select_paint()
                    return 0
                if msg == WM_MOUSEMOVE and self._select.get("start"):
                    self._select["cur"] = (x, y)
                    self._select_paint()
                    return 0
                if msg == WM_LBUTTONUP and self._select.get("start"):
                    (ax, ay) = self._select["start"]
                    r = (min(ax, x), min(ay, y), abs(x - ax), abs(y - ay))
                    self._end_select(r if r[2] >= 20 and r[3] >= 12 else None)
                    return 0
            if msg == MSG_QUIT:
                self._cleanup()
                DestroyWindow(self.overlay)
                DestroyWindow(self.host)
                PostQuitMessage(0)
                return 0
            if msg == MSG_HOTKEYS and hwnd == self.host:
                self._apply_hotkeys()
                return 0
            if msg == MSG_BALLOON and hwnd == self.host:
                self._tray(NIM_MODIFY)
                return 0
            if msg == MSG_TIP and hwnd == self.host:
                self._tray(NIM_MODIFY)
                return 0
            if msg == WM_HOTKEY and hwnd == self.host:
                if wparam == 0xBFF0:
                    if self._select:
                        self._end_select(None)
                    return 0
                hk = self._hotkeys.get(int(wparam))
                if hk is not None:
                    self.on_hotkey(hk.name)
                return 0
            if msg == MSG_TRAY and hwnd == self.host:
                event = lparam & 0xFFFF
                if event in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._menu()
                elif event == WM_LBUTTONUP:         # двойной щелчок = два «вверх» — второй отсечёт open_main_window
                    self.on_menu("open")
                return 0
            if msg == self._taskbar_created and self._taskbar_created and hwnd == self.host:
                self._tray(NIM_ADD)     # Проводник перезапустился — иконка трея пропала
                return 0
        except Exception:  # noqa: BLE001
            log.exception("обработка сообщения %s", msg)
        return DefWindowProcW(hwnd, msg, wparam, lparam)
