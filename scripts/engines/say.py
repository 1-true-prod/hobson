"""macOS say engine — zero dependencies, instant playback."""

from .base import BaseEngine


class SayEngine(BaseEngine):
    """Uses macOS `say` command. No cache, no dependencies."""

    engine_name = "say"
    templates_module = "bark_templates"
    cache_dir = None
    cache_ext = None

    def backfill(self, text):
        pass  # No cache to backfill
