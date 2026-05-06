import os
import subprocess
from pathlib import Path
from urllib.parse import urlparse, urljoin

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import requests
import yaml
import re

try:
    from recipe_scrapers import scrape_html
except ImportError:
    raise ImportError("recipe-scrapers not installed. Run: pip install recipe-scrapers")

app = FastAPI(title="Chowdown Recipe Importer")

REPO_URL = os.getenv("CHOWDOWN_REPO_URL", "")
REPO_BRANCH = os.getenv("CHOWDOWN_REPO_BRANCH", "main")
GIT_USER = os.getenv("GIT_USER", "Recipe Bot")
GIT_EMAIL = os.getenv("GIT_EMAIL", "bot@example.com")

# Persistent repo directory inside the container
REPO_DIR = Path("/app/repo")


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
    return [str(ing).strip() for ing in ingredients if str(ing).strip()]


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


def guess_tags_from_title(title: str) -> list:
    title_lower = title.lower()
    tag_map = {
        "vegetarian": ["vegetarian"],
        "vegan": ["vegan"],
        "gluten-free": ["gluten-free"],
        "chicken": ["chicken"],
        "beef": ["beef"],
        "pork": ["pork"],
        "fish": ["fish", "seafood"],
        "salmon": ["fish", "seafood"],
        "shrimp": ["seafood"],
        "pasta": ["pasta"],
        "soup": ["soup"],
        "stew": ["stew"],
        "salad": ["salad"],
        "dessert": ["dessert"],
        "cake": ["dessert", "baking"],
        "cookie": ["dessert", "baking"],
        "bread": ["bread", "baking"],
        "pizza": ["pizza"],
        "taco": ["mexican"],
        "curry": ["curry"],
    }
    tags = set()
    for keyword, tag_list in tag_map.items():
        if keyword in title_lower:
            tags.update(tag_list)
    return sorted(list(tags))


def download_image(image_url: str, source_url: str, save_path: Path) -> bool:
    """Download recipe image and save to repo images directory."""
    if not image_url:
        return False

    # Handle relative URLs
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

        # Ensure images directory exists
        save_path.parent.mkdir(parents=True, exist_ok=True)

        # Stream download to avoid loading large images into memory
        with open(save_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        return True
    except Exception:
        return False


def fetch_recipe(url: str):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36"
    }
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    scraper = scrape_html(response.text, url)

    data = {
        "title": scraper.title(),
        "ingredients": scraper.ingredients(),
        "instructions": scraper.instructions(),
        "tags": [],
        "image_url": "",
    }

    for method_name, key_name in [
        ("yields", "yield"),
        ("total_time", "total_time"),
        ("prep_time", "prep_time"),
        ("cook_time", "cook_time"),
        ("category", "category"),
        ("cuisine", "cuisine"),
    ]:
        try:
            val = getattr(scraper, method_name)()
            if val:
                data[key_name] = str(val)
        except Exception:
            pass

    # Try to get image URL from scraper
    try:
        data["image_url"] = scraper.image()
    except Exception:
        pass

    return data


def build_markdown(data: dict, image_filename: str, user_tags: list = None) -> str:
    auto_tags = guess_tags_from_title(data["title"])
    user_tags = user_tags or []
    all_tags = sorted(list(set(auto_tags + user_tags)))

    fm = {
        "layout": "recipe",
        "title": data.get("title", "Untitled Recipe"),
        "image": image_filename,
        "tags": all_tags,
        "ingredients": format_ingredients(data.get("ingredients", [])),
        "directions": format_instructions(data.get("instructions", [])),
    }

    for key in ["yield", "prep_time", "cook_time", "total_time", "category", "cuisine"]:
        if key in data and data[key]:
            fm[key] = data[key]

    yaml_content = yaml.dump(fm, default_flow_style=False, sort_keys=False, allow_unicode=True, width=120)
    return f"---\n{yaml_content}---\n"


def ensure_repo():
    """Clone the repo if it does not exist, or pull if it does."""
    if (REPO_DIR / ".git").exists():
        subprocess.run(["git", "-C", str(REPO_DIR), "pull", "origin", REPO_BRANCH], check=True)
    else:
        REPO_DIR.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--branch", REPO_BRANCH, REPO_URL, str(REPO_DIR)], check=True)


def git_commit_and_push(message: str):
    subprocess.run(["git", "-C", str(REPO_DIR), "config", "user.email", GIT_EMAIL], check=False)
    subprocess.run(["git", "-C", str(REPO_DIR), "config", "user.name", GIT_USER], check=False)
    subprocess.run(["git", "-C", str(REPO_DIR), "add", "."], check=True)
    result = subprocess.run(["git", "-C", str(REPO_DIR), "commit", "-m", message], capture_output=True, text=True)
    if result.returncode != 0 and "nothing to commit" not in result.stdout.lower():
        pass
    subprocess.run(["git", "-C", str(REPO_DIR), "push", "origin", REPO_BRANCH], check=True)


