"""
Test that requirements/*.in and requirements/*.txt files are consistent.

This test checks that when a package is removed from a .in file, the
corresponding .txt file is also updated. It does NOT run any dependency
resolution - it only parses the files directly.

The test detects packages that are listed in .txt files as direct dependencies
(via the .in file) but are no longer present in the .in file, which indicates
the .txt file needs to be regenerated.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple


class PackageInfo(NamedTuple):
    """Information about a package in a requirements file."""
    name: str
    line_number: int
    raw_line: str


def normalize_package_name(name: str) -> str:
    """
    Normalize package name according to PEP 503.

    Package names are case-insensitive and treat hyphens, underscores,
    and periods as equivalent.
    """
    return re.sub(r"[-_.]+", "-", name.lower())


def parse_package_name(line: str) -> str | None:
    """
    Extract the package name from a requirements line.

    Handles various formats:
    - simple: requests
    - with version: requests>=2.0
    - with extras: requests[security]
    - with markers: requests; python_version >= "3.8"
    - editable: -e git+https://...#egg=package

    Returns None for comments, options, constraints, or unparseable lines.
    """
    line = line.strip()

    # Skip empty lines and comments
    if not line or line.startswith("#"):
        return None

    # Skip pip options (lines starting with -)
    # but handle -e (editable) specially
    if line.startswith("-"):
        if line.startswith("-e ") or line.startswith("--editable "):
            # Try to extract egg name
            egg_match = re.search(r"#egg=([a-zA-Z0-9_-]+)", line)
            if egg_match:
                return egg_match.group(1)
        return None

    # Skip constraint file references
    if line.startswith("-c ") or line.startswith("--constraint"):
        return None

    # Skip requirement file references
    if line.startswith("-r ") or line.startswith("--requirement"):
        return None

    # Handle URL-based requirements
    if "://" in line:
        egg_match = re.search(r"#egg=([a-zA-Z0-9_-]+)", line)
        if egg_match:
            return egg_match.group(1)
        return None

    # Extract package name (everything before version specifier, extras, or markers)
    # Package names can contain letters, numbers, hyphens, underscores, and periods
    match = re.match(r"^([a-zA-Z0-9][-a-zA-Z0-9._]*)", line)
    if match:
        return match.group(1)

    return None


def parse_requirements_in(path: Path) -> dict[str, PackageInfo]:
    """
    Parse a .in requirements file and return a dict of normalized package names.

    Returns a dict mapping normalized package name to PackageInfo.
    """
    packages: dict[str, PackageInfo] = {}

    if not path.exists():
        return packages

    content = path.read_text()
    for line_num, line in enumerate(content.splitlines(), start=1):
        pkg_name = parse_package_name(line)
        if pkg_name:
            normalized = normalize_package_name(pkg_name)
            packages[normalized] = PackageInfo(
                name=pkg_name,
                line_number=line_num,
                raw_line=line.strip(),
            )

    return packages


def parse_requirements_txt(path: Path) -> dict[str, tuple[PackageInfo, set[str]]]:
    """
    Parse a .txt requirements file and return packages with their sources.

    For pip-compile/uv generated .txt files, packages have comments indicating
    their source, like:
        requests==2.28.0    # via -r base.in
        urllib3==1.26.0     # via requests

    Returns a dict mapping normalized package name to (PackageInfo, set of sources).
    Sources are extracted from "# via ..." comments.
    """
    packages: dict[str, tuple[PackageInfo, set[str]]] = {}

    if not path.exists():
        return packages

    content = path.read_text()
    for line_num, line in enumerate(content.splitlines(), start=1):
        # Skip pure comment lines
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # Split line into requirement and comment
        comment_match = re.search(r"\s+#\s*(.*)$", line)
        req_part = re.sub(r"\s+#.*$", "", line).strip()

        pkg_name = parse_package_name(req_part)
        if not pkg_name:
            continue

        # Extract sources from "via" comment
        sources: set[str] = set()
        if comment_match:
            comment = comment_match.group(1)
            # Look for "via" followed by source names
            via_match = re.search(r"via\s+(.+)", comment, re.IGNORECASE)
            if via_match:
                via_content = via_match.group(1)
                # Sources can be comma-separated or just a single source
                # e.g., "via requests, urllib3" or "via -r base.in"
                for source in re.split(r",\s*", via_content):
                    source = source.strip()
                    if source:
                        sources.add(source)

        normalized = normalize_package_name(pkg_name)
        packages[normalized] = (
            PackageInfo(
                name=pkg_name,
                line_number=line_num,
                raw_line=stripped,
            ),
            sources,
        )

    return packages


def get_direct_dependencies_from_txt(
    txt_packages: dict[str, tuple[PackageInfo, set[str]]],
    in_filename: str,
) -> set[str]:
    """
    Get packages from .txt that claim to be direct dependencies from the .in file.

    Looks for packages with "via -r <filename>" in their sources.
    """
    direct_deps: set[str] = set()

    # Patterns to match references to the .in file
    # Could be "via -r base.in" or "via -r requirements/base.in"
    in_patterns = [
        f"-r {in_filename}",
        f"-r ./{in_filename}",
        in_filename,  # Sometimes just the filename without -r
    ]

    for pkg_name, (info, sources) in txt_packages.items():
        for source in sources:
            for pattern in in_patterns:
                if pattern in source:
                    direct_deps.add(pkg_name)
                    break

    return direct_deps


def find_requirements_pairs(
    requirements_dir: Path,
) -> list[tuple[Path, Path]]:
    """
    Find matching .in and .txt file pairs in a requirements directory.

    Returns list of (in_path, txt_path) tuples.
    """
    pairs: list[tuple[Path, Path]] = []

    if not requirements_dir.exists():
        return pairs

    for in_file in requirements_dir.glob("*.in"):
        txt_file = in_file.with_suffix(".txt")
        if txt_file.exists():
            pairs.append((in_file, txt_file))

    return pairs


def check_requirements_consistency(
    in_path: Path,
    txt_path: Path,
) -> list[str]:
    """
    Check consistency between a .in file and its corresponding .txt file.

    Returns a list of error messages. Empty list means files are consistent.
    """
    errors: list[str] = []

    in_packages = parse_requirements_in(in_path)
    txt_packages = parse_requirements_txt(txt_path)

    in_filename = in_path.name

    # Check 1: Packages marked as direct deps in .txt should be in .in
    # This catches the case where a package was removed from .in but .txt
    # wasn't regenerated
    direct_in_txt = get_direct_dependencies_from_txt(txt_packages, in_filename)

    for pkg_normalized in direct_in_txt:
        if pkg_normalized not in in_packages:
            pkg_info, sources = txt_packages[pkg_normalized]
            errors.append(
                f"{txt_path.name}:{pkg_info.line_number}: "
                f"Package '{pkg_info.name}' is marked as coming from {in_filename} "
                f"(via comment: {sources}) but is not in {in_filename}. "
                f"The .txt file may need to be regenerated."
            )

    # Check 2: All packages in .in should have a corresponding entry in .txt
    # This catches the case where a package was added to .in but .txt wasn't
    # regenerated
    for pkg_normalized, pkg_info in in_packages.items():
        if pkg_normalized not in txt_packages:
            errors.append(
                f"{in_path.name}:{pkg_info.line_number}: "
                f"Package '{pkg_info.name}' is in {in_filename} "
                f"but not in {txt_path.name}. "
                f"The .txt file may need to be regenerated."
            )

    return errors


def find_all_requirements_dirs(root: Path) -> list[Path]:
    """
    Find all directories named 'requirements' under the root.
    """
    return list(root.rglob("requirements"))


class TestRequirementsConsistency:
    """Tests for requirements .in and .txt file consistency."""

    def test_in_txt_consistency(self, tmp_path: Path) -> None:
        """
        Test that .in and .txt files are consistent.

        This is a sample test structure. In a real monorepo, you would
        replace REPO_ROOT with the actual repository root path.
        """
        # Example: Create test files to demonstrate the check
        requirements_dir = tmp_path / "requirements"
        requirements_dir.mkdir()

        # Create a consistent pair
        in_file = requirements_dir / "base.in"
        txt_file = requirements_dir / "base.txt"

        in_file.write_text("requests>=2.0\ndjango>=3.0\n")
        txt_file.write_text(
            "# This file is autogenerated\n"
            "django==3.2.0    # via -r base.in\n"
            "requests==2.28.0    # via -r base.in\n"
            "urllib3==1.26.0    # via requests\n"
        )

        pairs = find_requirements_pairs(requirements_dir)
        assert len(pairs) == 1

        errors = check_requirements_consistency(in_file, txt_file)
        assert errors == [], f"Unexpected errors: {errors}"

    def test_detects_removed_package(self, tmp_path: Path) -> None:
        """
        Test that we detect when a package is removed from .in but still in .txt.
        """
        requirements_dir = tmp_path / "requirements"
        requirements_dir.mkdir()

        in_file = requirements_dir / "base.in"
        txt_file = requirements_dir / "base.txt"

        # Package 'flask' was removed from .in but still in .txt
        in_file.write_text("requests>=2.0\n")
        txt_file.write_text(
            "flask==2.0.0    # via -r base.in\n"
            "requests==2.28.0    # via -r base.in\n"
        )

        errors = check_requirements_consistency(in_file, txt_file)
        assert len(errors) == 1
        assert "flask" in errors[0].lower()
        assert "not in base.in" in errors[0]

    def test_detects_added_package(self, tmp_path: Path) -> None:
        """
        Test that we detect when a package is added to .in but not in .txt.
        """
        requirements_dir = tmp_path / "requirements"
        requirements_dir.mkdir()

        in_file = requirements_dir / "base.in"
        txt_file = requirements_dir / "base.txt"

        # Package 'newpackage' was added to .in but .txt not regenerated
        in_file.write_text("requests>=2.0\nnewpackage>=1.0\n")
        txt_file.write_text(
            "requests==2.28.0    # via -r base.in\n"
        )

        errors = check_requirements_consistency(in_file, txt_file)
        assert len(errors) == 1
        assert "newpackage" in errors[0].lower()
        assert "not in base.txt" in errors[0]

    def test_transitive_deps_ok(self, tmp_path: Path) -> None:
        """
        Test that transitive dependencies (not from .in) don't cause errors.
        """
        requirements_dir = tmp_path / "requirements"
        requirements_dir.mkdir()

        in_file = requirements_dir / "base.in"
        txt_file = requirements_dir / "base.txt"

        # urllib3 is a transitive dep (via requests), not directly from .in
        in_file.write_text("requests>=2.0\n")
        txt_file.write_text(
            "certifi==2023.0.0    # via requests\n"
            "charset-normalizer==3.0.0    # via requests\n"
            "idna==3.4    # via requests\n"
            "requests==2.28.0    # via -r base.in\n"
            "urllib3==1.26.0    # via requests\n"
        )

        errors = check_requirements_consistency(in_file, txt_file)
        assert errors == [], f"Unexpected errors: {errors}"


def validate_requirements_directory(requirements_dir: Path) -> list[str]:
    """
    Validate all .in/.txt pairs in a requirements directory.

    This is the main entry point for CI integration.
    Returns a list of all errors found.
    """
    all_errors: list[str] = []

    pairs = find_requirements_pairs(requirements_dir)
    for in_path, txt_path in pairs:
        errors = check_requirements_consistency(in_path, txt_path)
        all_errors.extend(errors)

    return all_errors


if __name__ == "__main__":
    # When run directly, check all requirements directories in the repo
    import sys

    # Default to current directory if no argument provided
    if len(sys.argv) > 1:
        root = Path(sys.argv[1])
    else:
        root = Path.cwd()

    all_errors: list[str] = []

    for req_dir in find_all_requirements_dirs(root):
        errors = validate_requirements_directory(req_dir)
        all_errors.extend(errors)

    if all_errors:
        print("Requirements consistency errors found:")
        for error in all_errors:
            print(f"  {error}")
        sys.exit(1)
    else:
        print("All requirements files are consistent.")
        sys.exit(0)
