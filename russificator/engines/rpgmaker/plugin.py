"""Плагин движка RPG Maker — MV/MZ (JSON), XP/VX/VX Ace (Ruby Marshal, в т.ч. из архивов).

RPG Maker 2000/2003 распознаётся, но перевод для него не реализован —
пользователь получает честное сообщение.

Шрифты (заранее, а не по «квадратикам» в игре): шрифт игры проверяется на
кириллицу, и если её нет —
  * MV: к семейству GameFont добавляется PT Sans только для кириллицы
    (``unicode-range``) — латиница остаётся родной;
  * MZ: основной шрифт в System.json меняется на PT Sans (движок дожидается
    его загрузки, так что буквы гарантированно появятся);
  * VX/VX Ace: PT Sans кладётся в Fonts/, а маленький скрипт перед Main
    делает его шрифтом по умолчанию.
"""

from __future__ import annotations

import json
import re
import zlib
from pathlib import Path
from typing import List, Optional

from ...core.plugin_api import Detection, EnginePlugin
from ...core.universal import ExtractionResult, TranslationProject
from ...fonts.cmap import codepoints
from . import detect as _detect
from . import mv_mz, old_marshal
from . import ruby_marshal as M

VERSION_TITLES = _detect.RPGMAKER_VERSIONS
CYRILLIC_RANGE = "U+0400-045F, U+0490-0491, U+2116, U+00AB, U+00BB, U+2013-2014, U+2026, U+201C-201E"
FONT_FAMILY = "PT Sans"


def _has_cyrillic_bytes(data: Optional[bytes]) -> Optional[bool]:
    """True/False — есть ли кириллица; None — формат шрифта не распознан."""
    if not data:
        return None
    cps = codepoints(data)
    if not cps:
        return None
    return all(ord(c) in cps for c in "АБВЖЯабвжя")


