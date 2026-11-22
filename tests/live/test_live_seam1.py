"""The Seam 1 suite of `tests/seam1_shared.py`, against the sandboxes of the real cluster."""

import pytest
from seam1_shared import *

pytestmark = pytest.mark.live
