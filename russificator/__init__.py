"""Game Russificator — универсальный авторусификатор PC-игр.

Публичный API:
    russificator.detect_engine(path)                  -> (плагин, Detection)
    russificator.create_translator(mode, options)     -> Translator
    russificator.run_russification(game_dir, mode, …) -> PipelineResult
"""

__version__ = "2.2.0"

from .translation import ORDER_CONTACT, ORDER_TELEGRAM, create_translator  # noqa: E402,F401


def detect_engine(game_dir):
    from .core.registry import detect_engine as _detect
    return _detect(game_dir)


def run_russification(game_dir, mode: str = "machine", on_event=None, **options):
    """Запуск в одну строку: определить движок, извлечь, перевести, внедрить."""
    from .core.pipeline import Pipeline, PipelineOptions
    translator = create_translator(mode, options)
    return Pipeline(translator, PipelineOptions(), on_event=on_event).run(game_dir)
