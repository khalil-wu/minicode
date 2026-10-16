"""Preserve the executor's explicit argv[0] inside the existing sandbox."""
import json
import os
import sys

argv = json.loads(sys.argv[1])
os.execv(argv[0], [sys.argv[2], *argv[1:]])
