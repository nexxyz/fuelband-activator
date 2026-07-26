#!/usr/bin/env python3
"""Fail unless release/ contains exactly the intended publishable files."""

from pathlib import Path


EXPECTED_FILES = frozenset(
    {
        ".gitignore",
        "LICENSE",
        "README.md",
        "check_release_layout.py",
        "fuelband_cli.py",
        "fuelband.ps1",
        "test_fuelband_cli.py",
    }
)


def main():
    actual_files = frozenset(path.name for path in Path(__file__).parent.iterdir())
    if actual_files != EXPECTED_FILES:
        missing = sorted(EXPECTED_FILES - actual_files)
        unexpected = sorted(actual_files - EXPECTED_FILES)
        raise SystemExit("release file allowlist mismatch; missing=%r unexpected=%r" % (missing, unexpected))
    print("release file allowlist passed")


if __name__ == "__main__":
    main()
