"""Промпт для нейросетей (локальной и облачной): перевод батча строк игры.

Инструкции — на английском: так их одинаково хорошо понимают и маленькие
локальные модели, и облачные. Строки уходят JSON-массивом с короткими id,
ответ — JSON-объект {id: перевод}. Контекст (говорящий, соседние реплики,
тип строки) помогает с родом, обращением на «ты/вы» и длиной UI-строк.
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Tuple

from ..core.universal import Entry, TextKind

SYSTEM_PROMPT = """You are a professional video game localizer. Translate English game text into Russian.

Rules:
1. Write natural, fluent Russian as in an official localization. Keep the tone, humor, slang and style of each character. Adapt idioms instead of translating them literally.
2. Copy every markup token EXACTLY as is, in the same quantity: tags like <b>, </color>, <size=20>; placeholders like {0}, {w}, {i}, [player], %s, %d, %1; engine codes like \\C[2], \\N[1], \\I[64], \\n, \\.; escaped braces {{ and [[. Never translate or change anything inside them. Do not add new tags.
3. Keep line breaks (\\n) in place when possible.
4. "speaker" and "context" are only there to help you (gender, formal/informal address, meaning). Translate ONLY the "text" field.
5. For type "ui" and "choice": short, like in Russian games (Save -> Сохранить, Quit -> Выход, Yes -> Да). Respect "max_chars" if given.
6. Names and terms from "glossary": use exactly the given Russian form; if the value is empty, choose one transliteration and use it consistently.
7. No quotes around the translation, no notes, no explanations. If the text is already Russian, is code, a file name or an identifier, return it unchanged.

Reply with ONLY a JSON object that maps every "id" to its Russian translation, e.g. {"1": "...", "2": "..."}."""

KIND_NAMES = {
    TextKind.DIALOGUE: "dialogue",
    TextKind.NARRATION: "narration",
    TextKind.CHOICE: "choice",
    TextKind.UI: "ui",
    TextKind.CHARACTER_NAME: "character name",
    TextKind.ITEM_NAME: "item name",
    TextKind.ITEM_DESCRIPTION: "item description",
    TextKind.SYSTEM: "ui",
    TextKind.OTHER: "game text",
}


def build_user_message(entries: List[Entry], glossary: Dict[str, str],
                       game_title: str = "") -> Tuple[str, Dict[str, str]]:
    """Собрать сообщение с батчем. Возвращает (текст, {короткий_id: entry.id})."""
    ids: Dict[str, str] = {}
    in_batch = {e.source for e in entries}
    items = []
    for n, e in enumerate(entries, start=1):
        sid = str(n)
        ids[sid] = e.id
        item = {"id": sid, "text": e.source, "type": KIND_NAMES.get(e.kind, "game text")}
        if e.speaker:
            item["speaker"] = e.speaker
        ctx = [c for c in (e.neighbors or []) if c and c not in in_batch][:2]
        if ctx:
            item["context"] = ctx
        if e.max_length:
            item["max_chars"] = e.max_length
        items.append(item)
    used_terms = {k: v for k, v in glossary.items()
                  if any(k in e.source or k == e.speaker for e in entries)}
    payload: Dict[str, object] = {}
    if game_title:
        payload["game"] = game_title
    if used_terms:
        payload["glossary"] = used_terms
    payload["items"] = items
    return json.dumps(payload, ensure_ascii=False, indent=1), ids


_THINK_RE = re.compile(r"<think>.*?</think>|<\|channel\|>analysis.*?<\|end\|>", re.S)


def parse_reply(text: str) -> Dict[str, str]:
    """Достать {id: перевод} из ответа модели (устойчиво к мусору вокруг JSON)."""
    text = _THINK_RE.sub("", text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    candidates = []
    if start != -1 and end > start:
        candidates.append((start, text[start:end + 1]))
    lstart, lend = text.find("["), text.rfind("]")
    if lstart != -1 and lend > lstart:
        candidates.append((lstart, text[lstart:lend + 1]))
    for _, raw in sorted(candidates):  # что начинается раньше — то и есть ответ
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        return _normalize(data)
    raise ValueError("в ответе модели нет JSON с переводами")


def _normalize(data) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if isinstance(data, dict):
        if isinstance(data.get("items"), list):
            return _normalize(data["items"])
        if isinstance(data.get("translations"), (dict, list)):
            return _normalize(data["translations"])
        for k, v in data.items():
            if isinstance(v, str):
                out[str(k)] = v
            elif isinstance(v, dict):
                val = v.get("text") or v.get("translation")
                if isinstance(val, str):
                    out[str(k)] = val
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "id" in item:
                val = item.get("translation") or item.get("text") or item.get("ru")
                if isinstance(val, str):
                    out[str(item["id"])] = val
    return out


SHORTEN_PROMPT = ("Shorten this Russian game UI translation to at most {limit} characters, keeping the meaning "
                  "and all markup tokens exactly. English original: {source}\nRussian: {text}\n"
                  "Reply with only the shortened Russian text.")
