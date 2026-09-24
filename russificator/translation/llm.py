"""Перевод нейросетью через OpenAI-совместимый API (общая часть способов 2 и 3).

Один и тот же клиент работает с локальным llama.cpp-сервером (способ 2)
и с любым облачным провайдером (способ 3): DeepSeek, OpenRouter, Gemini,
OpenAI, Anthropic, Mistral, Ollama, LM Studio...

Надёжность:
  * ошибки делятся на фатальные (неверный ключ, нет денег, нет модели —
    русификация сразу останавливается с понятным сообщением) и временные
    (лимит запросов, сбой сети/сервера — повтор с паузой);
  * если модель вернула битый/обрезанный JSON, батч делится пополам и
    переводится по частям — вплоть до одной строки;
  * провайдерам без JSON-режима запрос повторяется без него.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Dict, List, Optional

from .. import net
from ..core.universal import Entry
from .base import StatusFn, Translator, TranslatorError, _noop
from .prompt import SHORTEN_PROMPT, SYSTEM_PROMPT, build_user_message, parse_reply

log = logging.getLogger("russificator.llm")


class _BadReply(Exception):
    """Ответ не разобрать/обрезан — батч нужно поделить."""


class ChatClient:
    """Минимальный клиент OpenAI-совместимого /chat/completions."""

    def __init__(self, base_url: str, api_key: str = "", model: str = "", timeout: float = 300,
                 extra: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None,
                 max_retries: int = 6, local: bool = False):
        self.base_url = base_url.rstrip("/")
        self.api_key = (api_key or "").strip()
        self.model = model
        self.timeout = timeout
        self.extra = extra or {}
        self.headers = headers or {}
        self.max_retries = max_retries
        self.local = local
        self.json_mode: Optional[bool] = True
        self._lock = threading.Lock()

    def _headers(self) -> Dict[str, str]:
        h = dict(self.headers)
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def list_models(self) -> List[str]:
        data = net.get_json(f"{self.base_url}/models", headers=self._headers(), timeout=20)
        items = data.get("data", data.get("models", [])) if isinstance(data, dict) else data
        ids = []
        for m in items or []:
            mid = m.get("id") or m.get("name") if isinstance(m, dict) else str(m)
            if mid:
                ids.append(str(mid).removeprefix("models/"))
        return sorted(set(ids))

    def chat(self, system: str, user: str, temperature: float = 0.2,
             max_tokens: Optional[int] = None, want_json: bool = True) -> str:
        attempt = 0
        while True:
            payload: Dict[str, Any] = {
                "model": self.model or "local",
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "temperature": temperature,
            }
            if max_tokens:
                payload["max_tokens"] = max_tokens
            if want_json and self.json_mode:
                payload["response_format"] = {"type": "json_object"}
            payload.update(self.extra)
            try:
                data = net.post_json(f"{self.base_url}/chat/completions", payload,
                                     headers=self._headers(), timeout=self.timeout)
            except net.NetError as exc:
                self._classify(exc, attempt)  # бросает фатальные ошибки
                if exc.status == 400 and want_json and self.json_mode and _mentions_json(exc):
                    with self._lock:
                        self.json_mode = False  # провайдер не умеет JSON-режим — живём без него
                    continue
                attempt += 1
                if attempt > self.max_retries:
                    raise TranslatorError(f"Сервер перевода не отвечает: {exc}",
                                          fatal=self.local or exc.status == 0) from None
                time.sleep(_backoff(attempt, exc))
                continue
            return _content(data)

    def _classify(self, exc: net.NetError, attempt: int) -> None:
        s, body = exc.status, (exc.body or str(exc)).lower()
        if s in (401, 403):
            raise TranslatorError("Ключ API не подходит или у него нет доступа к модели "
                                  f"({exc}). Проверьте ключ и адрес API.") from None
        money = ("insufficient_quota", "insufficient balance", "insufficient funds", "insufficient credits",
                 "exceeded your current quota", "billing", "requires more credits", "payment required")
        if s == 402 or (s in (400, 403, 429) and any(m in body for m in money)):
            raise TranslatorError(f"На счёте провайдера закончились деньги или квота ({exc}).") from None
        if s == 404 or (s == 400 and "model" in body and ("not found" in body or "does not exist" in body
                                                          or "invalid model" in body)):
            raise TranslatorError(f"Модель «{self.model}» не найдена у провайдера или неверный адрес API "
                                  f"({exc}). Нажмите «Обновить список моделей».") from None
        if s == 400 and not _mentions_json(exc):
            raise TranslatorError(f"Провайдер отклонил запрос: {exc}", fatal=False) from None


def _mentions_json(exc: net.NetError) -> bool:
    body = (exc.body or str(exc)).lower()
    return "response_format" in body or "json" in body


def _backoff(attempt: int, exc: net.NetError) -> float:
    base = 5.0 if exc.status == 429 else 2.0
    return min(60.0, base * (2 ** (attempt - 1))) * (0.8 + random.random() * 0.4)


def _content(data: Any) -> str:
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError, TypeError):
        raise _BadReply(f"неожиданный ответ сервера: {str(data)[:200]}") from None
    msg = choice.get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if choice.get("finish_reason") == "length":
        raise _BadReply("ответ обрезан по длине")
    if not content:
        raise _BadReply("пустой ответ")
    return content


class LLMTranslator(Translator):
    """Переводчик на нейросети: батчи с контекстом, JSON-ответ, самовосстановление."""

    context_aware = True

    def __init__(self, client: ChatClient, cache_id: str, title: str, parallel: int = 1,
                 max_batch_items: int = 25, max_batch_chars: int = 2500, game_title: str = ""):
        self.client = client
        self.cache_id = cache_id
        self.title = title
        self.parallel = max(1, parallel)
        self.max_batch_items = max_batch_items
        self.max_batch_chars = max_batch_chars
        self.game_title = game_title

    def prepare(self, status: StatusFn = _noop, cancel: Optional[threading.Event] = None) -> None:
        status("Проверка подключения к нейросети…", None)
        try:
            reply = self.client.chat("Translate to Russian. Reply with JSON {\"1\": translation}.",
                                     '{"items": [{"id": "1", "text": "Hello!"}]}', max_tokens=64)
        except _BadReply as exc:
            raise TranslatorError(f"Нейросеть отвечает некорректно: {exc}") from None
        if not reply.strip():
            raise TranslatorError("Нейросеть вернула пустой ответ.")
        status("Нейросеть отвечает", 1.0)

    def _max_tokens(self, entries: List[Entry]) -> Optional[int]:
        if not self.client.local:
            return None
        chars = sum(len(e.source) for e in entries)
        return min(8192, int(chars * 1.2) + 40 * len(entries) + 200)

    def translate(self, entries: List[Entry], glossary: Dict[str, str]) -> Dict[str, str]:
        if not entries:
            return {}
        user, ids = build_user_message(entries, glossary, self.game_title)
        try:
            raw = self.client.chat(SYSTEM_PROMPT, user, max_tokens=self._max_tokens(entries))
            parsed = parse_reply(raw)
        except (_BadReply, ValueError) as exc:
            if len(entries) == 1:
                log.warning("Строка %s не переведена: %s", entries[0].id, exc)
                return {}
            mid = len(entries) // 2
            log.info("Батч из %d строк не разобран (%s) — делим пополам", len(entries), exc)
            out = self.translate(entries[:mid], glossary)
            out.update(self.translate(entries[mid:], glossary))
            return out
        result = {ids[k]: v for k, v in parsed.items() if k in ids}
        missing = [e for e in entries if e.id not in result]
        if missing and len(missing) < len(entries):
            result.update(self.translate(missing, glossary))  # модель пропустила часть строк
        return result

    def retry_markup(self, entry: Entry, glossary: Dict[str, str], lost: List[str]) -> Optional[str]:
        """Повторить строку, в переводе которой потерялась разметка.

        Вставки заменяются простыми маркерами @0, @1… (как в машинном
        переводе) — их не «улучшит» даже маленькая модель, — а после
        перевода возвращаются на место.
        """
        from dataclasses import replace
        from . import markup

        masked, originals, sign = markup.mask(entry.source)
        probe = replace(entry, source=masked, neighbors=[])
        user, _ = build_user_message([probe], glossary, self.game_title)
        system = SYSTEM_PROMPT + (f"\n\nIMPORTANT: tokens like {sign}0, {sign}1 are placeholders for markup. "
                                  "Keep every one of them exactly once, where it belongs in the sentence.")
        try:
            parsed = parse_reply(self.client.chat(system, user, max_tokens=self._max_tokens([entry])))
        except (_BadReply, ValueError, TranslatorError):
            return None
        text = parsed.get("1")
        if not text:
            return None
        restored, ok = markup.unmask(text, originals, sign, masked_source=masked)
        return restored if ok else None

    def shorten(self, text: str, limit: int, source: str) -> Optional[str]:
        try:
            reply = self.client.chat("You are an editor of Russian game localization.",
                                     SHORTEN_PROMPT.format(limit=limit, source=source, text=text),
                                     want_json=False, max_tokens=200 if self.client.local else None)
        except (TranslatorError, _BadReply):
            return None
        return reply.strip().strip('"«»').strip() or None
