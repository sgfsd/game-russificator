"""Слой перевода: четыре способа перевести игру.

  1. ``machine`` — машинный переводчик офлайн (слабые/старые ПК);
  2. ``local``   — нейросеть Gemma 4 на своём компьютере (мощные ПК);
  3. ``cloud``   — облачная нейросеть по API-ключу (любой провайдер);
  4. заказ перевода у автора — см. :data:`ORDER_CONTACT` (в интерфейсе
     отдельное окно, перевода программой не выполняется).
"""

from __future__ import annotations

from typing import Any, Dict

from .base import Translator, TranslatorError

ORDER_TELEGRAM = "TRAPYCHINO"
ORDER_CONTACT = f"https://t.me/{ORDER_TELEGRAM}"

MODES = ("machine", "local", "cloud")


def create_translator(mode: str, options: Dict[str, Any], game_title: str = "") -> Translator:
    """Создать переводчик по способу и настройкам (без скачиваний — это в prepare)."""
    if mode == "machine":
        from .machine import MachineTranslator
        return MachineTranslator(threads=int(options.get("threads") or 0))
    if mode == "local":
        from .local_llm import LocalTranslator
        return LocalTranslator(preset_id=options.get("local_model", ""),
                               use_gpu=bool(options.get("local_gpu", True)),
                               threads=int(options.get("threads") or 0),
                               custom_model=options.get("local_custom_model", ""),
                               game_title=game_title)
    if mode == "cloud":
        from .cloud import CloudTranslator
        return CloudTranslator(options.get("cloud_provider", "custom"), options.get("api_key", ""),
                               base_url=options.get("cloud_base_url", ""), model=options.get("cloud_model", ""),
                               parallel=int(options.get("cloud_parallel") or 4), game_title=game_title)
    raise TranslatorError(f"Неизвестный способ перевода: {mode}")
