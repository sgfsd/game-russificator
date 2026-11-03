"""Язык оригинала игры: английский, другой язык на латинице, китайский, японский или корейский.

Программа делалась для английских игр, и «текстом» долго считалась только строка с латинскими буквами:
японская игра на RPG Maker почти целиком уходила в «пропущено» (из 7508 строк перевелись 53 — те, что
латиницей). Теперь язык оригинала определяется по тексту самой игры после извлечения (:func:`detect`):

  * китайский, японский, корейский — если иероглифов, каны и хангыля по объёму во много раз больше, чем
    латиницы: английские надписи в такой игре — меньшинство (параметры плагинов, «HP», «Save»);
  * иначе язык на латинице — английский, если по служебным словам не видно явно другого языка
    (испанский, французский, немецкий…).

Латиница переводится всегда — английский интерфейс бывает и в японской игре. Строки другого письма в
английской игре (китайская локализация рядом с английской, «日本語» в выборе языка) не переводятся: это
другие языки игры, а не её текст (:func:`translatable`).
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Iterable

_HAN = "㐀-䶿一-鿿豈-﫿"
_KANA = "ぁ-ゖゝゞァ-ヺヽヾㇰ-ㇿｦ-ﾝ"
_HANGUL = "가-힣ᄀ-ᅞᅡ-ᇿㄱ-ㅣㅥ-ㆎ"     # без невидимых заполнителей U+115F, U+1160, U+3164 (каомодзи, ники)
#: буква китайского, японского или корейского письма (знаки «ー», «・» и пунктуация — не буквы)
CJK_RE = re.compile(f"[{_HAN}{_KANA}{_HANGUL}]")
_KANA_RE = re.compile(f"[{_KANA}]")
_HAN_RE = re.compile(f"[{_HAN}]")
#: слово: от двух знаков подряд («ー» тянет слог: «セーブ»)
_CJK_RUN_RE = re.compile(f"[{_HAN}{_KANA}{_HANGUL}][{_HAN}{_KANA}{_HANGUL}ー]+")
_HANGUL_RE = re.compile(f"[{_HANGUL}]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")
_LATIN_RE = re.compile(r"[A-Za-z]")

#: языки с иероглифами, каной или хангылем
CJK_LANGS = ("ja", "zh", "ko")

#: название языка: для подсказки нейросети (по-английски) и для журнала — по-русски, «с японского»,
#: «на японском»
NAMES = {
    "en": ("English", "английский", "английского", "английском"),
    "ja": ("Japanese", "японский", "японского", "японском"),
    "zh": ("Chinese", "китайский", "китайского", "китайском"),
    "ko": ("Korean", "корейский", "корейского", "корейском"),
    "es": ("Spanish", "испанский", "испанского", "испанском"),
    "fr": ("French", "французский", "французского", "французском"),
    "de": ("German", "немецкий", "немецкого", "немецком"),
    "it": ("Italian", "итальянский", "итальянского", "итальянском"),
    "pt": ("Portuguese", "португальский", "португальского", "португальском"),
    "pl": ("Polish", "польский", "польского", "польском"),
}

#: частые служебные слова, почти не встречающиеся в других языках из списка
_STOPWORDS = {
    "en": "the and you to of is it that this what for with are have not your be on my was don't i'm can will",
    "es": "el los las que y es un una por con para pero del tu mi está esto como más muy también ya",
    "fr": "le les et est vous je pas une des du qui dans pour ce il sur au mais avec elle sont",
    "de": "der die das und ist nicht ich du sie ein eine zu mit den auf dem sich wir auch nur",
    "it": "il che di non è una per sono gli della ho ma questo sei anche come mi più",
    "pt": "não é um uma os você do da em para com eu mas isso está ele ela também",
    "pl": "nie się na to że jest co jak ale tak mnie już czy jestem może tylko",
}
_STOP = {lang: set(words.split()) for lang, words in _STOPWORDS.items()}
_WORD_RE = re.compile(r"[a-zà-öø-ÿąćęłńóśźż']+")


def cjk_letters(text: str) -> int:
    """Сколько в строке букв китайского, японского или корейского письма."""
    return len(CJK_RE.findall(text or ""))


def has_cjk(text: str) -> bool:
    return bool(CJK_RE.search(text or ""))


#: полноширинная пунктуация — так пишут только по-китайски и по-японски: «グラス（Hot Shot）»
_CJK_PUNCT_RE = re.compile(r"[（）「」『』【】、。，！？：；～]")


def cjk_text(text: str) -> bool:
    """Строка на китайском, японском, корейском: «第1章», «HP回復», «グラス（Hot Shot）», «按住Alt键…».
    Иероглиф весит как слово (четыре латинские буквы), знак каны и хангыля — как слог (три), полноширинная
    пунктуация — как буква; латиницы должно быть больше. Английская фраза с каомодзи («Meow! ㅤ/ᐠ - ˕ -マ»)
    или японским словом в кавычках («The memorial says "母梅の髪".») — не китайская и не японская."""
    if not CJK_RE.search(text or ""):
        return False
    latin = len(_LATIN_RE.findall(text))
    if not latin:
        return True
    han = len(_HAN_RE.findall(text))
    # одиночный знак каны среди латиницы — «рука» каомодзи («ヾ(oﾟДﾟ)ﾉ»), слово — от двух знаков подряд
    words = "".join(_CJK_RUN_RE.findall(text))
    syllables = len(CJK_RE.findall(words)) - len(_HAN_RE.findall(words))
    weight = 4 * han + 3 * syllables + len(_CJK_PUNCT_RE.findall(text))
    return weight > latin


def is_cjk(lang: str) -> bool:
    return lang in CJK_LANGS


def detect(texts: Iterable[str]) -> str:
    """Язык оригинала по извлечённым строкам игры (код: en, ja, zh, ko, es, fr, de, it, pt, pl)."""
    from .markup import plain_text
    latin_words = han = kana = hangul = 0
    sample = []
    for t in texts:
        p = plain_text(t or "")
        latin_words += len(_LATIN_WORD_RE.findall(p))
        found = CJK_RE.findall(p)
        if found:
            k = len(_KANA_RE.findall(p))
            h = len(_HANGUL_RE.findall(p))
            kana += k
            hangul += h
            han += len(found) - k - h
        if len(sample) < 20000 and _LATIN_WORD_RE.search(p):
            sample.append(p)
    # объём текста в «словах»: в китайском и японском слово — около двух знаков, в корейском — трёх слогов
    cjk_units = (han + kana) / 2 + hangul / 3
    if cjk_units and latin_words < 0.15 * cjk_units:
        if hangul > han + kana:
            return "ko"
        return "ja" if kana >= 0.05 * (han + kana) else "zh"
    return _latin_language(sample)


def _latin_language(texts: Iterable[str]) -> str:
    """Язык на латинице по служебным словам: другой, только если его слов вдвое больше английских."""
    score: Counter = Counter()
    for t in texts:
        for w in _WORD_RE.findall(t.lower()):
            for lang, words in _STOP.items():
                if w in words:
                    score[lang] += 1
    best, n = max(score.items(), key=lambda kv: kv[1], default=("en", 0))
    if best != "en" and n >= 30 and n >= 2 * score["en"]:
        return best
    return "en"


def translatable(text: str, lang: str) -> bool:
    """Есть ли в строке что переводить с языка ``lang``. В игре на китайском, японском, корейском — буква
    их письма или слово латиницей (английский интерфейс). В игре на английском — слово латиницей, если сама
    строка не китайская и не японская (:func:`cjk_text`): «UI尺寸», «グラス（Hot Shot）» — строки другой
    локализации, перевод одних латинских кусков давал кашу «ПОЛЬЗОВАТЕЛЬСКИЙ ИНТЕРФЕЙС尺寸». Но если латиница —
    фраза языка игры («Put it down and 捨てぷ 番号», «Don't 好かれて it.»: в 1001 vies чужая речь вставлена
    в английскую реплику), строка — текст игры: переводится английское, вставки остаются."""
    from .markup import plain_text
    p = plain_text(text or "")
    if is_cjk(lang):
        return bool(re.search(r"[A-Za-z]{2}", p)) or has_cjk(p)
    if not re.search(r"[A-Za-z]{2}", p):
        return False
    return not cjk_text(p) or with_inserts(text, lang)


def with_inserts(text: str, lang: str) -> bool:
    """Строка с большим числом иероглифов и каны — всё же реплика на языке игры со вставками чужого письма,
    а не строка китайской, японской локализации («\\c[0]Put it down and\\c[23] 捨てぷ 番号\\c[0].»: в 1001 vies
    так «говорят» на непонятном языке). Признак — служебные слова языка игры («it», «and») или слово,
    отделённое от вставки разметкой («\\c[0]Go\\c[23] ヴェリテ \\c[0]slowly.»), а не иероглифом. Кусок
    КАПСОМ («<Task>SUBJECT 01</>…») — метка, а не реплика. Решает и что отправлять в перевод (:func:`translatable`),
    и как проверять результат (``edges.check``: вставки в переводе остаются, а не «остались иероглифы»)."""
    from .markup import plain_text, split_by_markup
    if is_cjk(lang):
        return False
    if _native_words(plain_text(text or ""), lang):
        return True
    return any(not tag and re.search(r"[A-Za-z]{3}", piece) and re.search(r"[a-z]", piece) and not has_cjk(piece)
               for tag, piece in split_by_markup(text or ""))


#: ещё служебные слова для :func:`_native_words` — не для определения языка: «in», «me», «so» есть и в других
#: языках. Однобуквенных («a», «I») нет: «A.S.W.D를 눌러…» — клавиши, а не фраза
_PHRASE_WORDS = {"en": set("its his her he she we they me so do but just all in".split())}


def _native_words(text: str, lang: str) -> bool:
    """Есть ли в строке частые служебные слова языка ``lang`` («the», «and», «it»…) — признак фразы, а не
    названия, клавиши или сокращения («Jerry», «SPACE», «UI»)."""
    stop = _STOP.get(lang, set()) | _PHRASE_WORDS.get(lang, set())
    return bool(stop) and any(w in stop for w in _WORD_RE.findall(text.lower()))


def name(lang: str, case: int = 0) -> str:
    """Название языка: 0 — по-английски (для нейросети), 1 — по-русски, 2 — «с какого», 3 — «на каком»."""
    return NAMES.get(lang, NAMES["en"])[case]


#: пунктуация китайского и японского письма в русском переводе → русская
_CJK_PUNCT = str.maketrans({
    "「": "«", "」": "»", "『": "«", "』": "»", "〈": "«", "〉": "»", "《": "«", "》": "»",
    "。": ".", "、": ",", "，": ",", "！": "!", "？": "?", "：": ":", "；": ";",
    "（": "(", "）": ")", "［": "[", "］": "]", "｛": "{", "｝": "}", "～": "~", "〜": "~",
    "　": " ", "‥": "…", "―": "—", "─": "—", "━": "—",
})


def russian_punctuation(text: str) -> str:
    """Перевод с китайского или японского: их кавычки «「」», точки «。» и запятые «、» — русскими (в шрифте
    с кириллицей их часто нет, да и по-русски так не пишут). «……» и «――» (пауза и долгое тире японского) —
    одним знаком; полноширинные буквы и цифры — обычные. Скобки имён «【】» остаются: это оформление."""
    from .markup import split_by_markup

    def fix(part: str) -> str:
        part = re.sub(r"([。、，！？：；])(?=\w)", r"\1 ", part)        # после «。» японского пробела нет
        part = part.translate(_CJK_PUNCT)
        part = re.sub(r"…{2,}", "…", part)
        part = re.sub(r"—{2,}", "—", part)
        part = re.sub(r"(?<=\w)・(?=\w)", " ", part).replace("・", "•")     # «Джон・Смит», «・Пункт»
        part = re.sub(r"(^|\s)([—•])(?=\w)", r"\1\2 ", part)              # «— Нет», «• Пункт»
        return "".join(chr(ord(c) - 0xFEE0) if "！" <= c <= "～" else c for c in part)
    return "".join(c if is_tag else fix(c) for is_tag, c in split_by_markup(text))


def display_width(text: str) -> int:
    """Ширина строки в знаках латиницы: иероглиф и полноширинный знак — два (место в окне игры)."""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)
