"""
Standalone recipe scraper — called by GitHub Actions import-recipe workflow.
Usage:
  python tools/importer/scrape.py \
    --url "https://..." \
    --tags "chicken quick" \
    --season "fall winter" \
    --difficulty medium \
    --kid_friendly false
Writes _recipes/<slug>.md and images/<slug>.<ext> relative to the repo root.
"""
import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urlparse, urljoin

import requests
import yaml

try:
    from recipe_scrapers import scrape_html
except ImportError:
    print("recipe-scrapers not installed", file=sys.stderr)
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # tools/importer/scrape.py → repo root


# ---------------------------------------------------------------------------
# Helpers (mirror of app/main.py so the Actions workflow needs no FastAPI)
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[-\s]+", "-", text).strip("-")
    return text


def format_ingredients(ingredients) -> list:
    if isinstance(ingredients, str):
        ingredients = ingredients.split("\n")
    elif not isinstance(ingredients, list):
        ingredients = []
    return [str(i).strip() for i in ingredients if str(i).strip()]


def format_instructions(instructions) -> list:
    if isinstance(instructions, str):
        instructions = instructions.split("\n")
    elif not isinstance(instructions, list):
        instructions = []
    cleaned = []
    for step in instructions:
        step = str(step).strip()
        if not step:
            continue
        step = re.sub(r"<[^>]+>", "", step)
        step = re.sub(r"^(\d+[\.\)]\s+|-\s+|\*\s+)", "", step)
        if step:
            cleaned.append(step)
    return cleaned


def guess_tags(title: str) -> list:
    title_lower = title.lower()
    tag_map = {
        "vegetarian": ["vegetarian"], "vegan": ["vegan"],
        "gluten-free": ["gluten-free"], "chicken": ["chicken"],
        "beef": ["beef"], "pork": ["pork"],
        "fish": ["fish", "seafood"], "salmon": ["fish", "seafood"],
        "shrimp": ["seafood"], "pasta": ["pasta"],
        "soup": ["soup"], "stew": ["stew"], "salad": ["salad"],
        "dessert": ["dessert"], "cake": ["dessert", "baking"],
        "cookie": ["dessert", "baking"], "bread": ["bread", "baking"],
        "pizza": ["pizza"], "taco": ["mexican"], "curry": ["curry"],
    }
    tags: set = set()
    for keyword, tag_list in tag_map.items():
        if keyword in title_lower:
            tags.update(tag_list)
    return sorted(list(tags))


def download_image(image_url: str, source_url: str, save_path: Path) -> bool:
    if not image_url:
        return False
    if image_url.startswith("/"):
        parsed = urlparse(source_url)
        image_url = f"{parsed.scheme}://{parsed.netloc}{image_url}"
    elif not image_url.startswith("http"):
        image_url = urljoin(source_url, image_url)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Referer": source_url,
    }
    try:
        resp = requests.get(image_url, headers=headers, timeout=30, stream=True)
        resp.raise_for_status()
        save_path.parent.mkdir(parents=True, exist_ok=True)
        with open(save_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        return True
    except Exception as e:
        print(f"Image download failed: {e}", file=sys.stderr)
        return False


def fetch_recipe(url: str) -> dict:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36"
    }
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    scraper = scrape_html(response.text, url, supported_only=False)
    data: dict = {
        "title": scraper.title(),
        "ingredients": scraper.ingredients(),
        "instructions": scraper.instructions(),
        "image_url": "",
    }
    for method_name, key_name in [
        ("yields", "yield"), ("total_time", "total_time"),
        ("prep_time", "prep_time"), ("cook_time", "cook_time"),
        ("category", "category"), ("cuisine", "cuisine"),
    ]:
        try:
            val = getattr(scraper, method_name)()
            if val:
                data[key_name] = str(val)
        except Exception:
            pass
    try:
        data["image_url"] = scraper.image()
    except Exception:
        pass
    return data


def build_markdown(data: dict, image_ref: str, user_tags: list,
                   season: str, difficulty: str, kid_friendly: bool) -> str:
    auto_tags = guess_tags(data["title"])
    all_tags = sorted(list(set(auto_tags + (user_tags or []))))
    season_list = (
        [s.strip() for s in season.replace(",", " ").split() if s.strip()]
        if season and season.strip().lower() != "all"
        else ["all"]
    )
    from datetime import date
    fm = {
        "layout": "recipe",
        "title": data.get("title", "Untitled Recipe"),
        "image": image_ref,
        "tags": all_tags,
        "ingredients": format_ingredients(data.get("ingredients", [])),
        "directions": format_instructions(data.get("instructions", [])),
        "date_added": date.today(),
        "season": season_list,
        "difficulty": difficulty,
        "kid_friendly": kid_friendly,
    }
    for key in ["yield", "prep_time", "cook_time", "total_time", "category", "cuisine"]:
        if key in data and data[key]:
            fm[key] = data[key]
    yaml_content = yaml.dump(fm, default_flow_style=False, sort_keys=False, allow_unicode=True, width=120)
    return f"---\n{yaml_content}---\n"


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Scrape a recipe URL and write markdown")
    parser.add_argument("--url", required=True)
    parser.add_argument("--tags", default="")
    parser.add_argument("--season", default="all")
    parser.add_argument("--difficulty", default="easy")
    parser.add_argument("--kid_friendly", default="false")
    args = parser.parse_args()

    user_tags = [t.strip() for t in args.tags.split() if t.strip()]
    kid_bool = args.kid_friendly.lower() in ("true", "1", "yes", "on")
    difficulty_clean = (
        args.difficulty.strip().lower()
        if args.difficulty.strip().lower() in ("easy", "medium", "hard")
        else "easy"
    )

    print(f"Fetching recipe from {args.url}…")
    try:
        data = fetch_recipe(args.url)
    except Exception as e:
        print(f"ERROR: Failed to fetch recipe: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Scraped: {data['title']}")
    slug = slugify(data["title"])
    filename = f"{slug}.md"

    images_dir = REPO_ROOT / "images"
    image_filename = f"{slug}.jpg"
    image_save_path = images_dir / image_filename
    image_ref = image_filename

    if data.get("image_url"):
        downloaded = download_image(data["image_url"], args.url, image_save_path)
        if downloaded:
            actual_ext = image_save_path.suffix
            if actual_ext and actual_ext != ".jpg":
                new_name = f"{slug}{actual_ext}"
                image_save_path.rename(images_dir / new_name)
                image_ref = new_name
            print(f"Image saved: {image_ref}")
        else:
            image_ref = ""
            if image_save_path.exists():
                image_save_path.unlink()
            print("Image download failed — recipe will have no image")

    md_content = build_markdown(
        data, image_ref, user_tags,
        season=args.season,
        difficulty=difficulty_clean, kid_friendly=kid_bool,
    )
    recipes_dir = REPO_ROOT / "_recipes"
    recipes_dir.mkdir(exist_ok=True)
    out_path = recipes_dir / filename
    out_path.write_text(md_content, encoding="utf-8")
    print(f"Written: _recipes/{filename}")


if __name__ == "__main__":
    main()