@app.get("/", response_class=HTMLResponse)
async def form():
    return """<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Chowdown Importer</title>
    <style>
        body { font-family: system-ui, -apple-system, sans-serif; max-width: 500px; margin: 2rem auto; padding: 1rem; background: #fafafa; }
        h1 { font-size: 1.5rem; margin-bottom: 0.5rem; }
        p { color: #666; margin-bottom: 1.5rem; }
        input, button { width: 100%; padding: 0.75rem; font-size: 1rem; border-radius: 8px; border: 1px solid #ccc; box-sizing: border-box; margin-bottom: 0.75rem; }
        button { background: #2ea44f; color: white; border: none; cursor: pointer; font-weight: 600; }
        button:disabled { background: #999; }
        #result { margin-top: 1rem; padding: 1rem; border-radius: 8px; display: none; }
        .success { background: #d4edda; color: #155724; }
        .error { background: #f8d7da; color: #721c24; }
    </style>
</head>
<body>
    <h1>📖 Chowdown Importer</h1>
    <p>Paste a recipe URL to import it into your cookbook.</p>
    <form id="recipeForm">
        <input type="url" name="url" placeholder="https://cooking.nytimes.com/recipes/..." required>
        <input type="text" name="tags" placeholder="Optional tags (space separated)">
        <button type="submit" id="btn">Import Recipe</button>
    </form>
    <div id="result"></div>
    <script>
        document.getElementById("recipeForm").addEventListener("submit", async (e) => {
            e.preventDefault();
            const btn = document.getElementById("btn");
            const result = document.getElementById("result");
            btn.disabled = true; btn.textContent = "Importing...";
            result.style.display = "none";
            const form = new FormData(e.target);
            try {
                const r = await fetch("/import", { method: "POST", body: form });
                const data = await r.json();
                result.className = r.ok ? "success" : "error";
                result.textContent = data.detail || data.message || JSON.stringify(data);
                result.style.display = "block";
                if (r.ok) e.target.reset();
            } catch (err) {
                result.className = "error";
                result.textContent = "Error: " + err.message;
                result.style.display = "block";
            } finally {
                btn.disabled = false; btn.textContent = "Import Recipe";
            }
        });
    </script>
</body>
</html>"""


class ImportResponse(BaseModel):
    message: str
    filename: str
    title: str
    tags: list
    image: str


@app.post("/import", response_model=ImportResponse)
async def import_recipe(
    url: str = Form(...),
    tags: str = Form("")
):
    if not REPO_URL:
        raise HTTPException(status_code=500, detail="CHOWDOWN_REPO_URL not configured")

    user_tags = [t.strip() for t in tags.split() if t.strip()]

    try:
        ensure_repo()
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Git clone/pull failed: {e}")

    try:
        data = fetch_recipe(url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to fetch recipe: {e}")

    slug = slugify(data["title"])
    filename = f"{slug}.md"

    # Set up image paths
    images_dir = REPO_DIR / "images"
    image_filename = f"{slug}.jpg"
    image_save_path = images_dir / image_filename
    image_ref = image_filename

    # Download image if URL exists
    image_downloaded = False
    if data.get("image_url"):
        image_downloaded = download_image(data["image_url"], url, image_save_path)
        if image_downloaded:
            # Determine actual extension from downloaded file
            actual_ext = Path(image_save_path).suffix
            if actual_ext and actual_ext != ".jpg":
                image_filename = f"{slug}{actual_ext}"
                new_path = images_dir / image_filename
                image_save_path.rename(new_path)
                image_ref = image_filename
        else:
            # If download failed, use empty string (Chowdown handles missing images gracefully)
            image_ref = ""
            if image_save_path.exists():
                image_save_path.unlink()

    # Build markdown
    md_content = build_markdown(data, image_ref, user_tags)
    recipes_dir = REPO_DIR / "_recipes"
    recipes_dir.mkdir(exist_ok=True)
    recipe_path = recipes_dir / filename
    recipe_path.write_text(md_content, encoding="utf-8")

    try:
        git_commit_and_push(f"Add recipe: {data['title']}")
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Git push failed: {e}")

    return ImportResponse(
        message="Recipe imported successfully",
        filename=filename,
        title=data["title"],
        tags=data.get("tags", []),
        image=image_ref
    )