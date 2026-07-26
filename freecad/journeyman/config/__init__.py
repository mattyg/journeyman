"""Configuration model façade; Qt preferences remain in ``preferences``."""

from .settings import Settings, load_settings, save_settings

__all__ = ["Settings", "load_settings", "save_settings"]
