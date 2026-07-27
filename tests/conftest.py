"""Test configuration.

Puts the repo root and ``scripts/`` on ``sys.path`` so the tests can import
``ingest`` directly (it lives in ``scripts/``, which is not a package), and so
its own ``from app.embeddings import ...`` resolves against the repo-root
``app`` package.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))
