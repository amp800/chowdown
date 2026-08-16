"""
Standalone photo recipe importer — called by the "Import Recipe from Photo"
GitHub Actions workflow.

The browser compresses a phone photo of a printed cookbook/magazine page,
uploads it to images/ via the GitHub Contents API, then dispatches this
workflow. This script sends the photo to the Gemini vision API (free tier,
no credit card), asks it to read the recipe and return it as structured
JSON, writes _recipes/<slug>.md, and renames the photo to match the slug.

Usage:
  python tools/importer/photo_import.py --image photo-import-123.jpg --tags "chicken quick"

Requires the GEMINI_API_KEY env var (GitHub Actions secret).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # tools/importer/photo_import.py → repo root
sys.path.insert(0, str(REPO_ROOT / "tools"))
from recipe_frontmatter import render_recipe_markdown  # noqa: E402

# Vision-capable Gemini models, newest first. All are available on the free
# tier; we fall back to the older one if the newest has been renamed.
GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash"]
API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

PROMPT = """You are a recipe digitization assistant. A photo of a printed cookbook or magazine recipe page is attached. Read the recipe carefully and return ONLY valid JSON with exactly these fields:
- "title": the recipe name (string)
- "description": a short one-to-two sentence "what/why" description of the dish, written like a friendly cookbook intro (string)
- "yield": how many servings it makes, e.g. "4 servings" (string; use null if not stated)
- "prep_time": prep time as printed, e.g. "15 minutes" (string; use null if not stated)
- "cook_time": cook time as printed (string; use null if not stated)
- "total_time": total time as printed (string; use null if not stated)
- "tags": 2 to 4 short lowercase keywords (array of strings)
- "ingredients": each ingredient exactly as printed, one per list item, keeping quantities and units as written (array of strings)
- "directions": each numbered step as its own list item, cleaned of leading numbers and formatting but keeping all detail (array of strings)

Rules:
- Transcribe the recipe faithfully. Do not invent ingredients or steps, and do not add metric conversions or substitutions.
- Ignore page furniture: page numbers, cookbook title, chapter headings, headers, footers, photo captions, decorative text, website/blog text.
- Keep sub-sections of the recipe (such as "For the sauce:") as separate list items within their section.
- Return raw JSON only — no markdown fences, no commentary."""


def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[-\s]+", "-", text).strip("-")
    return text


def mime_for(path: Path) -> str:
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(path.suffix.lower(), "image/jpeg")


def call_gemini(image_path: Path, api_key: str) -> dict:
    """Send the photo to Gemini vision and return the parsed JSON recipe."""
    image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": PROMPT},
                    {"inline_data": {"mime_type": mime_for(image_path), "data": image_b64}},
                ]
            }
        ],
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0.2},
    }
    last_error = None
    for model in GEMINI_MODELS:
        try:
            resp = requests.post(
                API_URL.format(model=model), params={"key": api_key}, json=payload, timeout=120
            )
            resp.raise_for_status()
            data = resp.json()
            try:
                text = data["candidates"][0]["content"]["parts"][0]["text"]
            except (KeyError, IndexError, TypeError):
                raise RuntimeError(f"Gemini returned no text: {json.dumps(data)[:500]}")
            text = text.strip()
            # Defensive: strip markdown code fences if the model adds them.
            if text.startswith("```"):
                text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
                text = re.sub(r"\s*```$", "", text).strip()
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                raise RuntimeError("Gemini response was not a JSON object")
            return parsed
        except requests.exceptions.HTTPError as e:
            last_error = e
            status = e.response.status_code if e.response is not None else None
            if status in (400, 404, 429):
                print(f"Model {model} failed (HTTP {status}); trying next model…", file=sys.stderr)
                continue
            raise
        except (requests.RequestException, ValueError, RuntimeError) as e:
            last_error = e
            print(f"Model {model} failed: {e}", file=sys.stderr)
            continue
    raise RuntimeError(f"All Gemini models failed. Last error: {last_error}")


def as_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.splitlines() if v.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def unique_slug(title: str) -> str:
    """Slug the title, appending -2, -3… if the recipe file already exists."""
    slug = slugify(title) or "recipe"
    recipes_dir = REPO_ROOT / "_recipes"
    candidate, n = slug, 2
    while (recipes_dir / f"{candidate}.md").exists():
        candidate = f"{slug}-{n}"
        n += 1
    return candidate


def main():
    parser = argparse.ArgumentParser(description="Import a recipe from a photo via Gemini vision")
    parser.add_argument("--image", required=True, help="photo filename inside images/")
    parser.add_argument("--tags", default="")
    args = parser.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("ERROR: GEMINI_API_KEY env var is not set", file=sys.stderr)
        sys.exit(1)

    image_arg = args.image.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.(jpg|jpeg|png|webp)", image_arg):
        print(f"ERROR: invalid image filename: {args.image}", file=sys.stderr)
        sys.exit(1)
    image_path = REPO_ROOT / "images" / image_arg
    if not image_path.is_file():
        print(f"ERROR: photo not found at images/{image_arg}", file=sys.stderr)
        sys.exit(1)
    if image_path.stat().st_size > 19 * 1024 * 1024:
        print("ERROR: photo is too large (Gemini accepts up to 20 MB)", file=sys.stderr)
        sys.exit(1)

    print(f"Reading photo images/{image_arg} with Gemini vision…")
    try:
        data = call_gemini(image_path, api_key)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    title = str(data.get("title") or "").strip() or "Untitled Recipe"
    print(f"Read: {title}")

    ingredients = as_list(data.get("ingredients"))
    directions = as_list(data.get("directions"))
    if not ingredients or not directions:
        print("ERROR: Gemini did not return ingredients/directions; nothing to import", file=sys.stderr)
        sys.exit(1)

    slug = unique_slug(title)

    user_tags = [t.strip() for t in args.tags.split() if t.strip()]
    gemini_tags = [str(t).strip().lower() for t in as_list(data.get("tags"))]
    all_tags = sorted(set(gemini_tags + user_tags))

    # Rename the uploaded photo to match the recipe slug (the commit step
    # picks up the rename together with the new .md file).
    final_image = f"{slug}.jpg"
    final_image_path = REPO_ROOT / "images" / final_image
    if final_image_path != image_path:
        if final_image_path.exists():
            final_image_path.unlink()
        image_path.rename(final_image_path)
        print(f"Photo renamed: images/{final_image}")

    fm = {
        "date_added": date.today(),
        "layout": "recipe",
        "title": title,
        "image": final_image,
        "original_url": None,
        "tags": all_tags,
        "ingredients": ingredients,
        "directions": directions,
    }
    for key in ("description", "yield", "prep_time", "cook_time", "total_time"):
        value = str(data.get(key) or "").strip()
        if value:
            fm[key] = value

    out_path = REPO_ROOT / "_recipes" / f"{slug}.md"
    out_path.write_text(render_recipe_markdown(fm), encoding="utf-8")
    print(f"Written: _recipes/{slug}.md")


if __name__ == "__main__":
    main()
