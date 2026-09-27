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
    """WinRT через pythonnet. Объекты WinRT приходят как ``__ComObject`` — у них нет атрибутов
    и итерации в Python, поэтому всё читается отражением через известные типы и интерфейсы
    (коллекции WinRT CLR показывает как ``IReadOnlyList<T>``)."""

    name = "pythonnet"

    def __init__(self, lang: str = "en"):
        try:
            import clr  # noqa: F401  (pythonnet: .NET Framework в процессе)
        except Exception as exc:  # noqa: BLE001
            raise OcrUnavailable("winrt", f"pythonnet недоступен: {exc}") from exc
        import clr
        clr.AddReference("System.Runtime.WindowsRuntime")
        from System import Activator, Array, Byte, Enum, Int32, IntPtr, Object, Type  # type: ignore
        from System.Runtime.InteropServices import Marshal  # type: ignore
        self._Array, self._Byte, self._Enum, self._IntPtr, self._Marshal = Array, Byte, Enum, IntPtr, Marshal
        self._Object, self._Int32 = Object, Int32

        def wrt(name: str, *asms: str):
            for a in asms:
                t = Type.GetType(f"{name}, {a}, ContentType=WindowsRuntime")
                if t is not None:
                    return t
            raise OcrUnavailable("winrt", f"тип WinRT не найден: {name}")

        media = ("Windows.Foundation", "Windows.Media", "Windows")
        graphics = ("Windows.Graphics", "Windows.Foundation", "Windows")
        self.Engine = wrt("Windows.Media.Ocr.OcrEngine", *media)
        result_t = wrt("Windows.Media.Ocr.OcrResult", *media)
        self._line_t = wrt("Windows.Media.Ocr.OcrLine", *media)
        self._word_t = wrt("Windows.Media.Ocr.OcrWord", *media)
        lang_t = wrt("Windows.Globalization.Language", "Windows.Globalization", "Windows.Foundation", "Windows")
        self.Bitmap = wrt("Windows.Graphics.Imaging.SoftwareBitmap", *graphics)
        self.PixelFormat = wrt("Windows.Graphics.Imaging.BitmapPixelFormat", *graphics)
        self.AlphaMode = wrt("Windows.Graphics.Imaging.BitmapAlphaMode", *graphics)
        self._ro_list = Type.GetType("System.Collections.Generic.IReadOnlyList`1")
        self._ro_coll = Type.GetType("System.Collections.Generic.IReadOnlyCollection`1")
        self._disposable = Type.GetType("System.IDisposable")
        if self._ro_list is None or self._ro_coll is None:
            raise OcrUnavailable("winrt", "IReadOnlyList не найден")
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
        self._lines_p = result_t.GetProperty("Lines")
        self._words_p = self._line_t.GetProperty("Words")
        self._text_p = self._line_t.GetProperty("Text")
        self._rect_p = self._word_t.GetProperty("BoundingRect")
        tag_p = lang_t.GetProperty("LanguageTag")

        # язык: сначала напрямую (en-US, en-GB…), затем среди установленных
        supported = self.Engine.GetMethod("IsLanguageSupported")
        pick = None
        for tag in ([lang] if "-" in lang else []) + [f"{lang}-US", f"{lang}-GB", lang]:
            try:
                cand = Activator.CreateInstance(lang_t, Array[Object]([tag]))
                if bool(supported.Invoke(None, Array[Object]([cand]))):
                    pick = cand
                    break
            except Exception:  # noqa: BLE001
                continue
        if pick is None:
            try:
                langs = self._items(self.Engine.GetProperty("AvailableRecognizerLanguages").GetValue(None, None), lang_t)
                pick = next((x for x in langs if str(tag_p.GetValue(x, None)).lower().startswith(lang.lower())), None)
            except Exception as exc:  # noqa: BLE001
                log.warning("список языков распознавания: %s", exc)
        if pick is None:
            raise OcrUnavailable("no_language", "распознавание английского не установлено в Windows")
        self.engine = self.Engine.GetMethod("TryCreateFromLanguage").Invoke(None, Array[Object]([pick]))
        if self.engine is None:
            raise OcrUnavailable("no_language", "распознавание английского не установлено в Windows")
        self.lang = str(tag_p.GetValue(pick, None))
        try:
            self.max_dim = int(self.Engine.GetProperty("MaxImageDimension").GetValue(None, None))
        except Exception:  # noqa: BLE001
            pass
        self._fmt = self._Enum.ToObject(self.PixelFormat, 87)      # Bgra8
        self._alpha = self._Enum.ToObject(self.AlphaMode, 2)       # Ignore
        self._lock = threading.Lock()

    def _items(self, vector, elem_t) -> list:
        """Элементы коллекции WinRT (IVectorView<T>) через IReadOnlyList<T>."""
        if vector is None:
            return []
        count = int(self._ro_coll.MakeGenericType(elem_t).GetProperty("Count").GetValue(vector, None))
        item = self._ro_list.MakeGenericType(elem_t).GetProperty("Item")
        return [item.GetValue(vector, self._Array[self._Object]([self._Int32(i)])) for i in range(count)]

    def _dispose(self, obj) -> None:
        try:
            if obj is not None and self._disposable is not None:
                self._disposable.GetMethod("Dispose").Invoke(obj, None)     # IClosable.Close()
        except Exception:  # noqa: BLE001
            pass

    def recognize(self, w: int, h: int, bgra: bytes) -> List[Line]:
        n = w * h * 4
        with self._lock:
            raw = (ctypes.c_char * n).from_buffer_copy(bgra[:n])
            arr = self._Array.CreateInstance(self._Byte, n)
            self._Marshal.Copy(self._IntPtr(ctypes.addressof(raw)), arr, 0, n)
            buf = self._as_buffer.Invoke(None, self._Array[self._Object]([arr]))
            bmp = self._create.Invoke(None, self._Array[self._Object](
                [buf, self._fmt, self._Int32(w), self._Int32(h), self._alpha]))
            try:
                op = self._recognize.Invoke(self.engine, self._Array[self._Object]([bmp]))
                task = self._as_task.Invoke(None, self._Array[self._Object]([op]))
                if not task.Wait(15000):
                    raise RuntimeError("распознавание не ответило за 15 с")
                result = task.GetType().GetProperty("Result").GetValue(task, None)
                out: List[Line] = []
                for line in self._items(self._lines_p.GetValue(result, None), self._line_t):
                    xs, ys, xe, ye = [], [], [], []
                    for word in self._items(self._words_p.GetValue(line, None), self._word_t):
                        r = self._rect_p.GetValue(word, None)
                        x, y = float(r.X), float(r.Y)
                        xs.append(x)
                        ys.append(y)
                        xe.append(x + float(r.Width))
                        ye.append(y + float(r.Height))
                    if xs:
                        x0, y0 = int(min(xs)), int(min(ys))
                        out.append(Line(str(self._text_p.GetValue(line, None)), x0, y0,
                                        int(max(xe)) - x0, int(max(ye)) - y0))
                return out
            finally:
                self._dispose(bmp)


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
