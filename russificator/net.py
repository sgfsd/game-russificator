"""Сеть без внешних зависимостей: JSON-запросы и докачиваемые загрузки.

Только стандартная библиотека (urllib) — меньше зависимостей, проще
сборка exe. Системный прокси Windows/переменные окружения подхватываются
urllib автоматически.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

USER_AGENT = "GameRussificator/2.0 (+https://github.com)"

#: колбэк прогресса загрузки: (скачано_байт, всего_байт_или_0)
ProgressFn = Callable[[int, int], None]


class NetError(RuntimeError):
    """Ошибка сети/HTTP с понятным текстом и кодом ответа."""

    def __init__(self, message: str, status: int = 0, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class Cancelled(Exception):
    """Операцию отменил пользователь."""


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:  # в собранном exe берём сертификаты из certifi, если он есть
        import certifi  # type: ignore
        ctx.load_verify_locations(certifi.where())
    except Exception:  # noqa: BLE001
        pass
    return ctx


_CTX = _ssl_context()


def _request(url: str, data: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None,
             method: Optional[str] = None) -> urllib.request.Request:
    h = {"User-Agent": USER_AGENT}
    h.update(headers or {})
    return urllib.request.Request(url, data=data, headers=h, method=method)


def _http_error(exc: urllib.error.HTTPError, url: str) -> NetError:
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        body = ""
    detail = body
    try:
        parsed = json.loads(body)
        err = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(err, dict):
            detail = err.get("message") or body
        elif isinstance(err, str):
            detail = err
        elif isinstance(parsed, dict) and parsed.get("detail"):
            detail = str(parsed["detail"])
        elif isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            e0 = parsed[0].get("error", {})
            detail = e0.get("message", body) if isinstance(e0, dict) else body
    except ValueError:
        pass
    detail = " ".join(detail.split())[:300]
    return NetError(f"HTTP {exc.code}: {detail or exc.reason}", status=exc.code, body=body)


def get_json(url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 30) -> Any:
    try:
        with urllib.request.urlopen(_request(url, headers=headers), timeout=timeout,
                                    context=_CTX) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise _http_error(exc, url) from None
    except urllib.error.URLError as exc:
        raise NetError(f"Нет соединения с {_host(url)}: {exc.reason}") from None
    except (TimeoutError, OSError) as exc:
        raise NetError(f"Нет соединения с {_host(url)}: {exc}") from None


def post_json(url: str, payload: Any, headers: Optional[Dict[str, str]] = None,
              timeout: float = 120) -> Any:
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        with urllib.request.urlopen(_request(url, data=data, headers=h, method="POST"),
                                    timeout=timeout, context=_CTX) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise _http_error(exc, url) from None
    except urllib.error.URLError as exc:
        raise NetError(f"Нет соединения с {_host(url)}: {exc.reason}") from None
    except (TimeoutError, OSError) as exc:
        raise NetError(f"Сервер {_host(url)} не ответил: {exc}") from None
    try:
        return json.loads(raw)
    except ValueError:
        raise NetError(f"Сервер {_host(url)} вернул не JSON: {raw[:200]}") from None


def _host(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).netloc or url


# ====================================================================== загрузка файлов
#
# Большие файлы (модели нейросетей — гигабайты) качаются так, чтобы загрузка доходила до конца
# при любой сети:
#   * несколько соединений сразу, каждое — своим куском файла (Range); освободившееся соединение
#     забирает половину самого большого оставшегося куска;
#   * несколько источников одного и того же файла (зеркала): файл одинаковый (размер и SHA-256
#     сверяются), поэтому куски можно брать с разных серверов, а сломавшийся сервер просто не берётся;
#   * обрыв, зависание (30 с без данных), ответ 5xx — повтор с паузой; загрузка идёт, пока хоть
#     откуда-то приходят данные, и сдаётся только после 5 минут без единого байта;
#   * скачанное и раскладка кусков сохраняются (.part + .part.json): «Скачать» ещё раз, перезапуск
#     программы или ПК — загрузка продолжится с того же места;
#   * по окончании файл сверяется с SHA-256 (его сообщает сервер или знает программа);
#   * каждая попытка пишется в журнал logs/downloads.log (он не стирается при запуске программы).

CONNECTIONS = 4               # соединений на большой файл
MULTI_MIN = 16 << 20          # файлы меньше — одним соединением
SEG_MIN = 8 << 20             # на старте файл делится на куски не мельче этого
STEAL_MIN = 1 << 20           # у медленного соединения забираем половину, пока остаток больше 2 МБ
CHUNK = 256 << 10
READ_TIMEOUT = 30             # с без единого байта — соединение зависло
STALL_LIMIT = 300             # с без данных со всех серверов — загрузка не удалась
SLOW_AFTER = 20               # с: медленное соединение уступает источнику в 5+ раз быстрее

log = logging.getLogger("russificator.net")
_LOG_READY = False


def download_log() -> Path:
    from . import paths
    return paths.logs_dir() / "downloads.log"


def _ensure_log() -> None:
    """Отдельный журнал загрузок: дописывается, а не стирается при запуске программы — после
    неудачной загрузки и перезапуска в нём остаётся, что именно пошло не так."""
    global _LOG_READY
    if _LOG_READY:
        return
    _LOG_READY = True
    try:
        f = download_log()
        if f.is_file() and f.stat().st_size > 2 << 20:
            f.replace(f.with_name("downloads.old.log"))
        h = logging.FileHandler(f, mode="a", encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(h)
        if log.level == logging.NOTSET or log.level > logging.INFO:
            log.setLevel(logging.INFO)
    except Exception:  # noqa: BLE001
        pass


def _sha(value: str) -> str:
    v = (value or "").strip().strip('"').lower()
    if v.startswith("w/"):
        v = v[2:].strip('"')
    return v if re.fullmatch(r"[0-9a-f]{64}", v) else ""


def _reason(exc: BaseException) -> str:
    """Причина сетевой ошибки по-человечески (для журнала и окна)."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"сервер ответил ошибкой HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        inner = exc.reason
        if not isinstance(inner, BaseException):
            return f"нет соединения ({inner})"
        exc = inner
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return f"сервер замолчал (нет данных {READ_TIMEOUT} с)"
    if isinstance(exc, ConnectionResetError):
        return "соединение сброшено (связь оборвали провайдер, VPN или антивирус)"
    if isinstance(exc, ConnectionAbortedError):
        return "соединение прервано на этом компьютере (антивирус или брандмауэр)"
    if isinstance(exc, ConnectionRefusedError):
        return "сервер отказал в соединении"
    if isinstance(exc, ssl.SSLError):
        return f"ошибка защищённого соединения (SSL): {exc.reason or exc}"
    if isinstance(exc, socket.gaierror):
        return "адрес сервера не найден (нет интернета или DNS)"
    if isinstance(exc, http.client.IncompleteRead):
        return "сервер оборвал передачу"
    if isinstance(exc, http.client.HTTPException):
        return f"сбой связи ({exc.__class__.__name__})"
    return f"{exc.__class__.__name__}: {exc}"


