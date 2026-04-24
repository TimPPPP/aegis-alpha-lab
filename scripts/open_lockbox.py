"""Single-use 2024-2025 locked sub-holdout opener (spec §5.2).

This is the one script that is run exactly once across V1's lifetime, after
all code and configs are frozen. Rerunning it is a protocol violation.
"""

from __future__ import annotations


def main() -> None:
    raise NotImplementedError(
        "Lockbox opener — implemented after all other modules freeze (spec §12 week 19-20)."
    )


if __name__ == "__main__":
    main()
