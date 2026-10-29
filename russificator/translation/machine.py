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
import unicodedata
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .. import net, paths
from ..core.universal import Entry
from . import markup
from .markup import join_soft_wraps, rewrap
from .base import StatusFn, Translator, TranslatorError, _noop
from .filters import spelled_in_cyrillic
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
        # файл скачан целиком в прошлый раз, а проверка модели тогда не прошла (например, из-за русских
        # букв в пути) — второй раз не качаем; битый файл отсеет пробный перевод
        if (dest / name).is_file():
            continue

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


def load_spm(path: Path):
    """Модель sentencepiece из файла. Читаем байты сами: sentencepiece на Windows открывает файл
    по пути в кодировке ANSI и не находит его, если в пути есть буквы не из неё (папка
    пользователя «Игорь» при английской Windows, китайские буквы при русской…)."""
    import sentencepiece as spm
    return spm.SentencePieceProcessor(model_proto=Path(path).read_bytes())


class _BigModel:
    """OPUS-MT tc-big: своя модель sentencepiece для исходного текста и для перевода, метка языка
    ``>>rus<<`` перед предложением."""

    def __init__(self, d: Path, threads: int):
        import ctranslate2
        types = ctranslate2.get_supported_compute_types("cpu")
        compute = "int8" if "int8" in types else "float32"
        self.tr = ctranslate2.Translator(str(d), device="cpu", compute_type=compute, inter_threads=1,
                                         intra_threads=threads)
        self.src = load_spm(d / "source.spm")
        self.tgt = load_spm(d / "target.spm")

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
        self._sp = load_spm(d / "sentencepiece.model")

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

    def memory_ns(self, source: str) -> str:
        if join_soft_wraps(source)[1] is not None:
            return self.cache_id + ":wrap"
        if _framed_phrase(source):
            return self.cache_id + ":ui"
        if _decap(source) != source:
            return self.cache_id + ":dc"
        return self.cache_id

    def rules_version(self) -> int:
        # 1 — фраза, разбитая на строки, переводится целиком; 2 — слово словаря интерфейса в рамке
        # из знаков («-NEW-») берётся из словаря; 3 — оставшиеся английскими слова Заглавного
        # Регистра и КАПСА переводятся повторно в обычном регистре; 4 — оставшиеся английскими слова
        # обычного регистра — повтор с другим концом фразы; 5 — подписи по словарю интерфейса (и в
        # несколько строк, и с «!»/«:»), пословный перевод Заглавного Регистра, полноширинные знаки и
        # слова вида «WorkBench»
        return 5

    def redo(self, source: str, translation: str, old_version: int) -> bool:
        return ((old_version < 1 and join_soft_wraps(source)[1] is not None)
                or (old_version < 2 and _framed_phrase(source))
                or (old_version < 3 and _decap(source) != source and _leftover(source, translation or "") > 0)
                or (old_version < 4 and self.stale(source, translation or ""))
                or (old_version < 5 and _redo_v5(source, translation or "")))

    def stale(self, source: str, translation: str) -> bool:
        """Запомненный перевод с непереведённым обычным словом оригинала («Кто cares») или сделанный до
        правил версии 5 («REPLAY» → «ВОСПРОИЗВЕСТИ», «Главная Блюда»): переводится заново."""
        return _leftover_common(source, translation) > 0 or _redo_v5(source, translation)

    def translate_texts(self, texts: List[str]) -> List[str]:
        """Перевести произвольные строки (с защитой разметки). Для тестов и сервера перевода.

        Строки модель переводит по одной; фраза, разбитая игрой на строки ради вёрстки («insert↵coin»),
        переводится целиком и раскладывается обратно на столько же строк."""
        layouts = []
        joined = []
        for text in texts:
            j, layout = join_soft_wraps(text)
            joined.append(j)
            layouts.append(layout)
        done = self._translate_lines(joined)
        return [rewrap(d, layout) if layout else d for d, layout in zip(done, layouts)]

    def _translate_lines(self, texts: List[str]) -> List[str]:
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
                masked, originals, sign = markup.mask(line, self._signs())
                masked = self._mask_unknown(_for_model(masked), originals, sign)
                sentences = []
                # маркер, приклеенный к слову («&0Warning:»), мешает модели перевести само слово:
                # ей он даётся через пробел, а после перевода приклеивается обратно (unmask)
                for sent in self._sentences(_spaced(masked, sign), always=self._big is not None):
                    lead, core, trail = _strip(sent)
                    caps = _is_caps(core)
                    src = core.lower().capitalize() if caps else core
                    if markup.has_words(core.replace(sign, " ")):
                        enqueue(src)
                    sentences.append((lead, src, trail, caps))
                lines.append((masked, originals, sign, sentences))
            plans.append((i, lines))

        done = self._run(queue)
        # Заглавный Регистр и КАПС модель принимает за имена и оставляет английскими («Sallow Cavern» →
        # «Sallow Пещера», «LEVEL 7»), а то и дописывает мусор («Quarry of the Damned скачать»):
        # такие строки переводятся ещё раз в обычном регистре — берётся вариант, где английского меньше
        retry = [k for k, src in enumerate(queue) if _leftover(src, done[k]) and _decap(src) != src]
        if retry:
            again = self._run([_decap(queue[k]) for k in retry])
            for k, alt in zip(retry, again):
                if _leftover(queue[k], alt) < _leftover(queue[k], done[k]):
                    alt = _recap_marked(queue[k], alt)
                    done[k] = alt[:1].upper() + alt[1:] if queue[k][:1].isupper() else alt
        # слово обычного регистра модель тоже бывает оставляет английским («Who cares lemme go» → «Кто
        # cares…»): та же фраза с точкой в конце (или без конечного знака) переводится иначе — берётся
        # вариант, где английского меньше, конечный знак — как в оригинале
        retry = [k for k, src in enumerate(queue) if _leftover(src, done[k])]
        if retry:
            variants = [_repunct(queue[k]) for k in retry]
            again = self._run(variants)
            for k, alt in zip(retry, again):
                if _leftover(queue[k], alt) < _leftover(queue[k], done[k]):
                    done[k] = _unrepunct(queue[k], alt)
        # незнакомое модели слово со строчной она пишет кириллицей («dang» → «дэнг», «nope» → «ноуп»): с
        # заглавной и точкой («Dang.») это уже реплика, и модель её переводит («Черт.»)
        retry = [k for k, src in enumerate(queue)
                 if src[:1].islower() and len(src.split()) <= 3 and spelled_in_cyrillic(src, done[k])]
        if retry:
            again = self._run([_repunct(queue[k][:1].upper() + queue[k][1:]) for k in retry])
            for k, alt in zip(retry, again):
                if not spelled_in_cyrillic(queue[k], alt) and re.search("[а-яё]", alt.lower()):
                    alt = _unrepunct(queue[k], alt)
                    done[k] = alt[:1].lower() + alt[1:]
        # подпись Заглавным Регистром модель переводит пословно, перенося заглавные: «Main Dish» →
        # «Главная Блюда», «Fire Cloud Sparrow» → «Огненное Облако Воробей» (по-русски слова внутри
        # строчные) — та же подпись обычным регистром переводится связно («Главное блюдо»)
        retry = [k for k, src in enumerate(queue) if _title_label(src) and _inner_capitals(done[k])]
        if retry:
            again = self._run([_decap(queue[k]) for k in retry])
            for k, alt in zip(retry, again):
                if _inner_capitals(alt) < _inner_capitals(done[k]) and not _leftover(queue[k], alt):
                    done[k] = alt[:1].upper() + alt[1:]

        for i, lines in plans:
            result_lines = []
            ok_all = True
            for masked, originals, sign, sentences in lines:
                parts = []
                for lead, src, trail, caps in sentences:
                    if src in index:
                        tr = _no_additions(src, done[index[src]])
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
        return [_no_unknown(o) if o is not None else t for o, t in zip(out, texts)]

    def _signs(self) -> str:
        """Знаки маркеров разметки, которые модель знает и переносит в перевод как есть: большая
        модель не знает «@» и «#» (вместо них «⁇» — маркер терялся, и строка переводилась по
        кускам), а «&N» переносит без искажений; запасная (Argos) лучше всего держит «@N»."""
        sp = self._big.src if self._big is not None else self._sp
        if sp is None:
            return "@#"
        return "".join(c for c in "@#&*$" if not _unknown(sp, c)) or "@#"

    def _mask_unknown(self, masked: str, originals: List[str], sign: str) -> str:
        """Значки (``=``, ``~``, ``→``, ``|``, ``♥``…) модель превращает в «⁇» (их нет в её словаре)
        или теряет — они прячутся маркерами, как разметка, и возвращаются на место."""
        sp = self._big.src if self._big is not None else self._sp
        out = []
        for ch in masked:
            if _keep(sp, ch):
                originals.append(ch)
                out.append(f"{sign}{len(originals) - 1}")
            else:
                out.append(ch)
        return "".join(out)

    def _translate_by_segments(self, text: str) -> str:
        """Запасной путь: переводим куски между вставками по отдельности."""
        lines = []
        for line in text.split("\n"):
            chunks = [part for is_tag, c in markup.split_by_markup(line)
                      for part in ([(True, c)] if is_tag else self._split_unknown(c))]
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

    def _split_unknown(self, text: str) -> List[Tuple[bool, str]]:
        """Кусок текста → [(это_вставка, текст)]: символы вне словаря модели — как вставки."""
        sp = self._big.src if self._big is not None else self._sp
        out: List[Tuple[bool, str]] = []
        for ch in text:
            tag = _keep(sp, ch)
            if out and not tag and not out[-1][0]:
                out[-1] = (False, out[-1][1] + ch)
            else:
                out.append((tag, ch))
        return out

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


