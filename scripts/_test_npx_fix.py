"""One-shot test: confirm shutil.which finds npx, then print its path."""

import shutil
import sys

npx = shutil.which("npx")
if npx:
    print(f"OK: npx found at {npx}")
    sys.exit(0)
else:
    print("FAIL: npx not found")
    sys.exit(1)
