"""Распознавание текста Windows (Windows.Media.Ocr) — двумя путями.

1. В процессе, через pythonnet (.NET Framework умеет вызывать WinRT) — быстро.
2. Если так не вышло — фоновый PowerShell 5.1 со скриптом resources/overlay/ocr.ps1:
   тот же API, проверенный способ; кадры передаются файлом во временной папке.

Язык распознавания — английский: он должен быть установлен в Windows (компонент
«Оптическое распознавание символов»). Если его нет — :class:`OcrUnavailable` с
кодом ``no_language``, а окно программы предлагает поставить его одной кнопкой
(:func:`install_language_command`).
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import List, Optional

from .text import Line

log = logging.getLogger("russificator.overlay.ocr")

CREATE_NO_WINDOW = 0x08000000


class OcrUnavailable(Exception):
    """Распознавание недоступно. ``code``: no_language | winrt | no_windows | failed."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


class Ocr:
    name = ""
    lang = ""
    max_dim = 4096

    def recognize(self, w: int, h: int, bgra: bytes) -> List[Line]:
        raise NotImplementedError

    def close(self) -> None:
        pass


# ------------------------------------------------------------------ pythonnet

class DotNetOcr(Ocr):
    name = "pythonnet"

    def __init__(self, lang: str = "en"):
        try:
            import clr  # noqa: F401  (pythonnet: .NET Framework в процессе)
        except Exception as exc:  # noqa: BLE001
            raise OcrUnavailable("winrt", f"pythonnet недоступен: {exc}") from exc
        import clr
        clr.AddReference("System.Runtime.WindowsRuntime")
        from System import Activator, Array, Byte, Enum, IntPtr, Type  # type: ignore
        from System.Runtime.InteropServices import Marshal  # type: ignore
        self._Array, self._Byte, self._Enum, self._IntPtr, self._Marshal = Array, Byte, Enum, IntPtr, Marshal

        def wrt(name: str, *asms: str):
            for a in asms:
                t = Type.GetType(f"{name}, {a}, ContentType=WindowsRuntime")
                if t is not None:
                    return t
            raise OcrUnavailable("winrt", f"тип WinRT не найден: {name}")

        self.Engine = wrt("Windows.Media.Ocr.OcrEngine", "Windows.Foundation", "Windows.Media", "Windows")
        result_t = wrt("Windows.Media.Ocr.OcrResult", "Windows.Foundation", "Windows.Media", "Windows")
        self.Bitmap = wrt("Windows.Graphics.Imaging.SoftwareBitmap", "Windows.Graphics", "Windows.Foundation", "Windows")
        self.PixelFormat = wrt("Windows.Graphics.Imaging.BitmapPixelFormat", "Windows.Graphics",
                               "Windows.Foundation", "Windows")
        self.AlphaMode = wrt("Windows.Graphics.Imaging.BitmapAlphaMode", "Windows.Graphics", "Windows.Foundation",
                             "Windows")
        rt = "System.Runtime.WindowsRuntime, Version=4.0.0.0, Culture=neutral, PublicKeyToken=b77a5c561934e089"
        ext = Type.GetType(f"System.WindowsRuntimeSystemExtensions, {rt}")
        bufext = Type.GetType(f"System.Runtime.InteropServices.WindowsRuntime.WindowsRuntimeBufferExtensions, {rt}")
        if ext is None or bufext is None:
            raise OcrUnavailable("winrt", "System.Runtime.WindowsRuntime не найден")
        as_task = [m for m in ext.GetMethods() if m.Name == "AsTask" and len(m.GetParameters()) == 1
                   and m.GetParameters()[0].ParameterType.Name == "IAsyncOperation`1"]
        if not as_task:
            raise OcrUnavailable("winrt", "AsTask не найден")
        self._as_task = as_task[0].MakeGenericMethod(result_t)
        byte_arr = Array.CreateInstance(Byte, 0).GetType()
        self._as_buffer = bufext.GetMethod("AsBuffer", [byte_arr])
        creates = [m for m in self.Bitmap.GetMethods() if m.Name == "CreateCopyFromBuffer" and len(m.GetParameters()) == 5]
        if self._as_buffer is None or not creates:
            raise OcrUnavailable("winrt", "SoftwareBitmap.CreateCopyFromBuffer не найден")
        self._create = creates[0]
        self._recognize = self.Engine.GetMethod("RecognizeAsync")
        langs = list(self.Engine.GetProperty("AvailableRecognizerLanguages").GetValue(None, None))
        pick = next((x for x in langs if str(x.LanguageTag).lower().startswith(lang.lower())), None)
        if pick is None:
            raise OcrUnavailable("no_language", "распознавание английского не установлено в Windows")
        self.engine = self.Engine.GetMethod("TryCreateFromLanguage").Invoke(None, [pick])
        if self.engine is None:
            raise OcrUnavailable("no_language", "распознавание английского не установлено в Windows")
        self.lang = str(pick.LanguageTag)
        try:
            self.max_dim = int(self.Engine.GetProperty("MaxImageDimension").GetValue(None, None))
        except Exception:  # noqa: BLE001
            pass
        self._fmt = self._Enum.ToObject(self.PixelFormat, 87)      # Bgra8
        self._alpha = self._Enum.ToObject(self.AlphaMode, 2)       # Ignore
        self._lock = threading.Lock()

    def recognize(self, w: int, h: int, bgra: bytes) -> List[Line]:
        n = w * h * 4
        with self._lock:
            raw = (ctypes.c_char * n).from_buffer_copy(bgra[:n])
            arr = self._Array.CreateInstance(self._Byte, n)
            self._Marshal.Copy(self._IntPtr(ctypes.addressof(raw)), arr, 0, n)
            buf = self._as_buffer.Invoke(None, [arr])
            bmp = self._create.Invoke(None, [buf, self._fmt, w, h, self._alpha])
            try:
                op = self._recognize.Invoke(self.engine, [bmp])
                task = self._as_task.Invoke(None, [op])
                task.Wait(15000)
                result = task.Result
                out: List[Line] = []
                for line in result.Lines:
                    xs, ys, xe, ye = [], [], [], []
                    for word in line.Words:
                        r = word.BoundingRect
                        xs.append(float(r.X))
                        ys.append(float(r.Y))
                        xe.append(float(r.X) + float(r.Width))
                        ye.append(float(r.Y) + float(r.Height))
                    if xs:
                        x0, y0 = int(min(xs)), int(min(ys))
                        out.append(Line(str(line.Text), x0, y0, int(max(xe)) - x0, int(max(ye)) - y0))
                return out
            finally:
                try:
                    bmp.Dispose()
                except Exception:  # noqa: BLE001
                    pass