_unknown_cache: Dict[Tuple[int, str], bool] = {}


def _keep(sp, ch: str) -> bool:
    """Символ не для перевода — переносится как есть: символ вне словаря модели или значок не из
    ASCII (категория Unicode «S»: → ★ ♥ ≥ — модели их теряют)."""
    if ch.isspace():
        return False
    return (ord(ch) > 127 and unicodedata.category(ch)[0] == "S") or (sp is not None and _unknown(sp, ch))


def _unknown(sp, ch: str) -> bool:
    key = (id(sp), ch)
    if key not in _unknown_cache:
        _unknown_cache[key] = sp.unk_id() in sp.encode(ch, out_type=int)
    return _unknown_cache[key]


def _spaced(masked: str, sign: str) -> str:
    """Маркеры вставок отделены пробелами от соседних слов (для модели)."""
    s = re.sub(rf"(?<=\S)({re.escape(sign)}\d+)", r" \1", masked)
    return re.sub(rf"({re.escape(sign)}\d+)(?=[^\s\d])", r"\1 ", s)


#: полноширинная пунктуация китайской раскладки: «WorkBench：Cultivate»
_FULLWIDTH_PUNCT_RE = re.compile(r"\s*([！，：；？])\s*")
_COMPOUND_RE = re.compile(r"(?<![\w@#&*$])[A-Z]?[a-z]+[A-Z][a-z]+(?![\w])")