class RPGMakerPlugin(EnginePlugin):
    engine_id = "rpgmaker"
    title = "RPG Maker"
    version: Optional[str] = None

    def detect(self, game_dir: Path) -> Detection:
        version, conf, details, notes = _detect.detect(Path(game_dir))
        self.version = version
        return Detection(confidence=conf, engine_name=VERSION_TITLES.get(version or "", self.title),
                         details=details, notes=notes)

    def extract(self, game_dir: Path, project: TranslationProject) -> ExtractionResult:
        game_dir = Path(game_dir)
        if self.version is None and self.detect(game_dir).confidence <= 0:
            return ExtractionResult([], warnings=["Движок RPG Maker не распознан."])
        if self.version in ("mv", "mz"):
            entries, warnings, _ = mv_mz.extract(game_dir)
        elif self.version in ("xp", "vx", "ace"):
            entries, warnings = old_marshal.extract(game_dir, self.version)
        elif self.version in ("2k", "2k3"):
            return ExtractionResult([], warnings=[
                "RPG Maker 2000/2003 распознан, но перевод этой версии пока не поддерживается."])
        else:
            return ExtractionResult([], warnings=["Версия RPG Maker не определена."])
        for e in entries:
            project.add_entry(e)
        project.meta["rpgmaker_version"] = self.version
        return ExtractionResult(entries, meta={"version": self.version}, warnings=warnings)

    def inject(self, game_dir: Path, project: TranslationProject) -> None:
        game_dir = Path(game_dir)
        if self.version in ("mv", "mz"):
            count, warnings = mv_mz.inject(game_dir, project, self.version)
            if not mv_mz.install_runtime(game_dir, project.backup):
                warnings.append("Плагин переноса слов не подключён (нет js/plugins.js) — длинные строки "
                                "перенесены заранее, по ширине окна.")
        elif self.version in ("xp", "vx", "ace"):
            count, warnings = old_marshal.inject(game_dir, project, self.version)
        else:
            raise RuntimeError("Внедрение для этой версии RPG Maker не поддерживается.")
        project.warnings.extend(warnings)
        project.meta["injected"] = count

    # ---------- шрифты ----------

    def install_font(self, game_dir: Path, project: TranslationProject, font_path) -> Optional[bool]:
        from ...fonts.library import ensure_font
        font = Path(font_path) if font_path else ensure_font()
        if font is None or not font.is_file():
            return False
        game_dir = Path(game_dir)
        backup = project.backup
        if self.version == "mv":
            return self._font_mv(game_dir, font, backup, project)
        if self.version == "mz":
            return self._font_mz(game_dir, font, backup, project)
        if self.version in ("vx", "ace"):
            return self._font_rgss(game_dir, font, backup, project)
        project.warnings.append("RPG Maker XP берёт шрифт из Windows: если вместо букв квадратики, "
                                "установите шрифт с кириллицей, указанный в настройках игры.")
        return None

    def _font_mv(self, game_dir: Path, font: Path, backup, project) -> bool:
        base = game_dir / "www" if (game_dir / "www").is_dir() else game_dir
        css = base / "fonts" / "gamefont.css"
        if not css.is_file():
            return False
        text = css.read_text(encoding="utf-8")
        m = re.search(r"""url\(\s*['"]?([^'")]+)['"]?\s*\)""", text)
        game_font = (css.parent / m.group(1)) if m else None
        if game_font and game_font.is_file() and _has_cyrillic_bytes(game_font.read_bytes()) is True:
            return True
        backup.copy_new(font, css.parent / font.name)
        face = ("\n/* Game Russificator: русские буквы из PT Sans, латиница — родным шрифтом */\n"
                f"@font-face {{ font-family: GameFont; src: url('{font.name}'); unicode-range: {CYRILLIC_RANGE}; }}\n")
        backup.write_text(css, text + face)
        self._preload_hint(base, backup, "GameFont")
        project.warnings.append("В шрифте игры нет русских букв — они подставлены из PT Sans.")
        return True

    def _font_mz(self, game_dir: Path, font: Path, backup, project) -> bool:
        ddir = mv_mz.data_dir(game_dir)
        sys_file = ddir / "System.json"
        try:
            sysdata = json.loads(sys_file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return False
        adv = sysdata.get("advanced") or {}
        fonts_dir = ddir.parent / "fonts"
        current = fonts_dir / str(adv.get("mainFontFilename") or "")
        if current.is_file() and _has_cyrillic_bytes(current.read_bytes()) is True:
            return True
        backup.copy_new(font, fonts_dir / font.name)
        adv["mainFontFilename"] = font.name
        sysdata["advanced"] = adv
        backup.write_text(sys_file, json.dumps(sysdata, ensure_ascii=False, separators=(",", ":")))
        project.warnings.append("В шрифте игры нет русских букв — основной шрифт заменён на PT Sans.")
        return True

    def _preload_hint(self, base: Path, backup, family: str) -> None:
        """Скрытый текст с кириллицей в index.html — шрифт загрузится до первого кадра."""
        index = base / "index.html"
        if not index.is_file():
            return
        html = index.read_text(encoding="utf-8")
        if "russificator-preload" in html or "</body>" not in html:
            return
        hint = (f'<div id="russificator-preload" style="font-family:{family};position:absolute;'
                f'left:-9999px;visibility:hidden">АаЯяЁё</div>\n')
        backup.write_text(index, html.replace("</body>", hint + "</body>"))

    def _font_rgss(self, game_dir: Path, font: Path, backup, project) -> bool:
        fonts_dir = game_dir / "Fonts"
        existing = [p for p in fonts_dir.glob("*") if p.suffix.lower() in (".ttf", ".otf", ".ttc")] \
            if fonts_dir.is_dir() else []
        if existing and all(_has_cyrillic_bytes(p.read_bytes()) is True for p in existing):
            return True
        ext = old_marshal.EXT[self.version]
        scripts = game_dir / "Data" / f"Scripts.{ext}"
        if not scripts.is_file():
            project.warnings.append("Скрипты игры не найдены — шрифт с кириллицей не подключён.")
            return False
        data, utf8 = M.load_with_info(scripts.read_bytes())
        if not isinstance(data, list):
            return False
        if any(isinstance(s, list) and len(s) > 1 and str(s[1]) == "Russificator Font" for s in data):
            return True
        if not fonts_dir.exists():
            fonts_dir.mkdir()
            backup.track_new(fonts_dir)
        backup.copy_new(font, fonts_dir / font.name)
        code = (f'# Game Russificator: шрифт с кириллицей\n'
                f'Font.default_name = ["{FONT_FAMILY}"] + Array(Font.default_name)\n')
        # код скрипта хранится как «бинарная» строка без кодировки — сжатые zlib байты как есть
        packed = M.RubyString(zlib.compress(code.encode("utf-8")).decode("utf-8", "surrogateescape"))
        entry = [90000001, "Russificator Font", packed]
        main = next((i for i, s in enumerate(data) if isinstance(s, list) and len(s) > 1 and str(s[1]) == "Main"),
                    len(data))
        data.insert(main, entry)
        raw = M.dump(data, utf8_strings=utf8 or self.version == "ace")
        M.load(raw)
        backup.write_bytes(scripts, raw)
        project.warnings.append("Шрифт игры без кириллицы — подключён PT Sans (папка Fonts, скрипт перед Main).")
        return True

    def post_inject_instructions(self) -> List[str]:
        return ["Запустите игру — текст будет на русском."]


from ...core.registry import register  # noqa: E402
RPGMakerPlugin = register(RPGMakerPlugin)
