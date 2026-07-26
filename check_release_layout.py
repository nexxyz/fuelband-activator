#!/usr/bin/env python3
"""Fail unless release/ and its macOS artifact contain only intended files."""

from pathlib import Path


EXPECTED_ROOT_FILES = frozenset(
    {
        ".gitignore",
        "LICENSE",
        "README.md",
        "check_release_layout.py",
        "fuelband_cli.py",
        "fuelband.ps1",
        "test_fuelband_cli.py",
        "test_wsl_hidraw_probe.py",
        "wsl_hidraw_probe.py",
        "macos",
    }
)
EXPECTED_MACOS_FILES = frozenset(
    {
        "README.md",
        "fuelband_macos.py",
        "requirements.txt",
        "test_fuelband_macos.py",
    }
)
IGNORED_METADATA = frozenset((".git",))


def main():
    root = Path(__file__).resolve().parent
    actual_root_files = frozenset(
        path.name for path in root.iterdir() if path.name not in IGNORED_METADATA
    )
    macos = root / "macos"
    actual_macos_files = frozenset(path.name for path in macos.iterdir()) if macos.is_dir() else frozenset()
    if actual_root_files != EXPECTED_ROOT_FILES or actual_macos_files != EXPECTED_MACOS_FILES:
        missing_root = sorted(EXPECTED_ROOT_FILES - actual_root_files)
        unexpected_root = sorted(actual_root_files - EXPECTED_ROOT_FILES)
        missing_macos = sorted(EXPECTED_MACOS_FILES - actual_macos_files)
        unexpected_macos = sorted(actual_macos_files - EXPECTED_MACOS_FILES)
        raise SystemExit(
            "release file allowlist mismatch; root missing=%r unexpected=%r; "
            "macos missing=%r unexpected=%r"
            % (missing_root, unexpected_root, missing_macos, unexpected_macos)
        )
    print("release file allowlist passed")


if __name__ == "__main__":
    main()
