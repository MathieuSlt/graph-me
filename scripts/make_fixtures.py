"""Write the synthetic fixtures to tests/fixtures/generated/ (git-ignored)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import shutil  # noqa: E402

from factory import build_msgvault, make_docs  # noqa: E402

from graph_me.connectors.filesystem import IGNORE_MARKER  # noqa: E402

if __name__ == "__main__":
    out = ROOT / "tests" / "fixtures" / "generated"
    docs = make_docs(out)
    # synthetic data: keep it out of a real scan that walks over this clone
    (out / IGNORE_MARKER).write_text("graph-me test fixtures, not personal data\n")
    print(f"docs fixture: {docs}")
    if shutil.which("msgvault"):
        lease = (docs / "Logement/Contrat_bail_2025.pdf").read_bytes()
        home = build_msgvault(out, lease)
        print(f"msgvault fixture: {home}  (contacts: {out / 'contacts/contacts.vcf'})")
    else:
        print("msgvault not installed: skipping the mail/WhatsApp fixture")
