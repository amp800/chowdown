"""
Standalone recipe scraper — called by GitHub Actions import-recipe workflow.
Usage:
  python tools/importer/scrape.py \
    --url "https://..." \
    --tags "chicken quick"
Writes _recipes/<slug>.md and images/<slug>.<ext> relative to the repo root.
"""
import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup

try:
    from recipe_scrapers import scrape_html
except ImportError:
    print("recipe-scrapers not installed", file=sys.stderr)
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # tools/importer/scrape.py → repo root
sys.path.insert(0, str(REPO_ROOT / "tools"))
from recipe_frontmatter import render_recipe_markdown


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


RECIPE_CARD_RE = re.compile(
    r"(wprm-recipe|tasty-recipes|recipe-card|recipecontainer|recipe-content|"
    r"recipe-instructions|recipe-ingredients|recipe__content|recipe__ingredients|"
    r"recipe__instructions|recipe__summary|recipe-meta|hrecipe|mv-create-card)",
    re.IGNORECASE,
)

BOILERPLATE_RE = re.compile(
    r"^(jump to recipe|skip to recipe|recipe video|print recipe|save recipe|"
    r"pin recipe|this post (may contain|contains) affiliate|advertisement|"
    r"disclaimer|shop the post|read more)",
    re.IGNORECASE,
)


def clean_text(text) -> str:
    if text is None:
        return ""
    text = str(text).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_first_paragraph(html: str) -> str:
    """Return the first meaningful intro paragraph of a recipe page (the
    'what/why' blurb that usually appears before the ingredients list)."""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return ""
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "form"]):
        tag.decompose()
    for p in soup.find_all("p"):
        # Skip paragraphs inside nav/header/footer/aside/form or inside the
        # recipe card (where the ingredients/directions live).
        ancestor = p.parent
        skip = False
        while ancestor is not None and getattr(ancestor, "name", None):
            if ancestor.name.lower() in ("nav", "header", "footer", "aside", "form"):
                skip = True
                break
            cls = " ".join(ancestor.get("class", [])) if hasattr(ancestor, "get") else ""
            ident = ancestor.get("id", "") if hasattr(ancestor, "get") else ""
            if RECIPE_CARD_RE.search(cls) or RECIPE_CARD_RE.search(ident):
                skip = True
                break
            ancestor = ancestor.parent
        if skip:
            continue
        text = clean_text(p.get_text(" ", strip=True))
        if len(text) < 40:
            continue
        if BOILERPLATE_RE.search(text.lower()):
            continue
        return text
    return ""


def shorten_description(text, limit: int = 400) -> str:
    """Trim an over-long fallback description at a sentence/word boundary."""
    text = clean_text(text)
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    if cut > limit // 2:
        return head[: cut + 1]
    return head.rsplit(" ", 1)[0] + "…"


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
    # Description: prefer the first intro paragraph (the "what/why" blurb),
    # falling back to the structured description when no paragraph is found.
    description = extract_first_paragraph(response.text)
    if not description:
        try:
            description = shorten_description(str(scraper.description()))
        except Exception:
            description = ""
    data["description"] = description
    return data


def build_markdown(data: dict, image_ref: str, user_tags: list,
                   original_url: str) -> str:
    auto_tags = guess_tags(data["title"])
    all_tags = sorted(list(set(auto_tags + (user_tags or []))))
    from datetime import date
    fm = {
        "date_added": date.today(),
        "layout": "recipe",
        "title": data.get("title", "Untitled Recipe"),
        "image": image_ref,
        "original_url": original_url,
        "tags": all_tags,
        "ingredients": format_ingredients(data.get("ingredients", [])),
        "directions": format_instructions(data.get("instructions", [])),
    }
    for key in ["description", "yield", "prep_time", "cook_time", "total_time"]:
        if key in data and data[key]:
            fm[key] = data[key]
    return render_recipe_markdown(fm)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Scrape a recipe URL and write markdown")
    parser.add_argument("--url", required=True)
    parser.add_argument("--tags", default="")
    args = parser.parse_args()

    user_tags = [t.strip() for t in args.tags.split() if t.strip()]

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

    md_content = build_markdown(data, image_ref, user_tags, original_url=args.url)
    recipes_dir = REPO_ROOT / "_recipes"
    recipes_dir.mkdir(exist_ok=True)
    out_path = recipes_dir / filename
    out_path.write_text(md_content, encoding="utf-8")
    print(f"Written: _recipes/{filename}")


if __name__ == "__main__":
    main()
