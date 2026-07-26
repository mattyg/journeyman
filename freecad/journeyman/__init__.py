"""Namespace package for the Journeyman addon."""

# Compatibility aliases for integrations written against the former flat
# package. Implementations live in the transcript domain package.
from .transcript import export as transcript_export
from .transcript import storage as history_store

__all__ = ["history_store", "transcript_export"]
