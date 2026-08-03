"""Local telco voice assistant."""

import os
from pathlib import Path

# Pipecat's sentence aggregator consults NLTK at import time. Point it at the explicitly
# provisioned application cache before any Pipecat submodule is imported.
_model_cache = Path(os.getenv("MODEL_CACHE_DIR", "models"))
os.environ.setdefault("NLTK_DATA", str(_model_cache / "nltk"))

__version__ = "0.1.0"
