#!/usr/bin/env python3
"""Backward-compatible CLI wrapper and public API re-exports."""

from __future__ import annotations

import sys

from counter import *
from git_utils import *
from main import *
from models import *
from output import *


if __name__ == "__main__":
    raise SystemExit(entrypoint(sys.argv[1:]))
