"""Convert a Mealie zip export into chowdown recipe files.

Usage:
  python tools/mealie_import.py --export mealie-export/extracted/recipes [--dry-run]

The Mealie export is a folder per recipe, each containing ``<slug>.json`` and
an ``images/`` folder holding the hero image as ``original.<ext>`` (plus
``min-original``/``tiny-original`` variants) and an ``assets/`` folder of
instruction-embedded images.

For every recipe this script:
  * reads the JSON,
  * maps Mealie fields to the chowdown front matter (via
    ``recipe_frontmatter.render_recipe_markdown`` so the layout matches the
    URL importer and the in-page editor exactly),
  * copies only the hero image to ``images/<slug>.<ext>`` (instruction images
    are skipped),
  * writes ``_recipes/<slug>.md``.

The existing ``_recipes/*.md`` and ``images/*`` are removed first — the site's
dummy recipes are replaced by this import. Use ``--dry-run`` to preview
without touching anything.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent  # tools/mealie_import.py -> repo root
sys.path.insert(0, str(REPO_ROOT / "tools"))
from recipe_frontmatter import render_recipe_markdown  # noqa: E402

# Unicode fraction glyphs -> plain "n/m" (the servings scaler only understands
# ASCII digits, so converting keeps adjustable servings working).
UNICODE_FRACTIONS = {
    "\u00bd": "1/2", "\u2153": "1/3", "\u2154": "2/3", "\u00bc": "1/4",
    "\u00be": "3/4", "\u2155": "1/5", "\u2156": "2/5", "\u2157": "3/5",
    "\u2158": "4/5", "\u2159": "1/6", "\u215a": "5/6", "\u215b": "1/8",
    "\u215c": "3/8", "\u215d": "5/8", "\u215e": "7/8",
}
SUPERSCRIPT = {"\u2070": "0", "\u00b9": "1", "\u00b2": "2", "\u00b3": "3",
               "\u2074": "4", "\u2075": "5", "\u2076": "6", "\u2077": "7",
               "\u2078": "8", "\u2079": "9"}
SUBSCRIPT = {"\u2080": "0", "\u2081": "1", "\u2082": "2", "\u2083": "3",
             "\u2084": "4", "\u2085": "5", "\u2086": "6", "\u2087": "7",
             "\u2088": "8", "\u2089": "9"}


def clean_text(text) -> str:
    """Strip HTML (embedded <img> tags etc.) and collapse whitespace."""
    if text is None:
        return ""
    text = str(text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def normalize_fractions(text: str) -> str:
    """Turn unicode fraction glyphs (including superscript/subscript forms
    like ``1/2``, ``\u00b9/\u2082``) into plain ``n/m`` text."""
    def repl(m) -> str:
        num = "".join(SUPERSCRIPT.get(c, c) for c in m.group(1))
        den = "".join(SUBSCRIPT.get(c, c) for c in m.group(2))
        return f"{num}/{den}"
    text = re.sub(r"([\u2070\u00b9\u00b2\u00b3\u2074-\u2079]+)/"
                  r"([\u2080-\u2089]+)", repl, text)
    for glyph, plain in UNICODE_FRACTIONS.items():
        text = text.replace(glyph, plain)
    return text


def parse_minutes(value):
    """Parse a Mealie time string to minutes.

    Handles ``'15 minutes'``, ``'1 Hour 15 Minutes'``, ``'30 mins'``,
    ``'110 mins'``, ISO ``'PT1H30M'`` and ``'none'``.
    """
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s or s in ("none", "n/a", "0", "0 minutes"):
        return None
    m = re.match(r"^pt(?:(\d+)h)?(?:(\d+)m)?$", s)
    if m:
        hours = int(m.group(1) or 0)
        minutes = int(m.group(2) or 0)
        return hours * 60 + minutes if (hours or minutes) else None
    total = 0.0
    found = False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|minutes?|mins?)", s):
        found = True
        if unit.startswith(("hour", "hr")):
            total += float(num) * 60
        else:
            total += float(num)
    return int(round(total)) if found and total else None


def render_ingredient(ing) -> str:
    """Return a human-readable ingredient line.

    Prefers Mealie's ``original_text`` (plain fractions, scaler-friendly),
    falls back to ``display``, then composes from quantity/unit/food/note.
    """
    if not isinstance(ing, dict):
        return clean_text(ing)
    text = str(ing.get("original_text") or "").strip()
    if not text:
        text = str(ing.get("display") or "").strip()
    if not text:
        parts = []
        qty = ing.get("quantity")
        if qty not in (None, "", 0):
            parts.append(str(qty))
        unit = ing.get("unit")
        if isinstance(unit, dict) and unit.get("name"):
            parts.append(str(unit["name"]))
        food = ing.get("food")
        if isinstance(food, dict) and food.get("name"):
            parts.append(str(food["name"]))
        note = str(ing.get("note") or "").strip()
        if note:
            parts.append(f"({note})")
        text = " ".join(parts)
    text = clean_text(text)
    # Tidy Mealie's "(, ...)" and "( ...)" artifacts, then normalise any
    # unicode fractions.
    text = re.sub(r"\(\s*,", "(", text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+\)", ")", text)
    text = re.sub(r"\s+", " ", text).strip()
    return normalize_fractions(text)


def render_directions(instructions) -> list:
    """Flatten Mealie instruction objects into plain steps.

    Strips embedded <img> tags, skips image-only steps, and prefixes any
    non-empty step title (e.g. ``To Cook``) onto the step text.
    """
    out = []
    for ins in instructions or []:
        if not isinstance(ins, dict):
            text = clean_text(ins)
            if text:
                out.append(text)
            continue
        title = clean_text(ins.get("title"))
        text = clean_text(ins.get("text"))
        if not title and not text:
            continue
        # The recipe page renders directions as plain text (not markdown), so
        # strip emphasis markers that Mealie imported from the source site.
        text = text.replace("**", "")
        if title and not text:
            out.append(title)
        elif title:
            out.append(f"{title}: {text}")
        else:
            out.append(text)
    return out


def render_notes(notes) -> list:
    """Flatten Mealie note objects into one bullet per paragraph.

    A new bullet starts at blank lines, at bullet-marker lines (``- ``,
    ``* ``, ``1. ``) and at short heading lines ending in ``:``. Everything
    else continues the current bullet, so word-wrapped lines stay together.
    A note title (e.g. ``Recipe Notes``) is prefixed onto the first bullet.
    """
    out = []
    for note in notes or []:
        if not isinstance(note, dict):
            text = clean_text(note)
            if text:
                out.append(text)
            continue
        title = clean_text(note.get("title"))
        body = str(note.get("text") or "")
        bullets = []
        current = ""
        for raw_line in body.split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            is_new = (
                bool(re.match(r"^[-*\u2022\u25e6]\s+", line))
                or bool(re.match(r"^\d+[.)]\s*", line))
                or (line.endswith(":") and len(line) < 80)
            )
            if is_new:
                if current:
                    bullets.append(current)
                current = line
            elif current:
                current += " " + line
            else:
                current = line
        if current:
            bullets.append(current)
        for bullet in bullets:
            if title:
                bullet = f"{title}: {bullet}"
                title = ""
            out.append(bullet)
    return out


def build_yield(d: dict) -> str:
    """Combine Mealie servings/quantity with the yield unit word."""
    servings = d.get("recipe_servings")
    quantity = d.get("recipe_yield_quantity")
    unit = str(d.get("recipe_yield") or "").strip()
    qty = None
    if quantity and float(quantity) > 0:
        qty = float(quantity)
    elif servings and float(servings) > 0:
        qty = float(servings)
    if qty is None:
        return unit
    qty_str = str(int(qty)) if qty == int(qty) else str(qty)
    if unit:
        return f"{qty_str} {unit}"
    return f"{qty_str} servings"


def build_tags(d: dict) -> list:
    """Merge Mealie categories + tags, lowercased (site style), deduped, sorted."""
    tags = []
    for cat in d.get("recipe_category") or []:
        name = (cat.get("name") if isinstance(cat, dict) else str(cat)).strip()
        if name:
            tags.append(name)
    for tag in d.get("tags") or []:
        name = (tag.get("name") if isinstance(tag, dict) else str(tag)).strip()
        if name:
            tags.append(name)
    seen, out = set(), []
    for tag in tags:
        lowered = tag.lower()
        if lowered not in seen:
            seen.add(lowered)
            out.append(lowered)
    return sorted(out)


def build_times(d: dict) -> dict:
    """Convert prep/cook/total times to minutes; derive total from prep+cook."""
    fm = {}
    prep = parse_minutes(d.get("prep_time"))
    cook = parse_minutes(d.get("cook_time"))
    total = parse_minutes(d.get("total_time"))
    if prep:
        fm["prep_time"] = str(prep)
    if cook:
        fm["cook_time"] = str(cook)
    if total:
        fm["total_time"] = str(total)
    elif prep and cook:
        fm["total_time"] = str(prep + cook)
    return fm


def build_front_matter(d: dict, slug: str, image_ref: str) -> dict:
    fm = {
        "layout": "recipe",
        "title": str(d.get("name") or slug).strip(),
    }
    if image_ref:
        fm["image"] = image_ref

    date_added = str(d.get("date_added") or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", date_added):
        fm["date_added"] = date_added[:10]

    org = str(d.get("org_url") or "").strip()
    if org:
        fm["original_url"] = org

    tags = build_tags(d)
    if tags:
        fm["tags"] = tags

    desc = clean_text(d.get("description"))
    if desc:
        fm["description"] = desc

    yield_value = build_yield(d)
    if yield_value:
        fm["yield"] = yield_value

    fm.update(build_times(d))

    ingredients = [render_ingredient(i) for i in d.get("recipe_ingredient") or []]
    fm["ingredients"] = [i for i in ingredients if i]

    directions = render_directions(d.get("recipe_instructions"))
    if directions:
        fm["directions"] = directions

    notes = render_notes(d.get("notes"))
    if notes:
        fm["notes"] = notes

    return fm


def find_hero_image(folder: Path):
    """Return the hero image path (prefer ``original.*``, then minified variants)."""
    img_dir = folder / "images"
    if not img_dir.is_dir():
        return None
    for prefix in ("original", "min-original", "tiny-original"):
        matches = sorted(img_dir.glob(prefix + ".*"))
        if matches:
            return matches[0]
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Import a Mealie zip export")
    parser.add_argument("--export", required=True,
                        help="Path to the extracted Mealie 'recipes' folder")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview the conversion without writing any files")
    args = parser.parse_args()

    export_dir = Path(args.export)
    if not export_dir.is_dir():
        print(f"ERROR: export folder not found: {export_dir}", file=sys.stderr)
        sys.exit(1)

    recipes_dir = REPO_ROOT / "_recipes"
    images_dir = REPO_ROOT / "images"

    recipes = []
    for folder in sorted(p for p in export_dir.iterdir() if p.is_dir()):
        json_path = folder / f"{folder.name}.json"
        if json_path.is_file():
            with open(json_path, encoding="utf-8") as f:
                recipes.append((folder.name, json.load(f)))
    if not recipes:
        print(f"ERROR: no recipe JSON files found under {export_dir}", file=sys.stderr)
        sys.exit(1)

    if not args.dry_run:
        removed_md = [f.unlink() for f in recipes_dir.glob("*.md")]
        removed_img = [f.unlink() for f in images_dir.glob("*") if f.is_file()]
        print(f"Removed {len(removed_md)} existing recipes and "
              f"{len(removed_img)} existing images")

    written = images_copied = no_image = 0
    for i, (slug, data) in enumerate(recipes, 1):
        hero = find_hero_image(export_dir / slug)
        image_ref = ""
        if hero:
            image_ref = f"{slug}{hero.suffix}"
            if not args.dry_run:
                shutil.copy2(hero, images_dir / image_ref)
                images_copied += 1
        else:
            no_image += 1

        fm = build_front_matter(data, slug, image_ref)
        markdown = render_recipe_markdown(fm)
        if not args.dry_run:
            (recipes_dir / f"{slug}.md").write_text(markdown, encoding="utf-8")
        written += 1
        print(f"[{i}/{len(recipes)}] {slug}")

    action = "would import" if args.dry_run else "imported"
    print(f"\nDone: {action} {written} recipes, {images_copied} images copied, "
          f"{no_image} without an image.")


if __name__ == "__main__":
    main()
