"""
Standalone recipe editor — called by the "Edit Recipe" GitHub Actions workflow.

Usage:
  python tools/editor/save_recipe.py \
    --path "_recipes/one-pot-salmon-bowl.md" \
    --payload "<base64-encoded JSON>"

The payload is a JSON object with the editable fields:

  title, description, yield, prep_time, cook_time, total_time,
  tags (list), ingredients (list), directions (list)

Fields that are NOT user-editable (image, original_url, date_added, layout,
the markdown body, and any unknown front matter keys) are preserved verbatim
from the existing file on disk.
"""
import argparse
import base64
import json
import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))
from recipe_frontmatter import render_recipe_markdown

# Front matter keys the editor manages. These are taken from the payload (or
# dropped when empty) rather than preserved from the existing file.
EDITABLE_KEYS = {
    "title", "description", "yield", "yields", "servings", "prep_time",
    "cook_time", "total_time", "tags", "ingredients", "directions",
}


def sanitize_path(rel: str) -> Path:
    p = Path(rel.strip())
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"invalid path: {rel}")
    if len(p.parts) != 2 or p.parts[0] != "_recipes" or p.suffix != ".md":
        raise ValueError(f"path must be _recipes/<name>.md: {rel}")
    return p


def split_existing(path: Path):
    """Return (front_matter_dict, raw_body) from an existing recipe file.

    Front matter is delimited by a ``---`` line (the same rule Jekyll uses), so
    the ``# ---`` section dividers inside the block are left untouched.
    """
    raw = path.read_text(encoding="utf-8")
    if raw.startswith("---"):
        parts = re.split(r"(?m)^---[ \t]*$", raw, maxsplit=2)
        fm_raw = parts[1] if len(parts) > 1 else ""
        body = parts[2] if len(parts) > 2 else ""
    else:
        fm_raw, body = "", raw
    existing = yaml.safe_load(fm_raw) if fm_raw.strip() else {}
    if not isinstance(existing, dict):
        existing = {}
    return existing, body


def as_list(value, split_lines=False):
    if value is None:
        return []
    if isinstance(value, str):
        value = value.splitlines() if split_lines else re.split(r"[\s,]+", value.strip())
    if not isinstance(value, (list, tuple)):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def build_front_matter(existing: dict, data: dict) -> dict:
    fm = {"layout": existing.get("layout") or "recipe"}

    title = str(data.get("title") or "").strip()
    if not title:
        raise ValueError("title is required")
    fm["title"] = title

    image = existing.get("image")
    if image:
        fm["image"] = str(image).strip()

    original_url = existing.get("original_url")
    if original_url:
        fm["original_url"] = str(original_url).strip()

    tags = as_list(data.get("tags"))
    if tags:
        fm["tags"] = tags

    description = str(data.get("description") or "").strip()
    if description:
        fm["description"] = description

    ingredients = as_list(data.get("ingredients"), split_lines=True)
    if ingredients:
        fm["ingredients"] = ingredients

    directions = as_list(data.get("directions"), split_lines=True)
    if directions:
        fm["directions"] = directions

    date_added = existing.get("date_added")
    if date_added:
        fm["date_added"] = date_added

    yield_value = str(data.get("yield") or "").strip()
    if not yield_value:
        # Older recipes may store servings under `yields` or `servings`.
        yield_value = str(
            existing.get("yield")
            or existing.get("yields")
            or existing.get("servings")
            or ""
        ).strip()
    if yield_value:
        fm["yield"] = yield_value

    for key in ("prep_time", "cook_time", "total_time"):
        value = str(data.get(key) or "").strip()
        if value:
            fm[key] = value

    # Preserve any front matter keys the editor does not manage.
    for key, value in existing.items():
        if key in EDITABLE_KEYS or key in fm:
            continue
        if value in (None, "", [], {}):
            continue
        fm[key] = value

    return fm


def build_markdown(fm: dict, body: str) -> str:
    return render_recipe_markdown(fm, body)


def main():
    parser = argparse.ArgumentParser(description="Save an edited recipe")
    parser.add_argument("--path", required=True)
    parser.add_argument("--payload", required=True)
    args = parser.parse_args()

    try:
        rel_path = sanitize_path(args.path)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    out_path = REPO_ROOT / rel_path
    if not out_path.exists():
        print(f"ERROR: file not found: {args.path}", file=sys.stderr)
        sys.exit(1)

    try:
        raw = base64.b64decode(args.payload).decode("utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("payload must be a JSON object")
    except Exception as e:
        print(f"ERROR: invalid payload: {e}", file=sys.stderr)
        sys.exit(1)

    existing, body = split_existing(out_path)
    try:
        fm = build_front_matter(existing, data)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    out_path.write_text(build_markdown(fm, body), encoding="utf-8")
    print(f"Written: {args.path}")


if __name__ == "__main__":
    main()
