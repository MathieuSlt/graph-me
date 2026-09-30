"""Write the synthetic fixtures to tests/fixtures/generated/ (git-ignored)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from factory import make_docs  # noqa: E402

if __name__ == "__main__":
    out = ROOT / "tests" / "fixtures" / "generated"
    docs = make_docs(out)
    print(f"docs fixture: {docs}")
