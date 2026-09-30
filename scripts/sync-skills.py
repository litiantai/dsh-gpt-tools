"""Synchronize distributable skills from their canonical sources."""

from pathlib import Path
import shutil

root = Path(__file__).resolve().parents[1]
sources = [root / "dsh-gpt-supervisor", *sorted((root / "skills").glob("*/"))]
for source in sources:
    if not (source / "SKILL.md").is_file():
        continue
    target = root / ".agent/skills" / source.name
    for path in source.rglob("*"):
        if (
            path.is_file()
            and "__pycache__" not in path.parts
            and path.name != ".DS_Store"
        ):
            dest = target / path.relative_to(source)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
