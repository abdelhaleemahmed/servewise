"""servewise — a safe local preview server for built static sites.

Caching disabled, first-free-port selection, an identity endpoint, and version
self-checking. See ``servewise.server`` for the implementation.
"""
from .server import main, __version__

__all__ = ["main", "__version__"]
