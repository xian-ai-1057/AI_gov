"""Process-wide limits shared by parser vision calls and review model calls."""

from __future__ import annotations

import threading

# One shared budget prevents concurrent uploads from independently saturating
# OCR/VLM and review endpoints. Deployment-level worker limits remain separate.
MODEL_CALL_SEMAPHORE = threading.BoundedSemaphore(4)
