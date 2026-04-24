"""One-shot CRSP + Compustat PIT pull from WRDS.

Run this once per data-cut. Writes raw responses to ``$AEGIS_DATA_DIR/raw/``
for downstream ``aegis data build``. Not idempotent — it is expected to be
re-run only when a new data cut is needed.
"""

from __future__ import annotations


def main() -> None:
    raise NotImplementedError("WRDS pull script — to be written during Module A (spec §12).")


if __name__ == "__main__":
    main()
