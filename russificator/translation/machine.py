"""Способ 1: машинный переводчик офлайн — для слабых и старых ПК.

Нейросетевой машинный перевод в формате CTranslate2 — только ``ctranslate2`` +
``sentencepiece``, на процессоре, без видеокарты и без AVX2. Модель скачивается один раз.

Основная модель — OPUS-MT tc-big en→ru (Helsinki-NLP, CC-BY 4.0, ~240 МБ): большая модель
(transformer-big), заметно грамотнее базовой — падежи, согласование, «клавиша», а не «ключ», — и
на процессоре почти так же быстра (~0,2 с на предложение на старом Xeon). Скачивается готовой
сборкой CTranslate2 с HuggingFace (закреплённая версия), а если её нет — официальный архив
модели конвертируется прямо здесь (:class:`ctranslate2.converters.OpusMTConverter`).
Запасная модель — Argos Translate en→ru (~190 МБ, базовая OPUS-MT): ею переводится, пока
основная ещё не скачана.

Что делается поверх «голой» модели:
  * UI-строки («Save», «Load Game») берутся из встроенного глоссария —
    на коротких фразах без контекста модель ошибается;
  * служебные вставки (теги, плейсхолдеры, коды движков) маскируются
    маркерами ``@N``, а после перевода проверяются; если модель маркер
    потеряла — строка переводится по кускам между вставками;
  * многострочный текст переводится построчно, строки — по предложениям (большая модель
    теряет часть реплики из нескольких предложений); КАПС переводится в обычном регистре и
    возвращается в КАПС.
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

#: основная модель: OPUS-MT tc-big en→zle (английский → русский/украинский/белорусский), CTranslate2 int8
BIG_NAME = "opus-mt-tc-big-en-ru"
BIG_REPO = "https://huggingface.co/NothingSoftware/opus-mt-tc-big-en-zle-ct2-int8/resolve/" \
           "60e35c8d683875ff7ead041d6b9c2ef69237e4e7/"
BIG_FILES = ("config.json", "shared_vocabulary.json", "source.spm", "target.spm", "model.bin")
#: официальный архив той же модели (Tatoeba-MT, Helsinki-NLP) — если сборки выше нет
BIG_OFFICIAL = "https://object.pouta.csc.fi/Tatoeba-MT-models/eng-zle/" \
               "opusTCv20210807+bt_transformer-big_2022-03-13.zip"
BIG_TARGET = ">>rus<<"
APPROX_SIZE_MB = 245

_SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+(?=[\"'«(\[@#]?[A-Z0-9])")


def model_dir() -> Path:
    """Папка запасной модели (Argos)."""
    return paths.models_dir() / "argos-en-ru"


def big_dir() -> Path:
    """Папка основной модели (OPUS-MT tc-big)."""
    return paths.models_dir() / BIG_NAME


def big_installed() -> bool:
    d = big_dir()
    return all((d / f).is_file() for f in ("model.bin", "source.spm", "target.spm"))


def argos_installed() -> bool:
    d = model_dir()
    return (d / "model" / "model.bin").is_file() and (d / "sentencepiece.model").is_file()


def is_installed() -> bool:
    """Есть хотя бы одна модель машинного перевода."""
    return big_installed() or argos_installed()


def model_dirs() -> List[Path]:
    return [big_dir(), model_dir()]


def installed_size_mb() -> int:
    return sum(f.stat().st_size for d in model_dirs() if d.is_dir() for f in d.rglob("*") if f.is_file()) >> 20


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
    """Скачать основную модель (докачивается при обрыве): готовую сборку, а если её нет — официальный
    архив с конвертацией. Модель проверяется пробным переводом до того, как заменить прежнюю."""
    target = big_dir()
    tmp = target.with_name(target.name + ".tmp")
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        _download_big(tmp, status, cancel)
    except TranslatorError:
        raise
    except Exception as exc:  # noqa: BLE001
        if cancel is not None and cancel.is_set():
            raise
        log.warning("готовая сборка модели недоступна (%s) — официальный архив", exc)
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        _convert_official(tmp, status, cancel)
    status("Проверка модели…", None)
    probe = _BigModel(tmp, threads=2).run(["Press any key to continue."])
    if not probe or not re.search("[А-Яа-я]", probe[0]):
        shutil.rmtree(tmp, ignore_errors=True)
        raise TranslatorError("Скачанная модель перевода не работает — попробуйте ещё раз.")
    shutil.rmtree(target, ignore_errors=True)
    tmp.rename(target)
    status("Модель перевода установлена", 1.0)


def _download_big(dest: Path, status: StatusFn, cancel: Optional[threading.Event]) -> None:
    total_mb = APPROX_SIZE_MB

    for name in BIG_FILES:
        def progress(done: int, total: int, name=name) -> None:
            if name == "model.bin":
                status(f"Скачивание модели перевода: {done >> 20} из {(total >> 20) or total_mb} МБ",
                       done / total if total else None)
        status("Скачивание модели перевода…", 0.0 if name == "model.bin" else None)
        net.download(BIG_REPO + name, dest / name, progress=progress, cancel=cancel)


def _convert_official(dest: Path, status: StatusFn, cancel: Optional[threading.Event]) -> None:
    """Официальный архив модели (~900 МБ) → CTranslate2 int8 (~240 МБ)."""
    archive = paths.models_dir() / "opus-mt-tc-big-en-zle.zip"

    def progress(done: int, total: int) -> None:
        status(f"Скачивание модели перевода: {done >> 20} из {(total >> 20) or 850} МБ", done / total if total else None)
    net.download(BIG_OFFICIAL, archive, progress=progress, cancel=cancel)
    status("Подготовка модели перевода…", None)
    src = dest.with_name(dest.name + ".src")
    shutil.rmtree(src, ignore_errors=True)
    try:
        with zipfile.ZipFile(archive) as z:
            for n in z.namelist():
                if n.endswith((".npz", "vocab.yml", "decoder.yml", ".spm")):
                    z.extract(n, src)
        from ctranslate2.converters import OpusMTConverter
        OpusMTConverter(str(src)).convert(str(dest), quantization="int8", force=True)
        for spm in ("source.spm", "target.spm"):
            shutil.copyfile(src / spm, dest / spm)
    except zipfile.BadZipFile:
        archive.unlink(missing_ok=True)
        raise TranslatorError("Архив модели повреждён — скачайте заново.")
    finally:
        shutil.rmtree(src, ignore_errors=True)
    archive.unlink(missing_ok=True)


def install_argos(status: StatusFn = _noop, cancel: Optional[threading.Event] = None) -> None:
    """Скачать и распаковать запасную модель Argos (докачивается при обрыве)."""
    target = model_dir()
    archive = paths.models_dir() / "argos-en-ru.argosmodel"
    url = _resolve_url()

    def progress(done: int, total: int) -> None:
        mb, tot = done >> 20, (total >> 20) or 195
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


_bg_lock = threading.Lock()
_bg_started = False


def install_in_background() -> None:
    """Докачать основную модель в фоне (переводит пока запасная) — один раз за запуск."""
    global _bg_started
    with _bg_lock:
        if _bg_started or big_installed():
            return
        _bg_started = True

    def work():
        try:
            install()
            log.info("основная модель машинного перевода скачана — будет использоваться со следующего запуска")
        except Exception as exc:  # noqa: BLE001
            log.warning("основная модель машинного перевода не скачана: %s", exc)
    threading.Thread(target=work, name="machine-model-download", daemon=True).start()


class _BigModel:
    """OPUS-MT tc-big: своя модель sentencepiece для исходного текста и для перевода, метка языка
    ``>>rus<<`` перед предложением."""

    def __init__(self, d: Path, threads: int):
        import ctranslate2
        import sentencepiece as spm
        types = ctranslate2.get_supported_compute_types("cpu")
        compute = "int8" if "int8" in types else "float32"
        self.tr = ctranslate2.Translator(str(d), device="cpu", compute_type=compute, inter_threads=1,
                                         intra_threads=threads)
        self.src = spm.SentencePieceProcessor(model_file=str(d / "source.spm"))
        self.tgt = spm.SentencePieceProcessor(model_file=str(d / "target.spm"))

    def run(self, sentences: List[str]) -> List[str]:
        pieces = [[BIG_TARGET] + p + ["</s>"] for p in self.src.encode(sentences, out_type=str)]
        results = self.tr.translate_batch(pieces, beam_size=2, max_batch_size=32,
                                          max_decoding_length=min(512, max(len(p) for p in pieces) * 3 + 10))
        return [self.tgt.decode(r.hypotheses[0]) for r in results]


def remove() -> None:
    for d in model_dirs():
        shutil.rmtree(d, ignore_errors=True)


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
        self._big: Optional[_BigModel] = None
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
        elif not big_installed():
            install_in_background()             # пока переводит запасная модель
        if self._tr is None and self._big is None:
            status("Загрузка модели перевода…", None)
            self._load()
        status("Машинный переводчик готов", 1.0)

    def _load(self) -> None:
        if big_installed():
            try:
                self._big = _BigModel(big_dir(), self.threads)
                self.cache_id = "mt:opus-tc-big-en-ru-2022"
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("основная модель перевода не загрузилась (%s) — запасная", exc)
                if not argos_installed():
                    raise TranslatorError(f"Не удалось загрузить модель перевода: {exc}. "
                                          "Попробуйте удалить модель и скачать заново.")
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
        if self._big is not None:
            return self._big.run(sentences)
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
                for sent in self._sentences(masked, always=self._big is not None):
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
    def _sentences(line: str, always: bool = False) -> List[str]:
        if len(line) < 160 and not always:
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
        if self._tr is None and self._big is None:
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
        self._big = None


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
