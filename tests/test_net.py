"""Загрузка больших файлов при плохой сети: обрывы, зависания, мёртвые и «чужие» зеркала, докачка."""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from russificator import net

SIZE = 40 << 20
DATA = os.urandom(1 << 20) * 40          # 40 МБ: несколько кусков по 8 МБ
SHA = hashlib.sha256(DATA).hexdigest()


class Server:
    """Локальный сервер с поломками: ``mode`` по пути — ok | flaky | stall | 404 | other | norange."""

    def __init__(self):
        self.requests = []
        self.flaky_every = 3                    # каждый N-й ответ обрывается на полпути
        self.stall_once = True
        owner = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                mode = self.path.strip("/").split("/")[0]
                owner.requests.append((mode, self.headers.get("Range")))
                if mode == "404":
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                data = DATA if mode != "other" else DATA[:-1] + b"x"
                m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range") or "")
                if mode == "norange" or not m:
                    start, end, code = 0, len(data) - 1, 200
                else:
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else len(data) - 1
                    code = 206
                body = data[start:end + 1]
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                if code == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                self.send_header("X-Linked-ETag", f'"{hashlib.sha256(data).hexdigest()}"')
                self.end_headers()
                n = len(owner.requests)
                try:
                    if mode == "flaky" and len(body) > 1 << 20 and n % owner.flaky_every == 0:
                        self.wfile.write(body[: len(body) // 2])
                        self.connection.close()             # обрыв посреди передачи
                        return
                    if mode == "stall" and len(body) > 1 << 20 and owner.stall_once:
                        owner.stall_once = False
                        self.wfile.write(body[: 1 << 20])
                        time.sleep(3)                       # «зависание» дольше тайм-аута чтения
                        return
                    if mode == "crawl":                     # 64 КБ/с — «провайдер душит» этот сервер
                        for i in range(0, len(body), 16 << 10):
                            self.wfile.write(body[i:i + (16 << 10)])
                            time.sleep(0.25)
                        return
                    if mode == "hang" and self.headers.get("Range") != "bytes=0-0":
                        self.wfile.write(body[: 2 << 20])
                        time.sleep(30)                      # дальше — ни байта
                        return
                    if mode == "slow":                      # ~10 МБ/с — успеваем отменить посреди
                        for i in range(0, len(body), 256 << 10):
                            self.wfile.write(body[i:i + (256 << 10)])
                            time.sleep(0.025)
                        return
                    self.wfile.write(body)
                except OSError:
                    pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, mode: str) -> str:
        return f"{self.base}/{mode}/model.gguf"


@pytest.fixture
def srv(monkeypatch, tmp_path):
    monkeypatch.setattr(net, "READ_TIMEOUT", 1.5)
    monkeypatch.setattr(net, "_LOG_READY", True)          # журнал — в лог pytest, не в папку программы
    monkeypatch.setattr(net, "STALL_LIMIT", 20)
    s = Server()
    yield s
    s.httpd.shutdown()


def _check(path):
    assert path.is_file() and path.stat().st_size == SIZE
    assert hashlib.sha256(path.read_bytes()).hexdigest() == SHA
    assert not path.with_name(path.name + ".part").exists()
    assert not path.with_name(path.name + ".part.json").exists()


def test_flaky_and_stalling_connections_still_finish(srv, tmp_path):
    """Обрывы на полпути и зависшее соединение — загрузка доходит до конца целым файлом, в несколько
    соединений, и никто не сдаётся после пяти неудач."""
    srv.flaky_every = 2
    notes = []
    out = net.download([srv.url("flaky")], tmp_path / "m.gguf")
    _check(out)
    out2 = net.download([srv.url("stall")], tmp_path / "s.gguf", notes=notes.append)
    _check(out2)
    ranges = {r for m, r in srv.requests if m == "flaky"}
    assert len(ranges) > 4                                   # куски, а не один поток
    assert any("127.0.0.1" in n and "замолчал" in n for n in notes)   # окну сказали, что случилось


def test_dead_and_foreign_mirrors_are_skipped(srv, tmp_path):
    """Первое зеркало без файла (404), второе с другой версией файла — берётся третье; файл верный."""
    notes = []
    out = net.download([srv.url("404"), srv.url("ok"), srv.url("other")], tmp_path / "m.gguf", notes=notes.append)
    _check(out)
    assert not [r for m, r in srv.requests if m == "other" and r != "bytes=0-0"]   # «чужое» не качали
    out = net.download([srv.url("norange")], tmp_path / "n.gguf")                 # без кусков — одним потоком
    _check(out)


def test_resume_after_cancel_continues_from_saved_pieces(srv, tmp_path):
    """Отмена посреди загрузки сохраняет куски; следующий вызов докачивает только недостающее."""
    cancel = threading.Event()
    dest = tmp_path / "m.gguf"
    seen = []

    def progress(done, total):
        seen.append(done)
        if done > SIZE // 3:
            cancel.set()

    with pytest.raises(net.Cancelled):
        net.download([srv.url("slow")], dest, progress=progress, cancel=cancel)
    assert dest.with_name("m.gguf.part.json").is_file()
    first = len(srv.requests)
    got = []
    net.download([srv.url("slow")], dest, progress=lambda d, t: got.append(d))
    _check(dest)
    assert SIZE // 4 < got[0] < SIZE                          # продолжили, а не начали заново
    starts = [int(re.match(r"bytes=(\d+)", r).group(1)) for m, r in srv.requests[first:] if r and r != "bytes=0-0"]
    assert starts and min(starts) > 0                        # с начала файла заново не качали


def test_wrong_checksum_is_reported_and_removed(srv, tmp_path, monkeypatch):
    """Сервер сообщил одну сумму, а пришёл другой файл — файл удаляется, ошибка понятная."""
    monkeypatch.setattr(net, "_sha", lambda v: "0" * 64 if v else "")
    with pytest.raises(net.NetError, match="повреждённым"):
        net.download([srv.url("ok")], tmp_path / "m.gguf", sha256="1" * 64)
    assert not (tmp_path / "m.gguf.part").exists()


def test_all_mirrors_down_gives_clear_error(srv, tmp_path):
    with pytest.raises(net.NetError, match="HTTP 404"):
        net.download([srv.url("404"), srv.url("404")], tmp_path / "m.gguf")


def test_throttled_mirror_gives_way_to_fast_one(srv, tmp_path, monkeypatch):
    """Один сервер душат до 64 КБ/с, другой быстрый — медленные соединения переходят на быстрый,
    загрузка не тянется часами."""
    monkeypatch.setattr(net, "SLOW_AFTER", 2)
    started = time.monotonic()
    out = net.download([srv.url("crawl"), srv.url("ok")], tmp_path / "m.gguf")
    _check(out)
    assert time.monotonic() - started < 30
    crawled = [r for m, r in srv.requests if m == "crawl" and r != "bytes=0-0"]
    assert crawled                                            # медленный тоже пробовали


def test_everything_hangs_error_keeps_downloaded_part(srv, tmp_path, monkeypatch):
    """Все серверы замолчали — через STALL_LIMIT понятная ошибка, скачанное остаётся для докачки."""
    monkeypatch.setattr(net, "STALL_LIMIT", 4)
    dest = tmp_path / "m.gguf"
    with pytest.raises(net.NetError) as e:
        net.download([srv.url("hang")], dest)
    msg = str(e.value)
    assert "нет данных" in msg and "Скачать" in msg and "127.0.0.1" in msg
    assert dest.with_name("m.gguf.part").is_file() and dest.with_name("m.gguf.part.json").is_file()
