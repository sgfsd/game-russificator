"""Способ 2: нейросеть на своём компьютере (для мощных ПК), полностью офлайн.

Программа сама скачивает:
  * llama.cpp (``llama-server``, сборка Vulkan — ускорение на любой
    видеокарте NVIDIA/AMD/Intel, без видеокарты работает на процессоре);
  * модель Gemma 4 от Google в формате GGUF (лицензия Apache 2.0) — одну из
    трёх по мощности ПК. Gemma — сильнейшее открытое семейство для
    перевода, хорошо знает русский.

Сервер запускается на 127.0.0.1 на время перевода и общается с программой
по OpenAI-совместимому API (тот же клиент, что и у облачного способа).
Если программа закроется или упадёт, сервер завершится вместе с ней.
"""

from __future__ import annotations

import atexit
import hashlib
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .. import net, paths
from .base import StatusFn, TranslatorError, _noop
from .llm import ChatClient, LLMTranslator

log = logging.getLogger("russificator.local_llm")

#: проверенная сборка llama.cpp; если её когда-нибудь удалят — берём свежую из релизов
LLAMA_BUILD = "b11147"
LLAMA_REPO = "ggml-org/llama.cpp"


@dataclass(frozen=True)
class ModelPreset:
    id: str
    title: str
    repo: str
    file: str
    size_gb: float
    min_ram_gb: int      # ОЗУ для работы на процессоре
    good_vram_gb: int    # видеопамять, при которой модель целиком на видеокарте
    description: str
    size: int = 0        # точный размер файла, байт
    sha256: str = ""     # контрольная сумма файла (та же на всех зеркалах)

    @property
    def url(self) -> str:
        return f"https://huggingface.co/{self.repo}/resolve/main/{self.file}?download=true"

    @property
    def urls(self) -> List[str]:
        """Один и тот же файл в двух местах: Hugging Face и его зеркало ModelScope (Alibaba) —
        у кого-то не открывается или душится один сервер, загрузка идёт с другого."""
        return [self.url, f"https://www.modelscope.cn/models/{self.repo}/resolve/master/{self.file}"]


PRESETS: List[ModelPreset] = [
    ModelPreset("gemma4-e2b", "Gemma 4 E2B — лёгкая", "unsloth/gemma-4-E2B-it-qat-GGUF",
                "gemma-4-E2B-it-qat-UD-Q4_K_XL.gguf", 2.62, 6, 4,
                "Быстрая, для ноутбуков и ПК с 8 ГБ ОЗУ. Качество — хорошее.",
                2620370976, "e531007218dfab990486a5de7676a6932d6ea8dea233d1f698d7c21cf8a16889"),
    ModelPreset("gemma4-e4b", "Gemma 4 E4B — оптимальная", "unsloth/gemma-4-E4B-it-qat-GGUF",
                "gemma-4-E4B-it-qat-UD-Q4_K_XL.gguf", 4.22, 10, 6,
                "Баланс скорости и качества. Для ПК с 16 ГБ ОЗУ или видеокартой 6+ ГБ.",
                4215695776, "df0fd4ee07072c607c29a0a1cb4f98918426cca12f45a2776bdd6ee6d09a4de3"),
    ModelPreset("gemma4-12b", "Gemma 4 12B — лучшая", "google/gemma-4-12B-it-qat-q4_0-gguf",
                "gemma-4-12b-it-qat-q4_0.gguf", 6.98, 16, 10,
                "Качество близко к облачным нейросетям. Нужна видеокарта 10+ ГБ "
                "(или 16+ ГБ ОЗУ и терпение).",
                6975879296, "93567e57a8fe10b23569b9d9ec38cd005deedf71e29477c421a4b83f418a538b"),
]
PRESET_BY_ID = {p.id: p for p in PRESETS}


# ---------- оборудование ----------

def total_ram_gb() -> float:
    try:
        if sys.platform == "win32":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(st)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return st.ullTotalPhys / (1 << 30)
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1 << 30)
    except (OSError, ValueError, AttributeError):
        return 8.0