# ------------------------------------------------------------------ PowerShell

def _script() -> Path:
    return Path(__file__).resolve().parents[1] / "resources" / "overlay" / "ocr.ps1"


class PowerShellOcr(Ocr):
    name = "powershell"

    def __init__(self, lang: str = "en", workdir: Optional[Path] = None):
        if sys.platform != "win32":
            raise OcrUnavailable("no_windows", "распознавание текста есть только в Windows")
        script = _script()
        if not script.is_file():
            raise OcrUnavailable("failed", f"нет скрипта распознавания: {script}")
        self._dir = Path(workdir or tempfile.gettempdir()) / "RussificatorLive"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._frame = self._dir / f"frame-{os.getpid()}.raw"
        exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / \
            "powershell.exe"
        self._proc = subprocess.Popen(
            [str(exe) if exe.is_file() else "powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
             "Bypass", "-File", str(script), "-Lang", lang],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
        self._lock = threading.Lock()
        first = self._read(timeout=60)
        if first is None:
            self.close()
            raise OcrUnavailable("failed", "PowerShell не ответил")
        if first.get("error"):
            self.close()
            code = "no_language" if first["error"] == "no_language" else "winrt"
            raise OcrUnavailable(code, str(first.get("message") or first["error"]))
        self.lang = str(first.get("lang", lang))
        self.max_dim = int(first.get("max") or 4096)

    def _read(self, timeout: float = 20) -> Optional[dict]:
        out: dict = {}

        def reader():
            line = self._proc.stdout.readline()
            if line:
                try:
                    out.update(json.loads(line.decode("utf-8", "replace").lstrip("\ufeff")))
                except ValueError:
                    out["error"] = "bad_json"
        t = threading.Thread(target=reader, daemon=True)
        t.start()
        t.join(timeout)
        return out if out else None

    def recognize(self, w: int, h: int, bgra: bytes) -> List[Line]:
        with self._lock:
            if self._proc.poll() is not None:
                raise OcrUnavailable("failed", "процесс распознавания завершился")
            self._frame.write_bytes(bgra[:w * h * 4])
            self._proc.stdin.write(f"{self._frame}|{w}|{h}\n".encode("utf-8"))
            self._proc.stdin.flush()
            res = self._read()
        if res is None:
            raise OcrUnavailable("failed", "распознавание не ответило вовремя")
        if res.get("error"):
            raise RuntimeError(str(res.get("message") or res["error"]))
        lines = res.get("lines") or []
        if isinstance(lines, dict):          # PowerShell превращает массив из одного элемента в объект
            lines = [lines]
        return [Line(str(x.get("t", "")), int(x.get("x", 0)), int(x.get("y", 0)), int(x.get("w", 0)),
                     int(x.get("h", 0))) for x in lines if x]

    def close(self) -> None:
        try:
            if self._proc.poll() is None:
                self._proc.stdin.write(b"quit\n")
                self._proc.stdin.flush()
                self._proc.wait(3)
        except Exception:  # noqa: BLE001
            try:
                self._proc.kill()
            except Exception:  # noqa: BLE001
                pass
        try:
            self._frame.unlink(missing_ok=True)
        except OSError:
            pass


def create(lang: str = "en", workdir: Optional[Path] = None) -> Ocr:
    """Первый работающий способ распознавания; OcrUnavailable с самой понятной причиной."""
    if sys.platform != "win32":
        raise OcrUnavailable("no_windows", "распознавание текста есть только в Windows")
    errors: List[OcrUnavailable] = []
    for make in (lambda: DotNetOcr(lang), lambda: PowerShellOcr(lang, workdir)):
        try:
            ocr = make()
            try:
                ocr.recognize(64, 32, b"\xff" * 64 * 32 * 4)     # проверка всего пути на пустой картинке
            except Exception as exc:  # noqa: BLE001
                ocr.close()
                raise OcrUnavailable("winrt", f"проверка распознавания не прошла: {exc}") from exc
            log.info("распознавание: %s, язык %s", ocr.name, ocr.lang)
            return ocr
        except OcrUnavailable as exc:
            log.warning("распознавание (%s): %s", exc.code, exc)
            errors.append(exc)
            if exc.code == "no_language":
                break                         # второй путь даст то же самое
        except Exception as exc:  # noqa: BLE001
            log.warning("распознавание: %s", exc)
            errors.append(OcrUnavailable("failed", str(exc)))
    best = next((e for e in errors if e.code == "no_language"), errors[-1] if errors else OcrUnavailable("failed"))
    raise best


def install_language_command(tag: str = "en-US") -> str:
    """Команда PowerShell (от администратора): поставить распознавание языка."""
    return (f"Add-WindowsCapability -Online -Name 'Language.OCR~~~{tag}~0.0.1.0'; "
            "Write-Host 'Готово. Закройте это окно.'; Start-Sleep 3")
