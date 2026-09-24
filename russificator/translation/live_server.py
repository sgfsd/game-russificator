"""Сервер «живого» перевода для Unity-игр (эндпоинт CustomTranslate XUnity).

Пока программа открыта, XUnity.AutoTranslator присылает сюда строки, которых
нет в заранее подготовленном словаре (текст, собранный кодом игры на лету,
новые диалоги и т.п.): ``GET /translate?from=en&to=ru&text=...``, ответ —
перевод обычным текстом. Переводит выбранный пользователем переводчик,
результаты попадают в общую память переводов, а XUnity сам сохраняет их в
файл игры — при следующем запуске программа уже не нужна.

Важно для надёжности: после 5 ошибок подряд XUnity отключает перевод до
конца сессии игры. Поэтому сервер начинает принимать запросы сразу, ещё до
готовности переводчика (запрос ждёт, пока модель загрузится), а строку,
которую перевести нельзя в принципе (сломалась разметка), возвращает как
есть — ошибкой отвечает только на временные сбои (сеть, модель).
"""

from __future__ import annotations

import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from ..core.universal import Entry, TextKind
from . import markup
from .base import Translator
from .filters import looks_technical, looks_translatable
from .memory import TranslationMemory
from .service import translate_by_segments

log = logging.getLogger("russificator.live")


class LiveServer:
    def __init__(self, translator: Optional[Translator], memory: Optional[TranslationMemory] = None,
                 on_event: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.translator = translator
        self.memory = memory
        self.on_event = on_event or (lambda ev: None)
        self.count = 0
        self._lock = threading.Lock()
        self._mem_lock = threading.Lock()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._ready.set()
        self._prepare_error: Optional[str] = None

    #: сколько запрос ждёт готовности переводчика (XUnity ждёт ответа до 150 с)
    READY_TIMEOUT = 120.0

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def start(self, port: int, prepare: Optional[Callable[[], None]] = None,
              factory: Optional[Callable[[], Tuple[Translator, Optional[TranslationMemory]]]] = None) -> None:
        """Начать принимать запросы сразу; подготовка идёт в фоне — запросы её дождутся.

        ``prepare`` — загрузка модели уже созданного переводчика; ``factory`` — создать
        переводчик и память целиком в фоне (так порт открывается за доли секунды).
        """
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                url = urlparse(self.path)
                if url.path.rstrip("/") == "/health":
                    return self._reply(200, "ok")
                if url.path.rstrip("/") != "/translate":
                    return self._reply(404, "not found")
                text = (parse_qs(url.query, keep_blank_values=True).get("text") or [""])[0]
                result = server.translate(text)
                if result is None:
                    return self._reply(503, "translation failed")
                self._reply(200, result)

            def _reply(self, code: int, body: str) -> None:
                raw = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args) -> None:
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._httpd.daemon_threads = True
        if prepare is not None or factory is not None:
            self._ready.clear()

            def warmup() -> None:
                try:
                    if factory is not None:
                        self.translator, self.memory = factory()
                    if prepare is not None:
                        prepare()
                except Exception as exc:  # noqa: BLE001
                    log.warning("Переводчик для живого перевода не готов: %s", exc)
                    self._prepare_error = str(exc) or exc.__class__.__name__
                finally:
                    self._ready.set()

            threading.Thread(target=warmup, name="live-prepare", daemon=True).start()
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="live-translate", daemon=True)
        self._thread.start()
        log.info("Живой перевод слушает 127.0.0.1:%d", port)

    def wait_ready(self, timeout: Optional[float] = None) -> bool:
        """Дождаться переводчика. False — не успел или не смог подготовиться."""
        return self._ready.wait(timeout) and self._prepare_error is None

    @property
    def prepare_error(self) -> Optional[str]:
        return self._prepare_error

    def translate(self, text: str) -> Optional[str]:
        # ники, коды, журналы и пути игра показывает как есть — возвращаем без перевода
        if not looks_translatable(text) or looks_technical(text):
            return text
        if not self.wait_ready(self.READY_TIMEOUT):   # cache_id уточняется при подготовке переводчика
            return None
        key = (text, None)
        if self.memory is not None:
            try:
                with self._mem_lock:
                    hit = self.memory.get_many(self.translator.cache_id, [key]).get(key)
            except Exception:  # noqa: BLE001
                hit = None
            if hit:
                return hit
        started = time.monotonic()
        try:
            with self._lock:  # переводчики (особенно локальная нейросеть) — по одному запросу
                out = self.translator.translate([Entry(id="live", source=text, kind=TextKind.OTHER)], {})
        except Exception as exc:  # noqa: BLE001
            log.warning("Живой перевод не удался: %s", exc)
            return None
        tr = out.get("live")
        if tr:
            tr = markup.normalize_layout(text, tr)
        if not tr or not markup.same_markup(text, tr):
            # перевод потерял разметку — переводим только текст между вставками
            try:
                with self._lock:
                    tr = translate_by_segments(self.translator, Entry(id="live", source=text,
                                                                       kind=TextKind.OTHER), {})
            except Exception:  # noqa: BLE001
                tr = None
        if not tr or not markup.same_markup(text, tr):
            # не вышло и так — отдаём оригинал: повтор даст то же самое, а ошибки XUnity
            # копит и после пяти подряд отключает перевод целиком
            log.info("Живой перевод пропущен (разметка): %r", text[:80])
            return text
        log.debug("живой перевод за %.1f с", time.monotonic() - started)
        if self.memory is not None:
            try:
                with self._mem_lock:
                    self.memory.put_many(self.translator.cache_id, {key: tr})
            except Exception:  # noqa: BLE001
                pass
        self.count += 1
        self.on_event({"type": "live", "count": self.count, "source": text[:120], "translation": tr[:120]})
        return tr

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        try:
            if self.translator is not None:
                self.translator.close()
        except Exception:  # noqa: BLE001
            pass
        if self.memory is not None:
            self.memory.close()
            self.memory = None
