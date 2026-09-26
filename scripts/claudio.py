#!/usr/bin/env python3
"""The entrypoint's name from before claudio became Hobson (0.3.0).

Hooks installed before the rename run `.../scripts/claudio.py`. A running
Claude Code session keeps the hooks it started with, and settings.json is
rewritten only when the installer runs, so this stays and hands each event
to hobson.py unchanged. `hobson doctor` reports hooks still pointing here.
"""

import os
import runpy

runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "hobson.py"),
               run_name="__main__")