def gpu_devices() -> List[dict]:
    """Видеокарты, которые видит llama.cpp (нужен установленный рантайм)."""
    exe = server_exe()
    if exe is None:
        return []
    try:
        out = subprocess.run([str(exe), "--list-devices"], capture_output=True, text=True, timeout=30,
                             creationflags=_NO_WINDOW, cwd=str(exe.parent)).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    devs = []
    for m in re.finditer(r"^\s*(\w+\d+):\s*(.+?)\s*\((\d+) MiB,\s*(\d+) MiB free\)", out, re.M):
        devs.append({"id": m.group(1), "name": m.group(2), "vram_gb": int(m.group(3)) / 1024,
                     "free_gb": int(m.group(4)) / 1024})
    return devs


#: видеопамять, нужная модели целиком (веса на видеокарте + контекст и буферы, замер llama.cpp).
#: У E-моделей Gemma таблицы эмбеддингов остаются в ОЗУ — им нужно меньше размера файла.
VRAM_NEED_GB = {"gemma4-e2b": 1.6, "gemma4-e4b": 3.0, "gemma4-12b": 8.0}


def vram_need_gb(preset: Optional[ModelPreset], custom: Optional[Path] = None) -> float:
    if custom is not None:
        try:
            return custom.stat().st_size / 1e9 + 0.8
        except OSError:
            return 0.0
    return VRAM_NEED_GB.get(preset.id, preset.size_gb + 0.8) if preset else 0.0


def best_gpu() -> Optional[dict]:
    """Видеокарта с наибольшей свободной памятью (как её видит llama.cpp прямо сейчас)."""
    devs = gpu_devices()
    return max(devs, key=lambda d: d["free_gb"]) if devs else None


def recommend_preset(ram_gb: Optional[float] = None, vram_gb: Optional[float] = None) -> str:
    ram = total_ram_gb() if ram_gb is None else ram_gb
    vram = vram_gb if vram_gb is not None else max((d["vram_gb"] for d in gpu_devices()), default=0)
    if vram >= 10 or (vram >= 8 and ram >= 16):
        return "gemma4-12b"
    if vram >= 6 or ram >= 14:
        return "gemma4-e4b"
    return "gemma4-e2b"


# ---------- llama.cpp ----------

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _asset_suffixes() -> List[str]:
    machine = platform.machine().lower()
    arm = machine in ("arm64", "aarch64")
    if sys.platform == "win32":
        return ["bin-win-cpu-arm64.zip"] if arm else ["bin-win-vulkan-x64.zip", "bin-win-cpu-x64.zip"]
    if sys.platform == "darwin":
        return ["bin-macos-arm64.tar.gz"] if arm else ["bin-macos-x64.tar.gz"]
    return ["bin-ubuntu-vulkan-arm64.tar.gz", "bin-ubuntu-arm64.tar.gz"] if arm else \
        ["bin-ubuntu-vulkan-x64.tar.gz", "bin-ubuntu-x64.tar.gz"]


def runtime_dir() -> Path:
    return paths.tools_dir() / "llama.cpp"


def server_exe() -> Optional[Path]:
    name = "llama-server.exe" if sys.platform == "win32" else "llama-server"
    d = runtime_dir()
    if not d.is_dir():
        return None
    for p in [d / name, *d.rglob(name)]:
        if p.is_file():
            return p
    return None


def _release_urls() -> List[str]:
    """Ссылки на архив llama.cpp: сначала проверенная сборка, затем свежие релизы."""
    urls = [f"https://github.com/{LLAMA_REPO}/releases/download/{LLAMA_BUILD}/llama-{LLAMA_BUILD}-{s}"
            for s in _asset_suffixes()]
    try:
        releases = net.get_json(f"https://api.github.com/repos/{LLAMA_REPO}/releases?per_page=10", timeout=15)
        for rel in releases:
            names = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
            for s in _asset_suffixes():
                for name, url in names.items():
                    if name.endswith(s) and not name.startswith("cudart"):
                        urls.append(url)
    except Exception as exc:  # noqa: BLE001
        log.info("Список релизов llama.cpp недоступен: %s", exc)
    return list(dict.fromkeys(urls))


