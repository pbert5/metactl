"""A small Textual peer runtime for compiled Meta WebUI documents.

The package deliberately imports neither application configuration nor any
domain package.  Applications provide a compiled document and source/action
adapters at the boundary.
"""
from .runtime import ApplicationLoader, TextualApplication, render_document

__all__ = ["ApplicationLoader", "TextualApplication", "render_document"]
