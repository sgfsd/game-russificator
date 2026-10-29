"""Отчёт о переводе игры («стенд»): цифры и примеры для разбора огрехов после русификации.

Нажал «Скопировать отчёт о переводе» в «Мои игры» (или ``russificator report <папка игры>``) — получил
markdown, который можно вставить в чат или issue: сколько строк найдено и откуда, что не перевелось,
что игра показала, а найдено заранее не было (досталось живому переводу), подозрительные переводы
(остались английские слова, пословный перевод Заглавного Регистра, мешанина латиницы с кириллицей),
шрифты, запись в данные игры, термины игры, предупреждения. Только чтение проекта — игра не запускается.
Личное (домашняя папка, имя компьютера, ключи) вычищается, как в отчёте об ошибке.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

EXAMPLES = 12
#: латиница — кириллица — латиница в одном переводе: «Proficiency in Яд Arts»
_MIXED_RE = re.compile(r"[A-Za-z]{3,}[\s\-:,]+[А-Яа-яЁё]+[\s\-:,]+[A-Za-z]{3,}")


def _source_of(scene: str) -> str:
    """Откуда строка, без подробностей: «LubanTables.dat:lang_tbtextmapper» → «LubanTables.dat»."""
    s = (scene or "").strip() or "—"
    return s.split(":")[0] if not re.match(r"^[A-Za-z]:[\\/]", s) else Path(s).name


def leftover_words(source: str, translation: str) -> List[str]:
    """Слова оригинала (от 3 букв), оставшиеся в переводе по-английски (без разметки, имён из кода и
    слов, записанных в оригинале капсом, — это обычно названия и клавиши)."""
    from .translation.markup import plain_text
    src = {w.lower() for w in re.findall(r"[A-Za-z]{3,}", plain_text(source)) if not w.isupper()}
    return [w for w in re.findall(r"[A-Za-z]{3,}", plain_text(translation)) if w.lower() in src and not w.isupper()]


def quality(entries) -> Dict[str, List[Tuple[str, str]]]:
    """Подозрительные переводы по видам: (оригинал, перевод)."""
    from .translation import machine
    out: Dict[str, List[Tuple[str, str]]] = {"english": [], "title": [], "mixed": []}
    for e in entries:
        tr = e.translation or ""
        if not tr or tr == e.source:
            continue
        if leftover_words(e.source, tr):
            out["english"].append((e.source, tr))
        if machine._title_label(e.source) and machine._inner_capitals(tr) and _phrase(e.source):
            out["title"].append((e.source, tr))
        if _MIXED_RE.search(tr) and not _MIXED_RE.search(e.source):
            out["mixed"].append((e.source, tr))
    return out


def _phrase(source: str) -> bool:
    """Подпись из обычных английских слов («Main Dish»), а не имя или название («Su Lianyan»): у имён
    заглавные в переводе — по праву."""
    from .translation.markup import _lexicon
    lex = _lexicon()
    words = [w.lower() for w in re.findall(r"[A-Za-z]{3,}", source)]
    return bool(words) and bool(lex) and all(w in lex or w.rstrip("s") in lex for w in words)


def live_missed(game: Path, engine: str, work: Path) -> List[str]:
    """Строки, которые игра показала, а в словаре их не было (их перевёл живой перевод)."""
    try:
        if engine == "unity":
            from .engines.unity import coverage
            return sorted(coverage.missed(game, work))
        if engine == "renpy":
            from .engines.renpy import detect as rdetect
            from .engines.renpy.plugin import LIVE_FILE, RenpyPlugin
            root = rdetect.find_game_dir(game)
            found = set()
            if root is not None:
                for p in RenpyPlugin._missed_files(root):
                    found.update(RenpyPlugin._read_missed(p))
            try:
                data = json.loads((work / LIVE_FILE).read_text(encoding="utf-8"))
                found.update(s for s in (data if isinstance(data, list) else data.get("strings", []))
                             if isinstance(s, str))
            except (OSError, ValueError, AttributeError):
                pass
            return sorted(found)
    except Exception:  # noqa: BLE001 — отчёт собирается и без этого раздела
        pass
    return []


def _clip(s: str, n: int = 140) -> str:
    s = s.replace("\r", "").replace("\n", "↵")
    return s if len(s) <= n else s[:n - 1] + "…"


def build(game_dir: Path) -> Optional[str]:
    """Отчёт о переводе игры (markdown) или None, если игру ещё не русифицировали."""
    from . import __version__, report
    from .core.pipeline import project_dir
    from .core.universal import EntryStatus, TranslationProject
    from .translation import terms

    game = Path(game_dir)
    work = project_dir(game)
    try:
        project = TranslationProject.load(work)
    except Exception:  # noqa: BLE001
        return None
    meta = project.meta or {}
    entries = project.entries
    by_status = Counter(e.status for e in entries)
    done = [e for e in entries if e.translation and e.status in (EntryStatus.TRANSLATED, EntryStatus.APPROVED)]
    title = meta.get("game_title") or game.name

    lines = [f"### Отчёт о переводе: {title}", ""]
    rows = [
        ("Программа", f"{__version__} ({report.program_build()})"),
        ("Движок", meta.get("detection_details") or project.engine),
        ("Папка", str(game)),
        ("Переводчик", f"{meta.get('translator', '—')}, правила v{meta.get('rules', 0)}"),
        ("Строк", f"всего {len(entries)}, переведено {len(done)}, не переведено "
                  f"{by_status.get(EntryStatus.FAILED, 0)}, пропущено {by_status.get(EntryStatus.SKIPPED, 0)}, "
                  f"ждут перевода {by_status.get(EntryStatus.NEW, 0)}"),
    ]
    lines += [f"- **{k}:** {v}" for k, v in rows]

    sources = Counter(_source_of(e.scene) for e in entries)
    lines += ["", "#### Откуда строки", ""]
    lines += [f"- {src}: {n}" for src, n in sources.most_common(15)]
    if len(sources) > 15:
        lines.append(f"- …и ещё {len(sources) - 15} источников: {sum(n for _, n in sources.most_common()[15:])}")

    if project.engine == "unity":
        fonts = meta.get("unity_fonts") or []
        direct = meta.get("unity_direct") or {}
        lines += ["", "#### Запись в игру", "",
                  f"- Словарь XUnity: {meta.get('unity_static_translations', '—')} строк, "
                  f"правил-шаблонов {meta.get('unity_template_rules', '—')}",
                  f"- Прямо в данные игры: {direct.get('strings', 0)} строк в {direct.get('files', 0)} файлах"
                  + (f", пропущено неоднозначных {direct['ambiguous']}" if direct.get("ambiguous") else ""),
                  f"- Подгонка размера: {meta.get('unity_resized', 0)} элементов, IL2CPP: "
                  f"{'да' if meta.get('unity_backend') == 'il2cpp' else 'нет'}"]
        if fonts:
            lines.append("- Шрифты: " + "; ".join(
                f"{f.get('name')} ({f.get('file')})" + (f" +{f['added']}" if f.get("added") else "")
                + (f" ОШИБКА: {f['error']}" if f.get("error") else "") for f in fonts[:20]))

    used_terms = {k: v for k, v in (project.glossary or {}).items() if v}
    genres = terms.genres([e.source for e in entries])
    lines += ["", "#### Термины игры", "",
              f"- Жанр: {', '.join(genres) or '—'}; терминов с переводом: {len(used_terms)}"]
    if used_terms:
        lines.append("- Примеры: " + "; ".join(f"{k} → {v}" for k, v in list(used_terms.items())[:EXAMPLES]))

    # найденное во время игры, чего до сих пор нет в словаре (прошлые находки уже переведены)
    known = {e.source for e in done}
    missed = [s for s in live_missed(game, project.engine, work) if s not in known]
    lines += ["", f"#### Досталось живому переводу (нет в словаре): {len(missed)}", ""]
    lines += [f"- `{_clip(s)}`" for s in missed[:EXAMPLES]] or ["- ничего (или в игру ещё не играли)"]

    failed = [e for e in entries if e.status == EntryStatus.FAILED]
    if failed:
        lines += ["", f"#### Не переведено: {len(failed)}", ""]
        lines += [f"- `{_clip(e.source)}`" for e in failed[:EXAMPLES]]

    q = quality(done)
    names = {"english": "Остались английские слова", "title": "Пословный перевод Заглавного Регистра",
             "mixed": "Мешанина латиницы и кириллицы"}
    lines += ["", "#### Подозрительные переводы", ""]
    for kind, title_ in names.items():
        items = q[kind]
        lines.append(f"- **{title_}:** {len(items)} ({len(items) * 100 // max(1, len(done))}% переводов)")
        for s, t in items[:EXAMPLES // 2]:
            lines.append(f"  - `{_clip(s, 90)}` → `{_clip(t, 90)}`")

    notes = list(project.warnings or []) + [f"ОШИБКА: {x}" for x in (project.errors or [])]
    if notes:
        lines += ["", "#### Предупреждения", ""]
        lines += [f"- {_clip(n, 400)}" for n in notes[:15]]
    return report.scrub("\n".join(lines).rstrip() + "\n")
