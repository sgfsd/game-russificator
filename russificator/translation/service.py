"""Сервис перевода: всё, что общее для любых переводчиков.

  1. отсеять то, что переводить не нужно (пустое, одна разметка, уже русское);
  2. склеить дубликаты — каждая уникальная строка переводится один раз;
  3. взять готовое из памяти переводов (повторный запуск — бесплатный);
  4. собрать батчи в порядке игры (соседние реплики — контекст для нейросети);
  5. перевести (параллельно, если переводчик позволяет), проверить разметку,
     длину, применить глоссарий; сломанную разметку — перевести повторно;
  6. сообщать прогресс и сохранять проект по ходу — работу можно прервать;
  7. «спасти» то, что не перевелось: каждую строку ещё раз по одной, затем
     по кускам между разметкой (структура строки тогда гарантированно цела),
     затем запасным переводчиком (машинным, если он установлен). Английской
     строка остаётся, только если не справился никто.

Фатальная ошибка переводчика (неверный ключ, кончились деньги) сразу
останавливает работу с понятным сообщением.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from ..core.universal import Entry, EntryStatus, TextKind, TranslationProject
from . import filters, keys, markup, terms
from .base import Translator, TranslatorError
from .glossary import GlossaryBuilder, apply_glossary
from . import edges
from .length import over_limit, validate_length, was_trimmed
from .memory import TranslationMemory

log = logging.getLogger("russificator.service")

#: колбэк прогресса: (переведено_уникальных, всего_уникальных, сообщение)
ProgressFn = Callable[[int, int, str], None]


class TranslationCancelled(Exception):
    """Пользователь остановил перевод (прогресс сохранён)."""


@dataclass
class ServiceStats:
    unique_total: int = 0
    from_memory: int = 0
    translated: int = 0
    failed: int = 0
    skipped: int = 0
    shortened: int = 0
    failed_examples: List[str] = field(default_factory=list)
    seconds: float = 0.0


Key = Tuple[str, Optional[str]]


class TranslationService:
    SAVE_EVERY = 30.0  # секунд между сохранениями проекта

    def __init__(self, translator: Translator, memory: Optional[TranslationMemory] = None,
                 on_progress: Optional[ProgressFn] = None, cancel: Optional[threading.Event] = None,
                 fallback: Optional[Translator] = None):
        self.tr = translator
        self.memory = memory
        self.on_progress = on_progress or (lambda d, t, m: None)
        self.cancel = cancel or threading.Event()
        self.fallback = fallback          # запасной переводчик для строк, которые не дались основному
        self._failed: Dict[Key, List[Entry]] = {}

    def _key(self, e: Entry) -> Key:
        return (e.source, e.speaker if self.tr.context_aware else None)

    # ---------- основной проход ----------

    def run(self, project: TranslationProject) -> ServiceStats:
        started = time.monotonic()
        stats = ServiceStats()
        builder = GlossaryBuilder()
        builder.scan(project)
        auto = builder.result()
        auto.update(project.glossary)
        project.glossary = auto
        # термины игры — одним переводом на всю игру (см. terms.py); уже известные не переводятся заново
        if not self.cancel.is_set():
            project.glossary.update(terms.build(project.entries, self.tr,
                                                {**project.glossary, **project.forced_glossary}))
        glossary = {**project.glossary, **project.forced_glossary}

        groups: Dict[Key, List[Entry]] = {}
        for e in project.entries:
            if e.status not in (EntryStatus.NEW, EntryStatus.FAILED):
                continue
            if not markup.has_words(e.source) or markup.is_cyrillic(e.source) or filters.is_brand(e.source):
                e.status = EntryStatus.SKIPPED      # названия («Discord», «Made with Unity») не переводятся
                stats.skipped += 1
                continue
            groups.setdefault(self._key(e), []).append(e)

        # переводы, уже сделанные в проекте (другой говорящий/прошлый запуск) — тоже память
        pending: Dict[Key, List[Entry]] = {}
        forced = project.forced_glossary
        for key, group in groups.items():
            exact = forced.get(key[0].strip())
            if exact:
                self._assign(group, exact, True)
                stats.from_memory += 1
            else:
                pending[key] = group
        # клавиши и подсказки управления — раньше памяти: прошлые версии запоминали «SPACE» → «КОСМОС»
        self._translate_hints(pending, glossary, stats)
        if self.memory is not None and pending:
            by_ns: Dict[str, List[Key]] = {}
            for key in pending:
                by_ns.setdefault(self.tr.memory_ns(key[0]), []).append(key)
            hits = {}
            refit: Dict[Key, str] = {}
            for ns, keys in by_ns.items():
                hits.update(self.memory.get_many(ns, keys))
            stale = getattr(self.tr, "stale", None)          # переводчик сам знает свой прошлый брак
            hits = {k: v for k, v in hits.items()           # обрезанное прошлым жёстким лимитом — заново
                    if not filters.is_transliteration(k[0], v) and not was_trimmed(k[0], v)
                    and not (stale is not None and stale(k[0], v))}
            for key, tr in hits.items():
                if tr == key[0] and filters.is_name_word(key[0]):
                    continue                             # имя («Tom») прошлые версии оставляли латиницей
                if tr == key[0]:                         # запомнено «оставить как есть» (инициалы, буквы)
                    for e in pending.pop(key):
                        e.status = EntryStatus.SKIPPED
                    stats.skipped += 1
                    continue
                tr = markup.normalize_layout(key[0], tr)    # записи прошлых версий — тоже в порядок
                checked, why = edges.check(key[0], tr, pending[key][0].speaker)
                if why:                                    # мусор прошлых версий — переводится заново
                    continue
                tr = edges.fix(key[0], checked)
                if markup.same_markup(key[0], tr):
                    group = pending.pop(key)
                    ok = True
                    if over_limit(group[0], tr):           # запомнено до лимитов по месту оригинала
                        ok, shorter = validate_length(group[0], tr, self.tr)
                        if shorter != tr:
                            tr = shorter
                            refit[key] = tr
                    self._assign(group, tr, ok)
                    stats.from_memory += 1
            if refit:
                try:
                    by_ns_fit: Dict[str, Dict[Key, str]] = {}
                    for key, tr in refit.items():
                        by_ns_fit.setdefault(self.tr.memory_ns(key[0]), {})[key] = tr
                    for ns, part in by_ns_fit.items():
                        self.memory.put_many(ns, part)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Сокращённые переводы не сохранены в память: %s", exc)

        stats.unique_total = len(pending)
        if stats.from_memory:
            log.info("Из памяти переводов: %d строк", stats.from_memory)
        batches = self._batches(pending)
        self.on_progress(0, stats.unique_total,
                         f"К переводу {stats.unique_total} уникальных строк"
                         + (f" (ещё {stats.from_memory} взято из памяти)" if stats.from_memory else ""))
        if not batches:
            self._polish(project, glossary)
            stats.seconds = time.monotonic() - started
            return stats

        done = 0
        last_save = time.monotonic()
        fatal: Optional[TranslatorError] = None
        with ThreadPoolExecutor(max_workers=self.tr.parallel) as pool:
            queue = list(batches)
            running: Dict[Future, List[Key]] = {}
            while queue or running:
                while queue and len(running) < self.tr.parallel and not self.cancel.is_set() and fatal is None:
                    keys = queue.pop(0)
                    reps = [pending[k][0] for k in keys]
                    running[pool.submit(self.tr.translate, reps, glossary)] = keys
                if not running:
                    break
                finished, _ = wait(list(running), timeout=0.5, return_when=FIRST_COMPLETED)
                for fut in finished:
                    keys = running.pop(fut)
                    try:
                        result = fut.result()
                    except TranslatorError as exc:
                        if exc.fatal:
                            fatal = fatal or exc
                            queue.clear()
                            continue
                        log.warning("Батч не переведён: %s", exc)
                        result = {}
                    except Exception as exc:  # noqa: BLE001
                        log.exception("Батч упал")
                        result = {}
                    done += self._accept(keys, pending, result, glossary, stats)
                    elapsed = time.monotonic() - started
                    speed = done / elapsed if elapsed > 0 else 0
                    eta = (stats.unique_total - done) / speed if speed > 0 else 0
                    self.on_progress(done, stats.unique_total,
                                     f"Перевод: {done} из {stats.unique_total}" + (f" · осталось ~{_eta(eta)}"
                                                                                   if done > 5 else ""))
                    if time.monotonic() - last_save > self.SAVE_EVERY:
                        project.save()
                        last_save = time.monotonic()
                if self.cancel.is_set():
                    queue.clear()
        if fatal is None and not self.cancel.is_set() and self._failed:
            self._rescue(glossary, stats)
        for key, group in self._failed.items():
            for e in group:
                e.status = EntryStatus.FAILED
            stats.failed += 1
            if len(stats.failed_examples) < 15:
                stats.failed_examples.append(key[0][:80])
        self._failed = {}
        self._polish(project, glossary)
        project.save()
        stats.seconds = time.monotonic() - started
        if fatal is not None:
            raise fatal
        if self.cancel.is_set():
            raise TranslationCancelled()
        return stats

    def _translate_hints(self, pending: Dict[Key, List[Entry]], glossary: Dict[str, str],
                         stats: ServiceStats) -> None:
        """Клавиши и подсказки управления: «SPACE» → «ПРОБЕЛ», «ESC» — как есть, подсказка из колонок
        («TAB Cancel    SPACE Confirm») — по колонкам, клавиша в колонке остаётся (см. ``keys``)."""
        plans = {k: p for k in pending for p in [keys.plan(k[0])] if p}
        if not plans:
            return
        texts = sorted({t for p in plans.values() for t in keys.to_translate(p)})
        done: Dict[str, str] = {}
        todo = [Entry(id=f"hint{n}", source=t, kind=TextKind.UI) for n, t in enumerate(texts)]
        step = max(1, self.tr.max_batch_items)
        for i in range(0, len(todo), step):
            try:
                out = self.tr.translate(todo[i:i + step], glossary) or {}
            except Exception as exc:  # noqa: BLE001
                log.info("Подсказки управления не переведены по частям: %s", exc)
                return
            done.update({e.source: out[e.id] for e in todo[i:i + step] if out.get(e.id)})
        for key, pieces in plans.items():
            tr = keys.assemble(pieces, done)
            if tr is not None:
                group = pending.pop(key)
                self._accept([key], {key: group}, {group[0].id: tr}, glossary, stats)

    # ---------- спасение непереведённого ----------

    def _rescue(self, glossary: Dict[str, str], stats: ServiceStats) -> None:
        """Строки, которые не перевелись пачкой: по одной, по кускам, запасным переводчиком."""
        total = len(self._failed)
        log.info("Повторный перевод %d строк", total)
        steps = [(self.tr, False), (self.tr, True)]
        if self.fallback is not None:
            steps += [(self.fallback, False), (self.fallback, True)]
        prepared_fallback = False
        for translator, by_segments in steps:
            if not self._failed or self.cancel.is_set():
                break
            if translator is self.fallback and not prepared_fallback:
                try:
                    self.fallback.prepare()
                    prepared_fallback = True
                except Exception as exc:  # noqa: BLE001
                    log.warning("Запасной переводчик недоступен: %s", exc)
                    break
            for key in list(self._failed):
                if self.cancel.is_set():
                    break
                rep = self._failed[key][0]
                self.on_progress(total - len(self._failed), total,
                                 f"Повторный перевод трудных строк: {total - len(self._failed)} из {total}")
                try:
                    if by_segments:
                        tr = translate_by_segments(translator, rep, glossary)
                    else:
                        tr = translator.translate([rep], glossary).get(rep.id)
                except TranslatorError as exc:
                    if exc.fatal and translator is self.tr:
                        break  # основной переводчик больше не работает — дальше только запасной
                    continue
                except Exception:  # noqa: BLE001
                    log.debug("повтор не удался", exc_info=True)
                    continue
                if tr:
                    group = self._failed.pop(key)
                    self._accept([key], {key: group}, {rep.id: tr}, glossary, stats)
        if prepared_fallback:
            try:
                self.fallback.close()
            except Exception:  # noqa: BLE001
                pass

    def _batches(self, pending: Dict[Key, List[Entry]]) -> List[List[Key]]:
        batches: List[List[Key]] = []
        cur: List[Key] = []
        chars = 0
        for key in pending:  # dict сохраняет порядок игры
            size = len(key[0]) + 20
            if cur and (len(cur) >= self.tr.max_batch_items or chars + size > self.tr.max_batch_chars):
                batches.append(cur)
                cur, chars = [], 0
            cur.append(key)
            chars += size
        if cur:
            batches.append(cur)
        return batches

    # ---------- приём результата ----------

    def _accept(self, keys: List[Key], pending: Dict[Key, List[Entry]], result: Dict[str, str],
                glossary: Dict[str, str], stats: ServiceStats) -> int:
        forced = {k: v for k, v in glossary.items() if v}
        to_memory: Dict[Key, str] = {}
        for key in keys:
            group = pending[key]
            rep = group[0]
            tr = result.get(rep.id)
            if tr:
                tr = markup.normalize_layout(rep.source, tr)
            if tr is not None and tr.strip() and not markup.same_markup(rep.source, tr):
                lost = markup.missing_markup(rep.source, tr)
                retry = getattr(self.tr, "retry_markup", None)
                fixed = retry(rep, glossary, lost) if retry else None
                fixed = markup.normalize_layout(rep.source, fixed) if fixed else None
                tr = fixed if fixed and markup.same_markup(rep.source, fixed) else None
                if tr is None:
                    log.info("Разметка потеряна в «%s»: %s", rep.source[:60], lost)
            if tr and tr.strip():
                tr, why = edges.check(rep.source, tr, rep.speaker)
                if why:
                    log.info("Перевод «%s» отброшен: %s", rep.source[:60], why)
            if not tr or not tr.strip():
                self._failed[key] = group   # решается в конце прохода: повтор, куски, запасной переводчик
                continue
            if filters.is_transliteration(rep.source, tr):
                for e in group:          # инициалы, буквы: «перевод» — те же буквы кириллицей
                    e.status = EntryStatus.SKIPPED
                stats.skipped += 1
                to_memory[key] = rep.source              # в следующий раз — сразу «как есть»
                continue
            used = {k: v for k, v in forced.items() if k.lower() in rep.source.lower()}
            tr = edges.fix(rep.source, apply_glossary(tr, used) if used else tr)
            ok, final = validate_length(rep, tr, self.tr)
            if not ok:
                stats.shortened += 1
            self._assign(group, final, ok)
            to_memory[key] = final
            stats.translated += 1
        if self.memory is not None and to_memory:
            try:
                by_ns: Dict[str, Dict[Key, str]] = {}
                for key, tr in to_memory.items():
                    by_ns.setdefault(self.tr.memory_ns(key[0]), {})[key] = tr
                for ns, part in by_ns.items():
                    self.memory.put_many(ns, part)
            except Exception as exc:  # noqa: BLE001
                log.warning("Память переводов недоступна: %s", exc)
        return len(keys)

    @staticmethod
    def _polish(project: TranslationProject, glossary: Dict[str, str]) -> None:
        """Термины и жанровые слова — и в переводах из памяти и прошлых запусков (см. ``terms.polish``)."""
        try:
            n = terms.polish(project, glossary)
            if n:
                log.info("Термины игры поправлены в %d переводах", n)
        except Exception as exc:  # noqa: BLE001 — доводка не должна ронять перевод
            log.warning("Термины игры не применены: %s", exc)

    @staticmethod
    def _assign(group: List[Entry], translation: str, approved: bool) -> None:
        for e in group:
            e.translation = translation
            e.status = EntryStatus.APPROVED if approved else EntryStatus.TRANSLATED


def translate_by_segments(translator: Translator, entry: Entry, glossary: Dict[str, str]) -> Optional[str]:
    """Перевести только текст между вставками разметки и собрать строку обратно.

    Структура строки (теги, коды, плейсхолдеры, переносы) при этом не может
    сломаться — переводчик их просто не видит. Качество ниже, чем у перевода
    целиком, поэтому это последний шаг перед отказом.
    """
    lines = entry.source.split("\n")
    pieces: List[Tuple[int, int, str]] = []   # (строка, кусок, текст)
    plan: List[List[Tuple[bool, str]]] = []
    for li, line in enumerate(lines):
        chunks = markup.split_by_markup(line)
        plan.append(chunks)
        for ci, (is_tag, text) in enumerate(chunks):
            if not is_tag and markup.has_words(text):
                pieces.append((li, ci, text))
    if not pieces:
        return None
    todo = [Entry(id=f"{entry.id}#s{n}", source=text.strip(), kind=entry.kind, speaker=entry.speaker)
            for n, (_, _, text) in enumerate(pieces)]
    done: Dict[str, str] = {}
    step = max(1, translator.max_batch_items)
    for i in range(0, len(todo), step):
        done.update(translator.translate(todo[i:i + step], glossary) or {})
    for n, (li, ci, text) in enumerate(pieces):
        tr = done.get(f"{entry.id}#s{n}")
        if not tr or not tr.strip() or markup.tokens(tr):
            return None      # кусок не перевёлся или переводчик добавил разметку — не годится
        lead = text[: len(text) - len(text.lstrip())]
        trail = text[len(text.rstrip()):]
        plan[li][ci] = (False, lead + tr.strip() + trail)
    return "\n".join("".join(t for _, t in chunks) for chunks in plan)


def _eta(seconds: float) -> str:
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600} ч {s % 3600 // 60} мин"
    if s >= 60:
        return f"{s // 60} мин"
    return f"{s} с"
