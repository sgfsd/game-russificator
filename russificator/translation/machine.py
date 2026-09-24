"""Способ 1: машинный переводчик офлайн — для слабых и старых ПК.

Нейросетевой машинный перевод (модель Argos Translate en→ru, ~190 МБ,
формат CTranslate2) без Python-пакета argostranslate — только
``ctranslate2`` + ``sentencepiece``. Работает на процессоре без видеокарты
и без AVX2, памяти берёт ~500 МБ. Модель скачивается один раз.

Что делается поверх «голой» модели:
  * UI-строки («Save», «Load Game») берутся из встроенного глоссария —
    на коротких фразах без контекста модель ошибается;
  * служебные вставки (теги, плейсхолдеры, коды движков) маскируются
    маркерами ``@N``, а после перевода проверяются; если модель маркер
    потеряла — строка переводится по кускам между вставками;
  * многострочный текст переводится построчно, длинные строки — по
    предложениям; КАПС переводится в обычном регистре и возвращается в КАПС.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .. import net, paths
from ..core.universal import Entry
from . import markup
from .base import StatusFn, Translator, TranslatorError, _noop
from .glossary import match_case, ui_phrase

log = logging.getLogger("russificator.machine")

ARGOS_INDEX = "https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json"
FALLBACK_URL = "https://argos-net.com/v1/translate-en_ru-1_9.argosmodel"
APPROX_SIZE_MB = 195

_SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+(?=[\"'«(\[@#]?[A-Z0-9])")


def model_dir() -> Path:
    return paths.models_dir() / "argos-en-ru"


def is_installed() -> bool:
    d = model_dir()
    return (d / "model" / "model.bin").is_file() and (d / "sentencepiece.model").is_file()


def installed_size_mb() -> int:
    d = model_dir()
    if not d.is_dir():
        return 0
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) // (1 << 20)


def _resolve_url() -> str:
    """Актуальная ссылка на модель из индекса Argos (с запасной ссылкой)."""
    try:
        index = net.get_json(ARGOS_INDEX, timeout=15)
        pkgs = [p for p in index if p.get("from_code") == "en" and p.get("to_code") == "ru" and p.get("links")]
        pkgs.sort(key=lambda p: [int(x) for x in re.findall(r"\d+", str(p.get("package_version", "0")))],
                  reverse=True)
        if pkgs:
            return pkgs[0]["links"][0]
    except Exception as exc:  # noqa: BLE001
        log.info("Индекс Argos недоступен (%s) — берём запасную ссылку", exc)
    return FALLBACK_URL


def install(status: StatusFn = _noop, cancel: Optional[threading.Event] = None) -> None:
    """Скачать и распаковать модель (докачивается при обрыве)."""
    target = model_dir()
    archive = paths.models_dir() / "argos-en-ru.argosmodel"
    url = _resolve_url()

    def progress(done: int, total: int) -> None:
        mb, tot = done >> 20, (total >> 20) or APPROX_SIZE_MB
        status(f"Скачивание модели перевода: {mb} из {tot} МБ", done / total if total else None)

    status("Скачивание модели перевода…", 0.0)
    net.download(url, archive, progress=progress, cancel=cancel)
    status("Распаковка модели…", None)
    tmp = target.with_name(target.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                parts = Path(info.filename).parts
                if len(parts) < 2 or info.is_dir():
                    continue
                rel = Path(*parts[1:])
                if rel.parts[0] not in ("model", "sentencepiece.model", "metadata.json"):
                    continue  # stanza и README не нужны
                dest = tmp / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, dest.open("wb") as out:
                    shutil.copyfileobj(src, out)
    except zipfile.BadZipFile:
        archive.unlink(missing_ok=True)
        raise TranslatorError("Архив модели повреждён — скачайте заново.")
    if not (tmp / "model" / "model.bin").is_file():
        raise TranslatorError("В архиве нет модели перевода — формат Argos изменился.")
    shutil.rmtree(target, ignore_errors=True)
    tmp.rename(target)
    archive.unlink(missing_ok=True)
    status("Модель перевода установлена", 1.0)


def remove() -> None:
    shutil.rmtree(model_dir(), ignore_errors=True)


class MachineTranslator(Translator):
    title = "Машинный перевод (офлайн)"
    context_aware = False
    max_batch_items = 48
    max_batch_chars = 5000
    parallel = 1

    def __init__(self, threads: int = 0):
        self.threads = threads or min(8, os.cpu_count() or 2)
        self._tr = None
        self._sp = None
        self.cache_id = "mt:argos-en-ru"

    # ---------- подготовка ----------

    def prepare(self, status: StatusFn = _noop, cancel: Optional[threading.Event] = None) -> None:
        try:
            import ctranslate2  # noqa: F401
            import sentencepiece  # noqa: F401
        except ImportError as exc:
            raise TranslatorError(f"Не установлены компоненты машинного перевода ({exc.name}). "
                                  "Установите: pip install ctranslate2 sentencepiece")
        if not is_installed():
            install(status, cancel)
        if self._tr is None:
            status("Загрузка модели перевода…", None)
            self._load()
        status("Машинный переводчик готов", 1.0)

    def _load(self) -> None:
        import ctranslate2
        import sentencepiece as spm
        d = model_dir()
        try:
            meta = json.loads((d / "metadata.json").read_text(encoding="utf-8"))
            self.cache_id = f"mt:argos-en-ru-{meta.get('package_version', '1')}"
        except (OSError, ValueError):
            pass
        types = ctranslate2.get_supported_compute_types("cpu")
        compute = "int8" if "int8" in types else "float32"
        try:
            self._tr = ctranslate2.Translator(str(d / "model"), device="cpu", compute_type=compute,
                                              inter_threads=1, intra_threads=self.threads)
        except Exception as exc:  # noqa: BLE001
            raise TranslatorError(f"Не удалось загрузить модель перевода: {exc}. "
                                  "Попробуйте удалить модель и скачать заново.")
        self._sp = spm.SentencePieceProcessor(model_file=str(d / "sentencepiece.model"))

    # ---------- перевод ----------

    def _run(self, sentences: List[str]) -> List[str]:
        """Перевести список предложений моделью (без всякой обработки)."""
        if not sentences:
            return []
        pieces = self._sp.encode(sentences, out_type=str)
        results = self._tr.translate_batch(pieces, beam_size=2, max_batch_size=32,
                                           max_decoding_length=min(512, max(len(p) for p in pieces) * 3 + 10))
        return [self._sp.decode(r.hypotheses[0]) for r in results]

    def translate_texts(self, texts: List[str]) -> List[str]:
        """Перевести произвольные строки (с защитой разметки). Для тестов и сервера перевода."""
        out: List[Optional[str]] = [None] * len(texts)
        plans: List[Tuple[int, List[Tuple[str, List[str], str, List[Tuple[str, str, str, bool]]]]]] = []
        queue: List[str] = []
        index: Dict[str, int] = {}

        def enqueue(s: str) -> None:
            if s not in index:
                index[s] = len(queue)
                queue.append(s)

        for i, text in enumerate(texts):
            ready = ui_phrase(text)
            if ready is not None:
                out[i] = ready
                continue
            lines = []
            for line in text.split("\n"):
                masked, originals, sign = markup.mask(line)
                sentences = []
                for sent in self._sentences(masked):
                    lead, core, trail = _strip(sent)
                    caps = _is_caps(core)
                    src = core.lower().capitalize() if caps else core
                    if markup.has_words(core.replace(sign, " ")):
                        enqueue(src)
                    sentences.append((lead, src, trail, caps))
                lines.append((masked, originals, sign, sentences))
            plans.append((i, lines))

        done = self._run(queue)

        for i, lines in plans:
            result_lines = []
            ok_all = True
            for masked, originals, sign, sentences in lines:
                parts = []
                for lead, src, trail, caps in sentences:
                    if src in index:
                        tr = done[index[src]]
                        tr = tr.upper() if caps else match_case(tr, src) if src[:1].isalpha() else tr
                    else:
                        tr = src
                    parts.append(lead + tr + trail)
                joined = "".join(parts)
                restored, ok = markup.unmask(joined, originals, sign, masked_source=masked)
                if not ok:
                    ok_all = False
                result_lines.append(restored)
            if ok_all:
                out[i] = "\n".join(result_lines)
            else:
                out[i] = self._translate_by_segments(texts[i])
        return [o if o is not None else t for o, t in zip(out, texts)]

    def _translate_by_segments(self, text: str) -> str:
        """Запасной путь: переводим куски между вставками по отдельности."""
        lines = []
        for line in text.split("\n"):
            chunks = markup.split_by_markup(line)
            todo = [c for is_tag, c in chunks if not is_tag and markup.has_words(c)]
            done = dict(zip(todo, self._run([c.strip() for c in todo])))
            rebuilt = []
            for is_tag, c in chunks:
                if not is_tag and c in done:
                    lead, _, trail = _strip(c)
                    rebuilt.append(lead + done[c] + trail)
                else:
                    rebuilt.append(c)
            lines.append("".join(rebuilt))
        return "\n".join(lines)

    @staticmethod
    def _sentences(line: str) -> List[str]:
        if len(line) < 160:
            return [line]
        parts = _SENT_SPLIT.split(line)
        # возвращаем пробелы-разделители, чтобы склейка была точной
        out, pos = [], 0
        for p in parts:
            start = line.index(p, pos)
            if out:
                out[-1] += line[pos:start]
            out.append(p)
            pos = start + len(p)
        if out:
            out[-1] += line[pos:]
        return out or [line]

    def translate(self, entries: List[Entry], glossary: Dict[str, str]) -> Dict[str, str]:
        if self._tr is None:
            self.prepare()
        out: Dict[str, str] = {}
        todo: List[Entry] = []
        for e in entries:
            forced = glossary.get(e.source.strip())
            if forced:
                out[e.id] = forced
            else:
                todo.append(e)
        for e, tr in zip(todo, self.translate_texts([e.source for e in todo])):
            out[e.id] = tr
        return out

    def close(self) -> None:
        self._tr = None
        self._sp = None


def _strip(s: str) -> Tuple[str, str, str]:
    core = s.strip()
    if not core:
        return s, "", ""
    lead = s[: len(s) - len(s.lstrip())]
    trail = s[len(s.rstrip()):]
    return lead, core, trail


def _is_caps(s: str) -> bool:
    letters = [c for c in s if c.isalpha()]
    return len(letters) > 3 and all(c.isupper() for c in letters)