def install_runtime(status: StatusFn = _noop, cancel: Optional[threading.Event] = None, notes=None) -> Path:
    if server_exe() is not None:
        return server_exe()
    target = runtime_dir()
    last_error: Optional[Exception] = None
    for url in _release_urls():
        archive = paths.tools_dir() / url.rsplit("/", 1)[-1]

        def progress(done: int, total: int) -> None:
            status(f"Скачивание llama.cpp: {done >> 20} из {(total >> 20) or '?'} МБ",
                   done / total if total else None)

        try:
            net.download(url, archive, progress=progress, cancel=cancel, notes=notes)
        except net.Cancelled:
            raise
        except net.NetError as exc:
            last_error = exc
            continue
        status("Распаковка llama.cpp…", None)
        tmp = target.with_name("llama.cpp.tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as z:
                z.extractall(tmp)
        else:
            with tarfile.open(archive) as t:
                t.extractall(tmp, filter="data")
        archive.unlink(missing_ok=True)
        shutil.rmtree(target, ignore_errors=True)
        tmp.rename(target)
        exe = server_exe()
        if exe is None:
            last_error = RuntimeError("в архиве нет llama-server")
            continue
        if sys.platform != "win32":
            for f in exe.parent.iterdir():
                if f.is_file() and not f.suffix:
                    f.chmod(0o755)
        return exe
    raise TranslatorError(f"Не удалось скачать llama.cpp: {last_error}")


# ---------- модели ----------

def model_path(preset: ModelPreset) -> Path:
    return paths.models_dir() / "gguf" / preset.file


def model_installed(preset: ModelPreset) -> bool:
    p = model_path(preset)
    return p.is_file() and p.stat().st_size > preset.size_gb * 0.9 * 1e9


def install_model(preset: ModelPreset, status: StatusFn = _noop,
                  cancel: Optional[threading.Event] = None, notes=None) -> Path:
    """Скачать модель (с Hugging Face и зеркала ModelScope сразу, с докачкой и проверкой SHA-256).
    ``notes`` получает сообщения о сбоях сети (что случилось и что программа делает дальше)."""
    dest = model_path(preset)
    if model_installed(preset):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(dest.parent).free / 1e9
    started = dest.with_name(dest.name + ".part").exists()   # начатая загрузка место уже заняла
    if not started and free < preset.size_gb + 0.3:
        raise TranslatorError(f"Недостаточно места на диске для модели: нужно {preset.size_gb:.1f} ГБ, "
                              f"свободно {free:.1f} ГБ. Смените папку данных в настройках.")
    window: List[tuple] = []

    def progress(done: int, total: int) -> None:
        total = total or int(preset.size_gb * 1e9)
        now = time.monotonic()
        window.append((now, done))
        while len(window) > 2 and now - window[0][0] > 15:
            window.pop(0)
        t0, d0 = window[0]
        speed = (done - d0) / (now - t0) if now - t0 > 1 else 0.0
        eta = (total - done) / speed if speed > 0 else 0
        status(f"Скачивание {preset.title}: {done / 1e9:.2f} из {total / 1e9:.2f} ГБ"
               + (f" · {speed / 1048576:.1f} МБ/с" if speed > 0 else "")
               + (f" · осталось ~{_fmt_eta(eta)}" if speed > 0 and done < total else ""), done / total)

    status(f"Скачивание {preset.title}…", None)
    log.info("модель %s: %s", preset.id, ", ".join(preset.urls))
    net.download(preset.urls, dest, progress=progress, cancel=cancel, sha256=preset.sha256,
                 expected_size=preset.size or int(preset.size_gb * 1e9), notes=notes)
    return dest


def import_model(preset: ModelPreset, src: Path, status: StatusFn = _noop,
                 cancel: Optional[threading.Event] = None) -> Path:
    """Поставить модель из файла, скачанного вручную (браузером, торрентом, с флешки): файл
    проверяется (GGUF, размер, SHA-256) и копируется в папку моделей."""
    src = Path(src)
    try:
        size = src.stat().st_size
        with src.open("rb") as fh:
            magic = fh.read(4)
    except OSError as exc:
        raise TranslatorError(f"Файл не открывается: {exc}") from None
    if magic != b"GGUF":
        raise TranslatorError("Это не файл модели GGUF — выберите скачанный .gguf.")
    if preset.size and size != preset.size:
        raise TranslatorError(f"Это другой файл или он скачан не до конца: {size / 1e9:.2f} ГБ вместо "
                              f"{preset.size / 1e9:.2f} ГБ. Нужен {preset.file}.")
    dest = model_path(preset)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".import")
    h = hashlib.sha256()
    done = 0
    try:
        with src.open("rb") as fi, tmp.open("wb") as fo:
            for block in iter(lambda: fi.read(8 << 20), b""):
                if cancel is not None and cancel.is_set():
                    raise net.Cancelled()
                h.update(block)
                fo.write(block)
                done += len(block)
                status(f"Проверяю и копирую модель: {done / 1e9:.2f} из {size / 1e9:.2f} ГБ", done / max(1, size))
        if preset.sha256 and h.hexdigest() != preset.sha256:
            raise TranslatorError("Файл повреждён или это другая версия модели (не совпала контрольная сумма). "
                                  "Скачайте его заново.")
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    for p in (dest.with_name(dest.name + ".part"), dest.with_name(dest.name + ".part.json")):
        p.unlink(missing_ok=True)
    log.info("модель %s поставлена из файла %s", preset.id, src)
    return dest


