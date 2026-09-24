"""Сеть без внешних зависимостей: JSON-запросы и докачиваемые загрузки.

Только стандартная библиотека (urllib) — меньше зависимостей, проще
сборка exe. Системный прокси Windows/переменные окружения подхватываются
urllib автоматически.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional

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


def download(url: str, dest: Path, progress: Optional[ProgressFn] = None,
             cancel: Optional[threading.Event] = None, sha256: Optional[str] = None,
             expected_size: int = 0, retries: int = 5) -> Path:
    """Скачать файл с докачкой (.part + Range) и повторными попытками.

    Прерванная загрузка продолжается с того же места при следующем вызове.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    attempt = 0
    while True:
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with urllib.request.urlopen(_request(url, headers=headers), timeout=60,
                                        context=_CTX) as resp:
                status = resp.status
                if have and status != 206:
                    have = 0  # сервер не умеет докачку — начинаем заново
                length = int(resp.headers.get("Content-Length") or 0)
                total = (have + length) if length else expected_size
                mode = "ab" if have else "wb"
                done = have
                last = 0.0
                with part.open(mode) as fh:
                    while True:
                        if cancel is not None and cancel.is_set():
                            raise Cancelled()
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        fh.write(chunk)
                        done += len(chunk)
                        now = time.monotonic()
                        if progress and now - last > 0.2:
                            last = now
                            progress(done, total)
                if total and done < total:
                    raise NetError(f"Загрузка оборвалась ({done} из {total} байт)")
                if progress:
                    progress(done, total or done)
            break
        except Cancelled:
            raise
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and have:  # уже всё скачано
                break
            err = _http_error(exc, url)
            if exc.code in (401, 403, 404) or attempt >= retries:
                raise err from None
        except (urllib.error.URLError, TimeoutError, OSError, NetError) as exc:
            if attempt >= retries:
                raise NetError(f"Не удалось скачать {dest.name}: {exc}") from None
        attempt += 1
        time.sleep(min(2 ** attempt, 20))

    if sha256:
        h = hashlib.sha256()
        with part.open("rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        if h.hexdigest().lower() != sha256.lower():
            part.unlink(missing_ok=True)
            raise NetError(f"Файл {dest.name} повреждён (не совпала контрольная сумма) — скачайте заново")
    if dest.exists():
        dest.unlink()
    shutil.move(str(part), str(dest))
    return dest
