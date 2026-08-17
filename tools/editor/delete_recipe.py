"""
Delete a recipe file and its hero image — called by the "Delete Recipe"
GitHub Actions workflow.

Usage:
  python tools/editor/delete_recipe.py --path "_recipes/one-pot-salmon-bowl.md"

The recipe's `image` field (just a filename, e.g. `one-pot-salmon-bowl.webp`)
is read from the front matter and the matching file under `images/` is removed
too, so no orphaned hero images are left behind.
"""
import argparse
import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def sanitize_path(rel: str) -> Path:
    p = Path(rel.strip())
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"invalid path: {rel}")
    if len(p.parts) != 2 or p.parts[0] != "_recipes" or p.suffix != ".md":
        raise ValueError(f"path must be _recipes/<name>.md: {rel}")
    return p


def read_image_field(path: Path):
    """Return the front matter `image` value (or None) without writing anything."""
    raw = path.read_text(encoding="utf-8")
    if raw.startswith("---"):
        parts = re.split(r"(?m)^---[ \t]*$", raw, maxsplit=2)
        fm_raw = parts[1] if len(parts) > 1 else ""
    else:
        fm_raw = ""
    try:
        fm = yaml.safe_load(fm_raw) if fm_raw.strip() else {}
    except Exception:
        fm = {}
    if not isinstance(fm, dict):
        return None
    image = fm.get("image")
    return str(image).strip() if image else None


def main():
    parser = argparse.ArgumentParser(description="Delete a recipe and its hero image")
    parser.add_argument("--path", required=True)
    args = parser.parse_args()

    try:
        rel_path = sanitize_path(args.path)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    recipe_path = REPO_ROOT / rel_path
    if not recipe_path.exists():
        print(f"ERROR: file not found: {args.path}", file=sys.stderr)
        sys.exit(1)

    image = read_image_field(recipe_path)

    recipe_path.unlink()
    print(f"Deleted: {rel_path}")

    if image:
        img_root = (REPO_ROOT / "images").resolve()
        img_path = (REPO_ROOT / "images" / image).resolve()
        try:
            img_path.relative_to(img_root)
            safe = True
        except ValueError:
            safe = False
        if safe and img_path.is_file():
            img_path.unlink()
            print(f"Deleted: images/{image}")
        elif safe:
            print(f"Note: image not found: images/{image}")
        else:
            print(f"Note: skipping unsafe image reference: {image}")
    else:
        print("Note: no image field in front matter")


if __name__ == "__main__":
    main()