def _opener(seen: Optional[dict] = None):
    """urllib с нашими сертификатами; ``seen`` собирает заголовки X-Linked-* с переадресаций
    (Hugging Face сообщает SHA-256 файла в ответе-переадресации на CDN)."""
    handlers: list = [urllib.request.HTTPSHandler(context=_CTX)]
    if seen is not None:
        class _Redirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                for k in ("X-Linked-ETag", "X-Linked-Size"):
                    if headers.get(k):
                        seen.setdefault(k.lower(), headers.get(k))
                return super().redirect_request(req, fp, code, msg, headers, newurl)
        handlers.append(_Redirect)
    return urllib.request.build_opener(*handlers)


@dataclass
class RemoteFile:
    url: str
    size: int                  # 0 — сервер не сообщил
    sha256: str                # "" — сервер не сообщил
    ranges: bool               # умеет отдавать куски (докачка, несколько соединений)


def probe(url: str, timeout: float = 20) -> RemoteFile:
    """Что за файл по ссылке: размер, SHA-256, умеет ли сервер отдавать куски."""
    seen: dict = {}
    try:
        with _opener(seen).open(_request(url, headers={"Range": "bytes=0-0"}), timeout=timeout) as resp:
            h = resp.headers
            sha = _sha(h.get("X-Linked-ETag") or seen.get("x-linked-etag") or "")
            m = re.match(r"bytes\s+\d+-\d+/(\d+)", h.get("Content-Range") or "")
            if resp.status == 206 and m:
                return RemoteFile(url, int(m.group(1)), sha, True)
            return RemoteFile(url, int(h.get("Content-Length") or 0), sha, False)
    except urllib.error.HTTPError as exc:
        raise _http_error(exc, url) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise NetError(f"{_host(url)}: {_reason(exc)}") from None