def _for_model(line: str) -> str:
    """Строка в том виде, в каком модель её понимает: полноширинные знаки — обычными с пробелом после
    («WorkBench：Cultivate» → «WorkBench: Cultivate»), обычное слово с заглавной в середине — как в
    словаре («WorkBench» → «Workbench»: иначе модель оставляет его английским)."""
    if _FULLWIDTH_PUNCT_RE.search(line):
        out = _FULLWIDTH_PUNCT_RE.sub(lambda m: unicodedata.normalize("NFKC", m.group(1)) + " ", line)
        line = out[:-1] if out.endswith(" ") and not line.endswith(" ") else out
    return _COMPOUND_RE.sub(lambda m: m.group(0)[0] + m.group(0)[1:].lower()
                            if markup.plain_compound(m.group(0)) else m.group(0), line)


def _redo_v5(source: str, translation: str) -> bool:
    """Перевод, который правила версии 5 сделали бы иначе."""
    ready = ui_phrase(source)
    if ready is not None:
        return ready != translation
    if _title_label(source) and _inner_capitals(translation):
        return True
    return _for_model(source) != source and _leftover(source, translation) > 0


def _title_label(src: str) -> bool:
    """Подпись Заглавным Регистром из 2–5 слов без знака конца фразы («Main Dish», «Auto Stack Radius»)."""
    words = re.findall(r"[A-Za-z][A-Za-z'-]*", src)
    return (2 <= len(words) <= 5 and len(src.split()) <= 6 and "\n" not in src and not re.search(r"[.!?…]\s*$", src)
            and all(w[0].isupper() and not w.isupper() for w in words))


