"""Способ 3: облачная нейросеть по API-ключу пользователя.

Любой OpenAI-совместимый провайдер: достаточно адреса API, ключа и имени
модели. Для популярных провайдеров есть пресеты. Названия моделей меняются
каждые несколько месяцев, поэтому зашитые значения — только стартовые:
кнопка «Обновить список» спрашивает у провайдера актуальные модели, а
:func:`recommend` выбирает из них подходящую для перевода (быструю и
дешёвую). Запросы идут напрямую к провайдеру, без посредников.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .base import TranslatorError
from .llm import ChatClient, LLMTranslator


@dataclass
class Provider:
    id: str
    title: str
    base_url: str
    default_model: str
    key_url: str = ""
    note: str = ""
    needs_key: bool = True
    headers: Dict[str, str] = field(default_factory=dict)
    #: модели, которые стоит предложить первыми (подстроки, по приоритету)
    prefer: List[str] = field(default_factory=list)


PROVIDERS: Dict[str, Provider] = {p.id: p for p in [
    Provider("deepseek", "DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat",
             "https://platform.deepseek.com/api_keys",
             "Дешёвый и очень хорошо знает русский. Оплата картой.",
             prefer=["deepseek-chat", "flash", "deepseek-v"]),
    Provider("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", "deepseek/deepseek-v4.1-flash",
             "https://openrouter.ai/settings/keys",
             "Сотни моделей по одному ключу, есть бесплатные (с пометкой :free).",
             headers={"X-Title": "Game Russificator"},
             prefer=["deepseek/deepseek-v4", "google/gemini-3", "flash", "deepseek/", ":free"]),
    Provider("gemini", "Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai",
             "gemini-flash-latest", "https://aistudio.google.com/apikey",
             "Есть бесплатный лимит запросов в день.",
             prefer=["gemini-flash-latest", "flash-lite", "flash"]),
    Provider("openai", "OpenAI", "https://api.openai.com/v1", "gpt-6-luna",
             "https://platform.openai.com/api-keys", "",
             prefer=["luna", "mini", "gpt-"]),
    Provider("anthropic", "Anthropic Claude", "https://api.anthropic.com/v1", "claude-haiku-4-5",
             "https://console.anthropic.com/settings/keys", "Отличное качество литературного перевода.",
             prefer=["haiku", "sonnet"]),
    Provider("mistral", "Mistral", "https://api.mistral.ai/v1", "mistral-small-latest",
             "https://console.mistral.ai/api-keys", "",
             prefer=["mistral-small-latest", "mistral-medium-latest", "small", "medium"]),
    Provider("ollama", "Ollama (свой ПК)", "http://localhost:11434/v1", "",
             "https://ollama.com", "Уже установленный Ollama на этом компьютере.", needs_key=False),
    Provider("lmstudio", "LM Studio (свой ПК)", "http://localhost:1234/v1", "",
             "https://lmstudio.ai", "Запустите сервер в LM Studio (вкладка Developer).", needs_key=False),
    Provider("custom", "Другой провайдер", "", "", "",
             "Любой OpenAI-совместимый API: укажите адрес (…/v1), ключ и модель.", needs_key=False),
]}

#: модели, которые для перевода не годятся (картинки, речь, эмбеддинги...)
_NOT_CHAT = re.compile(r"embed|whisper|tts|speech|audio|image|vision-exp|dall-e|moderation|rerank|realtime|"
                       r"transcri|ocr|search|computer-use|guard|codestral|coder|-code|sora|veo|imagen|lyria",
                       re.I)
_SLOW = re.compile(r"reasoner|thinking|-r1|\bo\d|pro-preview|deep-research", re.I)


def recommend(provider_id: str, models: List[str]) -> List[str]:
    """Отсортировать модели провайдера: сначала подходящие для перевода."""
    p = PROVIDERS.get(provider_id)
    chat = [m for m in models if not _NOT_CHAT.search(m)]

    def score(m: str) -> tuple:
        pref = len(p.prefer) if p else 0
        if p:
            for i, needle in enumerate(p.prefer):
                if needle in m:
                    pref = i
                    break
        dated = bool(re.search(r"\d{4}-?\d{2}-?\d{2}|-\d{4}$|preview|exp", m))
        return (pref, bool(_SLOW.search(m)), dated, -_version(m), m)

    return sorted(chat, key=score)


def _version(model: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)", model)
    try:
        return float(m.group(1)) if m else 0.0
    except ValueError:
        return 0.0


def make_client(provider_id: str, api_key: str, base_url: str = "", model: str = "") -> ChatClient:
    p = PROVIDERS.get(provider_id, PROVIDERS["custom"])
    url = (base_url or p.base_url).strip()
    return ChatClient(url, api_key=api_key, model=(model or p.default_model).strip(), headers=p.headers)


def list_models(provider_id: str, api_key: str, base_url: str = "") -> List[str]:
    """Актуальные модели провайдера, подходящие сверху."""
    return recommend(provider_id, make_client(provider_id, api_key, base_url).list_models())


class CloudTranslator(LLMTranslator):
    def __init__(self, provider_id: str, api_key: str, base_url: str = "", model: str = "",
                 parallel: int = 4, game_title: str = ""):
        p = PROVIDERS.get(provider_id, PROVIDERS["custom"])
        client = make_client(provider_id, api_key, base_url, model)
        if not client.base_url:
            raise TranslatorError("Не указан адрес API провайдера.")
        if not client.model or client.model == "local":
            raise TranslatorError("Не указано имя модели — выберите её из списка.")
        if p.needs_key and not client.api_key:
            raise TranslatorError(f"Не указан API-ключ {p.title}. Получить ключ: {p.key_url}")
        local = provider_id in ("ollama", "lmstudio") or "localhost" in client.base_url \
            or "127.0.0.1" in client.base_url
        client.local = local
        super().__init__(client, cache_id=f"llm:{client.model}", title=f"{p.title}: {client.model}",
                         parallel=1 if local else parallel,
                         max_batch_items=20 if local else 30,
                         max_batch_chars=2000 if local else 3500,
                         game_title=game_title)

    @staticmethod
    def provider(provider_id: str) -> Optional[Provider]:
        return PROVIDERS.get(provider_id)