def remove_model(preset: ModelPreset) -> None:
    for p in (model_path(preset), model_path(preset).with_name(preset.file + ".part"),
              model_path(preset).with_name(preset.file + ".part.json")):
        p.unlink(missing_ok=True)


def _fmt_eta(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600} ч {seconds % 3600 // 60} мин"
    if seconds >= 60:
        return f"{seconds // 60} мин"
    return f"{seconds} с"


# ---------- сервер ----------

class LlamaServer:
    """Процесс llama-server на свободном локальном порту."""

    def __init__(self, exe: Path, model: Path, use_gpu: bool = True, ctx: int = 8192, threads: int = 0):
        self.exe, self.model, self.use_gpu, self.ctx, self.threads = exe, model, use_gpu, ctx, threads
        self.proc: Optional[subprocess.Popen] = None
        self.port = 0
        self._job = None
        self.log_path = paths.logs_dir() / "llama-server.log"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def start(self, status: StatusFn = _noop, cancel: Optional[threading.Event] = None,
              timeout: float = 600) -> None:
        self.port = _free_port()
        args = [str(self.exe), "-m", str(self.model), "--host", "127.0.0.1", "--port", str(self.port),
                "-c", str(self.ctx), "-np", "1", "--no-webui", "--no-mmproj", "--reasoning-budget", "0",
                "--alias", "local"]
        if not self.use_gpu:
            args += ["--device", "none"]
        if self.threads:
            args += ["-t", str(self.threads)]
        status("Запуск нейросети…", None)
        logf = self.log_path.open("w", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(args, stdout=logf, stderr=subprocess.STDOUT, cwd=str(self.exe.parent),
                                     creationflags=_NO_WINDOW)
        self._job = _kill_with_parent(self.proc)
        atexit.register(self.stop)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                self.stop()
                raise net.Cancelled()
            if self.proc.poll() is not None:
                raise TranslatorError("Нейросеть не запустилась: " + self._log_tail())
            try:
                data = net.get_json(f"http://127.0.0.1:{self.port}/health", timeout=5)
                if isinstance(data, dict) and data.get("status") == "ok":
                    status("Нейросеть загружена", 1.0)
                    return
            except net.NetError:
                pass
            time.sleep(1.0)
        self.stop()
        raise TranslatorError("Нейросеть не успела загрузиться за 10 минут: " + self._log_tail())

    def _log_tail(self) -> str:
        try:
            lines = self.log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        except OSError:
            return "лог недоступен"
        errors = [ln for ln in lines if re.search(r"error|failed|unable|out of memory", ln, re.I)]
        return " | ".join((errors or lines)[-3:])[:500]

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _kill_with_parent(proc: subprocess.Popen):
    """Windows: job-объект — llama-server умрёт вместе с программой даже при её падении."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("Basic", BASIC), ("Io", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        job = k32.CreateJobObjectW(None, None)
        info = EXTENDED()
        info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
        k32.AssignProcessToJobObject(job, wintypes.HANDLE(int(proc._handle)))  # type: ignore[attr-defined]
        return job
    except Exception as exc:  # noqa: BLE001
        log.debug("job object недоступен: %s", exc)
        return None


# ---------- переводчик ----------

class LocalTranslator(LLMTranslator):
    """Способ 2: Gemma 4 на llama.cpp у пользователя на компьютере."""

    def __init__(self, preset_id: str = "", use_gpu: bool = True, threads: int = 0,
                 custom_model: str = "", game_title: str = "", fit_vram: bool = False):
        self.preset = PRESET_BY_ID.get(preset_id) or PRESET_BY_ID[recommend_preset()]
        #: живой перевод: игра уже заняла видеопамять — взять модель, которая поместится рядом с ней
        self.fit_vram = fit_vram
        self.custom_model = Path(custom_model) if custom_model else None
        self.use_gpu = use_gpu
        self.threads = threads
        self.server: Optional[LlamaServer] = None
        name = self.custom_model.stem if self.custom_model else self.preset.id
        client = ChatClient("http://127.0.0.1:0/v1", model="local", local=True, timeout=900, max_retries=2,
                            extra={"chat_template_kwargs": {"enable_thinking": False}})
        super().__init__(client, cache_id=f"llm:{name}", title=f"Нейросеть на ПК: {name}",
                         parallel=1, max_batch_items=16, max_batch_chars=1800, game_title=game_title)

    def prepare(self, status: StatusFn = _noop, cancel: Optional[threading.Event] = None) -> None:
        exe = install_runtime(status, cancel)
        if self.custom_model:
            if not self.custom_model.is_file():
                raise TranslatorError(f"Файл модели не найден: {self.custom_model}")
            model = self.custom_model
        else:
            if self.use_gpu:
                self._fit_to_vram(status)
            model = install_model(self.preset, status, cancel)
        if self.server is None or self.server.proc is None:
            self.server = LlamaServer(exe, model, use_gpu=self.use_gpu, threads=self.threads)
            self.server.start(status, cancel)
        self.client.base_url = self.server.base_url
        super().prepare(status, cancel)

    def _fit_to_vram(self, status: StatusFn) -> None:
        """llama.cpp раскладывает модель по свободной видеопамяти на момент запуска: что не влезло,
        считает процессор — медленно и с полной загрузкой ЦП. В живом переводе игра уже заняла
        видеопамять, поэтому модель «лучшая» рядом с ней почти вся уходит на процессор."""
        dev = best_gpu()
        if dev is None:
            log.info("Видеокарта для нейросети не найдена — модель %s на процессоре", self.preset.id)
            return
        need = vram_need_gb(self.preset)
        log.info("Видеокарта %s: свободно %.1f из %.1f ГБ, модели %s нужно ~%.1f ГБ",
                 dev["name"], dev["free_gb"], dev["vram_gb"], self.preset.id, need)
        if need <= dev["free_gb"]:
            return
        if self.fit_vram:
            fits = [p for p in PRESETS if model_installed(p) and vram_need_gb(p) <= dev["free_gb"]]
            if fits:
                best = max(fits, key=vram_need_gb)
                log.warning("Видеопамять занята игрой (свободно %.1f ГБ): для живого перевода взята модель %s "
                            "вместо %s", dev["free_gb"], best.id, self.preset.id)
                status(f"Игра заняла видеопамять — для живого перевода взята модель «{best.title}»", None)
                self.preset = best
                self.cache_id = f"llm:{best.id}"
                return
        log.warning("Модель %s не помещается в свободную видеопамять (%.1f из %.1f ГБ): часть слоёв будет "
                    "считать процессор — перевод медленнее, процессор загружен", self.preset.id,
                    dev["free_gb"], dev["vram_gb"])
        status("Модель не помещается в свободную видеопамять — часть работы на процессоре, перевод медленнее"
               + (". Поможет модель полегче" if self.fit_vram else ""), None)

    def close(self) -> None:
        if self.server is not None:
            self.server.stop()
            self.server = None