class _Seg:
    __slots__ = ("start", "end", "pos", "busy")

    def __init__(self, start: int, end: int, pos: Optional[int] = None):
        self.start, self.end = start, end
        self.pos = start if pos is None else pos
        self.busy = False

    @property
    def left(self) -> int:
        return max(0, self.end - self.pos)


class _Src:
    def __init__(self, url: str):
        self.url = url
        self.host = _host(url)
        self.fails = 0             # неудач подряд
        self.bytes = 0
        self.seconds = 0.0         # суммарное время соединений (скорость — на одно соединение)
        self.dead = ""             # причина, по которой источник больше не берём
        self.active = 0
        self.retry_at = 0.0
        self.last_error = ""

    def speed(self) -> float:
        """Скорость одного соединения; 0 — пока мало данных для оценки."""
        if self.seconds >= 3 or self.bytes >= 8 << 20:
            return self.bytes / max(self.seconds, 0.05)
        return 0.0


class _Dead(Exception):
    """Источник больше не годится (нет файла, другая версия, не отдаёт кусками)."""


class _Slow(Exception):
    """Соединение сильно медленнее другого источника — переключиться."""


class _DiskError(Exception):
    pass


def _make_sparse(fh) -> None:
    """Разреженный файл (NTFS): запись ближе к концу не заставляет Windows сначала забивать
    нулями гигабайты перед ней."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes
        ret = wintypes.DWORD()
        ctypes.windll.kernel32.DeviceIoControl(wintypes.HANDLE(msvcrt.get_osfhandle(fh.fileno())), 0x000900C4,
                                               None, 0, None, 0, ctypes.byref(ret), None)
    except Exception:  # noqa: BLE001
        pass


def _gb(n: float) -> str:
    return f"{n / 1e9:.2f} ГБ" if n >= 1e8 else f"{n / 1048576:.1f} МБ"


def download(url, dest: Path, progress: Optional[ProgressFn] = None,
             cancel: Optional[threading.Event] = None, sha256: Optional[str] = None,
             expected_size: int = 0, retries: int = 5, notes: Optional[Callable[[str], None]] = None,
             connections: int = CONNECTIONS) -> Path:
    """Скачать файл с докачкой. ``url`` — ссылка или список ссылок на один и тот же файл (зеркала,
    в порядке предпочтения). ``notes`` получает короткие сообщения для окна (обрыв, смена сервера).
    Прерванная загрузка продолжается с того же места при следующем вызове."""
    _ensure_log()
    urls = [url] if isinstance(url, str) else [u for u in url if u]
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    note = notes or (lambda m: None)
    log.info("=== %s: источники %s", dest.name, ", ".join(_host(u) for u in urls))

    # что за файл: первый ответивший источник задаёт размер и SHA-256, остальные с ним сверяются
    srcs = [_Src(u) for u in urls]
    probes: Dict[str, RemoteFile] = {}
    for attempt in range(3):
        for s in srcs:
            if s.url in probes or s.dead:
                continue
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            try:
                rf = probes[s.url] = probe(s.url)
                log.info("%s: размер %s, sha256 %s, куски %s", s.host, rf.size or "?", rf.sha256 or "?",
                         "да" if rf.ranges else "нет")
            except NetError as exc:
                s.last_error = str(exc)
                log.warning("%s: не ответил — %s", s.host, exc)
                if exc.status in (401, 403, 404, 410):
                    s.dead = f"файла нет (HTTP {exc.status})"
        if any(p.size for p in probes.values()) or all(s.dead or s.url in probes for s in srcs):
            break
        if cancel is not None and cancel.wait(3 * (attempt + 1)):
            raise Cancelled()
    info: Optional[RemoteFile] = None
    for s in srcs:
        rf = probes.get(s.url)
        if rf is None:
            continue
        if info is None and rf.size:
            info = rf
        elif info is not None and (rf.size != info.size or (rf.sha256 and info.sha256 and rf.sha256 != info.sha256)):
            s.dead = "на этом сервере другая версия файла"
            log.warning("%s: %s (размер %s)", s.host, s.dead, rf.size)
    if all(s.dead for s in srcs):
        raise NetError(f"Не удалось скачать {dest.name}: " + "; ".join(f"{s.host}: {s.dead}" for s in srcs),
                       status=404 if all("HTTP" in s.dead for s in srcs) else 0)
    if info is None or not info.ranges or info.size < MULTI_MIN:
        alive = [s.url for s in srcs if not s.dead]
        return _stream(alive, dest, progress, cancel, sha256 or (info.sha256 if info else ""), expected_size, retries)
    want_sha = info.sha256
    pinned = _sha(sha256 or "")
    if not want_sha and pinned and (not expected_size or info.size == expected_size):
        want_sha = pinned
    elif want_sha and pinned and want_sha != pinned:
        log.info("на сервере новая версия файла (sha256 %s, программа знала %s)", want_sha, pinned)
    return _segmented(srcs, info.size, want_sha, dest, progress, cancel, note, connections)


def _segmented(srcs: List[_Src], size: int, sha: str, dest: Path, progress: Optional[ProgressFn],
               cancel: Optional[threading.Event], note: Callable[[str], None], connections: int) -> Path:
    part = dest.with_name(dest.name + ".part")
    state_file = dest.with_name(dest.name + ".part.json")
    segs = _load_state(state_file, part, size, sha)
    if segs is None:
        have = part.stat().st_size if part.is_file() else 0
        if have > size:
            have = 0
        try:
            with part.open("r+b" if have else "wb") as fh:
                _make_sparse(fh)
                fh.truncate(size)
        except OSError as exc:
            raise NetError(f"Не удалось создать файл {part}: {exc}") from None
        segs = [_Seg(0, size, have)]           # старая загрузка (.part без раскладки) — начало уже есть
    n_conn = max(1, min(connections, size // SEG_MIN))
    _split(segs, n_conn)
    done0 = size - sum(s.left for s in segs)
    if done0:
        log.info("продолжаю: уже скачано %s из %s", _gb(done0), _gb(size))
    lock = threading.Lock()
    stop = threading.Event()
    fatal: List[str] = []
    last_data = [time.monotonic()]
    started = time.monotonic()

    def save_state() -> None:
        with lock:
            data = {"v": 1, "size": size, "sha256": sha, "segs": [[s.start, s.end, s.pos] for s in segs if s.left]}
        try:
            tmp = state_file.with_name(state_file.name + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(state_file)
        except OSError:
            pass

    def take() -> Optional[_Seg]:
        with lock:
            free = [s for s in segs if not s.busy and s.left]
            if free:
                seg = max(free, key=lambda s: s.left)
                seg.busy = True
                return seg
            big = max((s for s in segs if s.busy and s.left >= 2 * STEAL_MIN), key=lambda s: s.left, default=None)
            if big is None:
                return None
            mid = big.pos + big.left // 2
            seg = _Seg(mid, big.end)
            big.end = mid
            seg.busy = True
            segs.append(seg)
            return seg

    def pick() -> Optional[_Src]:
        """Источник для следующего запроса: сначала пробуем каждый, потом — самый быстрый из тех,
        что сейчас не на паузе после ошибки. Все на паузе — ждём ближайшего."""
        while not stop.is_set():
            with lock:
                alive = [s for s in srcs if not s.dead]
                if not alive:
                    return None
                now = time.monotonic()
                ready = [s for s in alive if s.retry_at <= now]
                if ready:
                    fresh = [s for s in ready if s.seconds < 3 and s.active == 0 and not s.fails]
                    src = fresh[0] if fresh else max(
                        ready, key=lambda s: ((s.speed() + 1.0) / (1 + 3 * s.fails), -s.active))
                    src.active += 1
                    return src
                wait = min(s.retry_at for s in alive) - now
            stop.wait(min(max(wait, 0.2), 2.0))
        return None

    def fail(src: _Src, exc: BaseException, n: int, got: int, seg: _Seg) -> None:
        with lock:
            src.fails += 1
            pause = min(2 ** min(src.fails, 5), 30)
            src.retry_at = time.monotonic() + pause
            src.last_error = _reason(exc)
            fails = src.fails
            others = [s.host for s in srcs if s is not src and not s.dead]
        log.warning("соединение %d: %s — %s (в этой попытке %s, кусок до %s), повтор через %d с", n, src.host,
                    _reason(exc), _gb(got), _gb(seg.end), pause)
        if fails in (1, 3, 6):
            note(f"{src.host}: {_reason(exc)} — " +
                 (f"продолжаю, качаю и с {', '.join(others)}" if others else "переподключаюсь и продолжаю"))

    def fetch(seg: _Seg, src: _Src, n: int) -> None:
        t0 = tick = time.monotonic()
        got = 0
        seen: dict = {}
        try:
            with lock:
                start, end = seg.pos, seg.end
            if start >= end:
                return
            req = _request(src.url, headers={"Range": f"bytes={start}-{end - 1}"})
            with _opener(seen).open(req, timeout=READ_TIMEOUT) as resp:
                tag = _sha(resp.headers.get("X-Linked-ETag") or seen.get("x-linked-etag") or "")
                if sha and tag and tag != sha:
                    raise _Dead("на сервере другая версия файла")
                m = re.match(r"bytes\s+(\d+)-\d+/(\d+)", resp.headers.get("Content-Range") or "")
                if resp.status != 206 or not m or int(m.group(1)) != start:
                    raise _Dead("сервер не отдаёт файл кусками")
                if int(m.group(2)) != size:
                    raise _Dead("на сервере другой размер файла")
                try:
                    fh = part.open("r+b", buffering=0)
                except OSError as exc:
                    raise _DiskError(str(exc)) from None
                with fh:
                    fh.seek(start)
                    while not stop.is_set():
                        chunk = resp.read(CHUNK)
                        if not chunk:
                            break
                        with lock:
                            room = seg.end - seg.pos
                        if room <= 0:
                            break
                        chunk = chunk[:room]
                        try:
                            fh.write(chunk)
                        except OSError as exc:
                            raise _DiskError(str(exc)) from None
                        now = time.monotonic()
                        with lock:
                            seg.pos += len(chunk)
                            src.bytes += len(chunk)
                            src.seconds += now - tick      # скорость считается и по идущим соединениям
                            tick = now
                            got += len(chunk)
                            last_data[0] = now
                            if got >= 1 << 20:
                                src.fails = 0
                            finished = seg.pos >= seg.end
                            best = max((s.speed() for s in srcs if s is not src and not s.dead), default=0.0)
                        if finished:
                            break
                        elapsed = now - t0
                        if elapsed > SLOW_AFTER and best > 5 * (got / elapsed) + 65536:
                            raise _Slow()
            with lock:
                cut = bool(seg.left) and not stop.is_set()
            if cut and got < 1 << 20:               # ответ оборвался почти сразу — это сбой
                raise ConnectionError("сервер оборвал передачу")
            if cut:
                log.info("соединение %d: %s оборвал передачу после %s — докачиваю", n, src.host, _gb(got))
        except _Slow:
            log.info("соединение %d: %s медленно (%.0f КБ/с) — перехожу на сервер побыстрее", n, src.host,
                     got / max(1e-3, time.monotonic() - t0) / 1024)
        except _Dead as exc:
            with lock:
                first = not src.dead
                src.dead = str(exc)
                others = [s.host for s in srcs if not s.dead]
            if first:
                log.warning("%s больше не используется: %s", src.host, exc)
                note(f"{src.host}: {exc}" + (f" — качаю с {', '.join(others)}" if others else ""))
        except _DiskError as exc:
            fatal.append(f"Не удалось записать файл на диск ({exc}). Проверьте свободное место и что "
                         f"антивирус не блокирует папку {part.parent}.")
            stop.set()
        except urllib.error.HTTPError as exc:
            fail(src, exc, n, got, seg)
            with lock:
                if exc.code in (401, 403, 404, 410):
                    src.dead = f"файла нет (HTTP {exc.code})"
                elif exc.code == 429 or exc.code >= 500:
                    after = exc.headers.get("Retry-After") if exc.headers else None
                    if after and after.isdigit():
                        src.retry_at = max(src.retry_at, time.monotonic() + min(int(after), 60))
        except (urllib.error.URLError, OSError, http.client.HTTPException, ConnectionError, ValueError) as exc:
            fail(src, exc, n, got, seg)
        finally:
            with lock:
                src.active -= 1
                src.seconds += time.monotonic() - tick

    def worker(n: int) -> None:
        while not stop.is_set():
            seg = take()
            if seg is None:
                return
            try:
                while seg.left and not stop.is_set():
                    src = pick()
                    if src is None:
                        return
                    fetch(seg, src, n)
            finally:
                with lock:
                    seg.busy = False

    threads = [threading.Thread(target=worker, args=(i + 1,), name=f"dl-{i + 1}", daemon=True)
               for i in range(n_conn)]
    for t in threads:
        t.start()
    last_save = last_report = 0.0
    stalled = False
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(0.25)
            now = time.monotonic()
            if cancel is not None and cancel.is_set():
                stop.set()
            if now - last_data[0] > STALL_LIMIT:
                stalled = True
                stop.set()
            if progress and now - last_report >= 0.25:
                last_report = now
                with lock:
                    done = size - sum(s.left for s in segs)
                progress(done, size)
            if now - last_save >= 2:
                last_save = now
                save_state()
    finally:
        stop.set()
        for t in threads:
            t.join(READ_TIMEOUT + 5)
        save_state()
    with lock:
        left = sum(s.left for s in segs)
    done = size - left
    secs = time.monotonic() - started
    log.info("итог: скачано %s из %s за %.0f с (%.1f МБ/с); источники: %s", _gb(done), _gb(size), secs,
             (done - done0) / max(secs, 1e-3) / 1048576,
             "; ".join(f"{s.host} {_gb(s.bytes)}" + (f" [{s.dead}]" if s.dead else "") for s in srcs))
    if fatal:
        raise NetError(fatal[0])
    if cancel is not None and cancel.is_set() and left:
        log.info("отменено пользователем — скачанное сохранено")
        raise Cancelled()
    if left:
        reasons = "; ".join(f"{s.host}: {s.dead or s.last_error or 'нет ответа'}" for s in srcs)
        kept = (f"Скачанное сохранено ({_gb(done)} из {_gb(size)}) — нажмите «Скачать» ещё раз, загрузка "
                f"продолжится с того же места.")
        if all(s.dead for s in srcs):
            raise NetError(f"Файл недоступен ни на одном сервере ({reasons}). {kept}")
        what = f"{STALL_LIMIT // 60} минут нет данных ни с одного сервера" if stalled else "Загрузка прервалась"
        raise NetError(f"{what} ({reasons}). {kept}")
    if progress:
        progress(size, size)
    if sha:
        note("Проверяю целостность файла…")
        got_sha = _file_sha256(part, cancel)
        if got_sha != sha:
            log.error("контрольная сумма не совпала: %s вместо %s — файл удалён", got_sha, sha)
            part.unlink(missing_ok=True)
            state_file.unlink(missing_ok=True)
            raise NetError(f"Файл {dest.name} скачался повреждённым (не совпала контрольная сумма) и удалён — "
                           "нажмите «Скачать» ещё раз.")
        log.info("sha256 совпала")
    state_file.unlink(missing_ok=True)
    _finish(part, dest)
    return dest


def partial_bytes(dest: Path) -> int:
    """Сколько уже скачано у незаконченной загрузки ``dest`` (для проверки места и окна)."""
    dest = Path(dest)
    part, state = dest.with_name(dest.name + ".part"), dest.with_name(dest.name + ".part.json")
    try:
        if state.is_file():
            st = json.loads(state.read_text(encoding="utf-8"))
            return max(0, int(st["size"]) - sum(int(b) - int(c) for _a, b, c in st.get("segs", [])))
        return part.stat().st_size if part.is_file() else 0
    except (OSError, ValueError, TypeError, KeyError):
        return 0


def _split(segs: List[_Seg], n: int) -> None:
    """Разделить оставшееся на ``n`` кусков (чтобы сразу качать в несколько соединений)."""
    while len([s for s in segs if s.left]) < n:
        big = max(segs, key=lambda s: s.left)
        if big.left < 2 * SEG_MIN:
            return
        mid = big.pos + big.left // 2
        segs.append(_Seg(mid, big.end))
        big.end = mid


def _load_state(state_file: Path, part: Path, size: int, sha: str) -> Optional[List[_Seg]]:
    if not state_file.exists():
        return None
    try:
        st = json.loads(state_file.read_text(encoding="utf-8"))
        if st.get("size") != size or (sha and st.get("sha256") and st["sha256"] != sha):
            raise ValueError("другой файл")
        if part.stat().st_size != size:
            raise ValueError("размер .part")
        segs = [_Seg(int(a), int(b), int(c)) for a, b, c in st.get("segs", [])]
        if any(not (0 <= s.start <= s.pos <= s.end <= size) for s in segs):
            raise ValueError("раскладка")
        return segs or [_Seg(size, size)]
    except (OSError, ValueError, TypeError, KeyError) as exc:
        log.info("раскладка прошлой загрузки не подошла (%s) — начинаю заново", exc)
        state_file.unlink(missing_ok=True)
        part.unlink(missing_ok=True)
        return None


def _file_sha256(path: Path, cancel: Optional[threading.Event] = None) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(4 << 20), b""):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            h.update(block)
    return h.hexdigest()


def _finish(part: Path, dest: Path) -> None:
    """.part -> готовый файл (антивирус может пару секунд держать свежий файл — ждём)."""
    for attempt in range(10):
        try:
            if dest.exists():
                dest.unlink()
            os.replace(part, dest)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(1)


def _stream(urls: List[str], dest: Path, progress: Optional[ProgressFn], cancel: Optional[threading.Event],
            sha256: str, expected_size: int, retries: int) -> Path:
    """Одним соединением (маленький файл или сервер без кусков): докачка, если сервер умеет,
    повторы, пока есть прогресс, источники по кругу."""
    part = dest.with_name(dest.name + ".part")
    dest.with_name(dest.name + ".part.json").unlink(missing_ok=True)
    fails = 0
    k = 0
    while True:
        url = urls[k % len(urls)]
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        got = 0
        try:
            with _opener().open(_request(url, headers=headers), timeout=READ_TIMEOUT * 2) as resp:
                if have and resp.status != 206:
                    have = 0  # сервер не умеет докачку — начинаем заново
                length = int(resp.headers.get("Content-Length") or 0)
                total = (have + length) if length else expected_size
                done = have
                last = 0.0
                with part.open("ab" if have else "wb") as fh:
                    while True:
                        if cancel is not None and cancel.is_set():
                            raise Cancelled()
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        fh.write(chunk)
                        done += len(chunk)
                        got += len(chunk)
                        now = time.monotonic()
                        if progress and now - last > 0.2:
                            last = now
                            progress(done, total)
                if total and done < total:
                    raise NetError(f"загрузка оборвалась ({done} из {total} байт)")
                if progress:
                    progress(done, total or done)
            break
        except Cancelled:
            raise
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and have:  # уже всё скачано
                break
            last_err = f"{_host(url)}: {_reason(exc)}"
            if exc.code in (401, 403, 404, 410):          # файла там нет — этот источник больше не берём
                urls = [u for u in urls if u != url]
                if not urls:
                    raise _http_error(exc, url) from None
                k -= 1
        except NetError as exc:
            last_err = f"{_host(url)}: {exc}"
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            last_err = f"{_host(url)}: {_reason(exc)}"
        fails = 0 if got >= 1 << 20 else fails + 1
        log.warning("%s: %s (скачано в попытке %s)", dest.name, last_err, _gb(got))
        if fails > retries * len(urls):
            raise NetError(f"Не удалось скачать {dest.name}: {last_err}")
        k += 1
        if cancel is not None and cancel.wait(min(2 ** min(fails, 5), 20)):
            raise Cancelled()
        if cancel is None:
            time.sleep(min(2 ** min(fails, 5), 20))

    if sha256 and _sha(sha256):
        if _file_sha256(part, cancel) != _sha(sha256):
            part.unlink(missing_ok=True)
            raise NetError(f"Файл {dest.name} повреждён (не совпала контрольная сумма) — скачайте заново")
    _finish(part, dest)
    return dest