def _inner_capitals(tr: str) -> int:
    """Сколько слов перевода не в начале написано с заглавной (кириллица; аббревиатуры не в счёт)."""
    words = re.findall(r"[А-ЯЁа-яё][а-яё]+", tr)
    return sum(1 for w in words[1:] if w[0].isupper())


def _leftover_common(src: str, tr: str) -> int:
    """Сколько слов оригинала, написанных строчными (не имён и не аббревиатур), осталось в переводе."""
    words = {w for w in re.findall(r"(?<![A-Za-z'])[a-z]{3,}(?![A-Za-z'])", src)}
    return sum(1 for w in re.findall(r"[A-Za-z]{3,}", tr) if w in words)


def _leftover(src: str, tr: str) -> int:
    """Сколько слов оригинала (от 3 букв) осталось в переводе по-английски."""
    words = {w.lower() for w in re.findall(r"[A-Za-z]{3,}", src)}
    return sum(1 for w in re.findall(r"[A-Za-z]{3,}", tr) if w.lower() in words)


_END_RE = re.compile(r"[.!?…]+$")


def _repunct(s: str) -> str:
    """Та же фраза с другим концом: без конечного знака — с точкой, со знаком — без него."""
    m = _END_RE.search(s)
    return s[:m.start()] if m and m.start() > 0 else s + "."


def _unrepunct(src: str, tr: str) -> str:
    """Конец перевода варианта из :func:`_repunct` — как у оригинала."""
    m = _END_RE.search(src)
    body = _END_RE.sub("", tr.rstrip())
    return body + (m.group(0) if m else "")


def _decap(s: str) -> str:
    """Та же строка в обычном регистре: слова капсом (от 3 букв) — строчными, слова с заглавной
    — строчными, кроме первого слова и первого после конца фразы."""
    out = []
    start = True
    for tok in re.split(r"(\s+)", s):
        m = re.match(r"^([^A-Za-z]*)([A-Za-z][A-Za-z']*)(.*)$", tok)
        if m:
            lead, word, rest = m.groups()
            if word.isupper() and len(word) >= 3:
                word = word.capitalize() if start else word.lower()
            elif word[0].isupper() and word[1:].islower() and not start:
                word = word.lower()
            tok = lead + word + rest
            start = bool(re.search(r"[.!?]$", tok))
        out.append(tok)
    return "".join(out)


def _recap_marked(src: str, tr: str) -> str:
    """Слово капсом между маркерами разметки («<color>ESCAPE</color>» — выделение) и в переводе
    остаётся капсом: перевод между теми же маркерами пишется заглавными."""
    for m in re.finditer(r"([@#&*$]\d+)\s*([A-Z][A-Z' ]*[A-Z])\s*([@#&*$]\d+)", src):
        a, b = re.escape(m.group(1)), re.escape(m.group(3))
        tr = re.sub(a + r"(?!\d)(.*?)" + b + r"(?!\d)", lambda x: x.group(0).upper(), tr, count=1)
    return tr


def _framed_phrase(source: str) -> bool:
    """Слово словаря интерфейса в рамке из знаков («-NEW-», «[ OK ]»), а не само по себе."""
    from .glossary import ui_phrases
    return ui_phrase(source) is not None and source.strip().lower() not in ui_phrases()


def _no_additions(src: str, tr: str) -> str:
    """Скобки, которых в оригинале нет, — приписки модели к непереводимому («62CO (англ.)»)."""
    if "(" not in src and "(" in tr:
        tr = re.sub(r"\s*\([^()]*\)", "", tr).strip() or tr
    return tr


def _no_unknown(text: str) -> str:
    """Последняя защита: «⁇» (символ вне словаря модели) в перевод не попадает."""
    if "⁇" not in text:
        return text
    return re.sub(r"[ \t]{2,}", " ", text.replace("⁇", " ")).strip()


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
