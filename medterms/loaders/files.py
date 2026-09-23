"""Locate release files by name pattern in directories, zips and zips inside zips."""

import io
import re
import zipfile
from pathlib import Path


def find_files(paths: list[Path], patterns: dict[str, re.Pattern]) -> dict[str, tuple[str, bytes]]:
    """Return {kind: (filename, contents)} for the first file matching each pattern."""
    found: dict[str, tuple[str, bytes]] = {}

    def consider(name: str, read):
        base = name.rsplit("/", 1)[-1]
        for kind, pattern in patterns.items():
            if pattern.search(base) and kind not in found:
                found[kind] = (base, read())

    def visit_zip(src):
        with zipfile.ZipFile(src) as zf:
            for member in zf.namelist():
                if member.lower().endswith(".zip"):
                    visit_zip(io.BytesIO(zf.read(member)))
                else:
                    consider(member, lambda m=member: zf.read(m))

    for path in paths:
        for f in sorted(path.rglob("*")) if path.is_dir() else [path]:
            if f.suffix.lower() == ".zip":
                visit_zip(f)
            elif f.is_file():
                consider(f.name, f.read_bytes)
    return found


def decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252")
