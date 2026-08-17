"""Shared helper for writing recipe ``.md`` files in a human-friendly layout.

Both the importer (``tools/importer/scrape.py``) and the editor
(``tools/editor/save_recipe.py``) use this so recipes keep one consistent
structure:

    ---
    date_added: 2026-02-01
    layout: recipe
    title: "One-Pot Salmon, Spinach and Lentil Salad"
    image: one-pot-salmon-bowl.jpg
    original_url: https://...

    # ---
    tags:
    - dinner
    description: ...
    yield: 4 servings
    prep_time: ...
    cook_time: ...
    total_time: ...
    ingredients:
    - ...

    # ---
    directions:
    - ...

    # ---
    notes:
    - ...
    ---

Why ``# ---`` instead of a literal ``---``?  Jekyll ends front matter at the
first line that is exactly ``---`` (the closing delimiter).  A bare ``---``
inside the block would truncate the front matter and push every later field
into the page body.  ``# ---`` is a YAML comment, so it renders the same
visual divider while leaving the front matter intact.
"""
from __future__ import annotations

import re
from typing import Dict, Mapping

import yaml

SECTION = "# ---"

# Field order for each visual section. Unrecognised keys are appended to the
# meta section (before ingredients) so they are never dropped.
HEADER_KEYS = [
    "date_added",
    "layout",
    "title",
    "image",
    "imagecredit",
    "author",
    "source",
    "published",
    "original_url",
]
META_KEYS = [
    "tags",
    "description",
    "yield",
    "yields",
    "servings",
    "prep_time",
    "cook_time",
    "total_time",
]
INGREDIENT_KEYS = ["ingredients"]
DIRECTION_KEYS = ["directions"]
NOTE_KEYS = ["notes"]


def _dump_section(data: Mapping) -> str:
    """Serialise one section as YAML, returning "" for an empty section."""
    if not data:
        return ""
    text = yaml.dump(
        data,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=120,
    )
    # An empty original_url is friendlier than the literal `null` YAML would emit.
    text = re.sub(r"^original_url: null[ \t]*$", "original_url:", text, flags=re.MULTILINE)
    return text.rstrip("\n")


def _pick(fm: Mapping, keys) -> Dict:
    return {key: fm[key] for key in keys if key in fm}


def render_recipe_markdown(fm: Mapping, body: str = "") -> str:
    """Render front matter (dict) plus optional markdown body to a .md file."""
    header = _pick(fm, HEADER_KEYS)
    meta = _pick(fm, META_KEYS)
    ingredients = _pick(fm, INGREDIENT_KEYS)
    directions = _pick(fm, DIRECTION_KEYS)
    notes = _pick(fm, NOTE_KEYS)

    # Keep any keys not listed above so nothing is ever dropped.
    known = set(HEADER_KEYS + META_KEYS + INGREDIENT_KEYS + DIRECTION_KEYS + NOTE_KEYS)
    for key, value in fm.items():
        if key not in known:
            meta[key] = value

    blocks = ["---"]
    if header:
        blocks.append(_dump_section(header))
    blocks.append(SECTION)
    if meta or ingredients:
        if meta:
            blocks.append(_dump_section(meta))
        if ingredients:
            blocks.append(_dump_section(ingredients))
    if directions:
        blocks.append(SECTION)
        blocks.append(_dump_section(directions))
    if notes:
        blocks.append(SECTION)
        blocks.append(_dump_section(notes))
    blocks.append("---")

    out = "\n".join(block for block in blocks if block != "") + "\n"

    cleaned_body = body.strip("\n") if body else ""
    if cleaned_body:
        out += "\n" + cleaned_body + "\n"
    return out
