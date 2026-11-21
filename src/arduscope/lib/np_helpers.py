"""Numpy helpers shared across analyzers and report builders."""

from __future__ import annotations

from typing import Optional

import numpy as np


def safe_nanmax(*arrays: np.ndarray,
                default: Optional[float] = None) -> Optional[float]:
    """Return the max across all arrays ignoring NaN.

    ``np.nanmax`` raises ``ValueError: All-NaN slice encountered`` when every
    element is NaN. This helper skips empty/all-NaN inputs and returns
    ``default`` if no array contributes a real value.

    Pass ``default=0.0`` for callsites that want a numeric fallback (e.g.
    "max variance is 0 when never recorded"); leave it as None when the
    caller distinguishes "no data" from "zero".
    """
    best: Optional[float] = None
    for arr in arrays:
        if len(arr) == 0 or np.all(np.isnan(arr)):
            continue
        v = float(np.nanmax(arr))
        if best is None or v > best:
            best = v
    return best if best is not None else default
