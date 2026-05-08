import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse, urljoin

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import requests
import yaml

try:
    from recipe_scrapers import scrape_html
except ImportError:
    raise ImportError("recipe-scrapers not installed. Run: pip install recipe-scrapers")

app = FastAPI(title="Chowdown Recipe Importer")

# Allow the Jekyll site to call the API from the browser.
# Set CORS_ORIGINS in .env as a comma-separated list to restrict origins.
_raw_origins = os.getenv("CORS_ORIGINS", "*")
_allowed_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

REPO_URL    = os.getenv("CHOWDOWN_REPO_URL", "")
REPO_BRANCH = os.getenv("CHOWDOWN_REPO_BRANCH", "main")
GIT_USER    = os.getenv("GIT_USER", "Recipe Bot")
GIT_EMAIL   = os.getenv("GIT_EMAIL", "bot@example.com")
REPO_DIR    = Path("/app/repo")

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


# ---------------------------------------------------------------------------
# Helpers
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
    except Exception:
        return False


def fetch_recipe(url: str) -> dict:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36"
    }
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    scraper = scrape_html(response.text, url)
    data: dict = {
        "title": scraper.title(),
        "ingredients": scraper.ingredients(),
        "instructions": scraper.instructions(),
        "tags": [],
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


def build_markdown(
    data: dict,
    image_filename: str,
    user_tags: list = None,
    season: str = "all",
    rating: int = None,
    difficulty: str = "easy",
    kid_friendly: bool = False,
) -> str:
    auto_tags = guess_tags_from_title(data["title"])
    user_tags = user_tags or []
    all_tags = sorted(list(set(auto_tags + user_tags)))
    season_list = (
        [s.strip() for s in season.replace(",", " ").split() if s.strip()]
        if season and season.strip().lower() != "all"
        else ["all"]
    )
    fm = {
        "layout": "recipe",
        "title": data.get("title", "Untitled Recipe"),
        "image": image_filename,
        "tags": all_tags,
        "ingredients": format_ingredients(data.get("ingredients", [])),
        "directions": format_instructions(data.get("instructions", [])),
        "date_added": str(__import__("datetime").date.today()),
        "season": season_list,
        "last_made": None,
        "rating": rating,
        "difficulty": difficulty,
        "kid_friendly": kid_friendly,
    }
    for key in ["yield", "prep_time", "cook_time", "total_time", "category", "cuisine"]:
        if key in data and data[key]:
            fm[key] = data[key]
    yaml_content = yaml.dump(fm, default_flow_style=False, sort_keys=False, allow_unicode=True, width=120)
    return f"---\n{yaml_content}---\n"


def ensure_repo():
    if (REPO_DIR / ".git").exists():
        subprocess.run(["git", "-C", str(REPO_DIR), "pull", "origin", REPO_BRANCH], check=True)
    else:
        REPO_DIR.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--branch", REPO_BRANCH, REPO_URL, str(REPO_DIR)], check=True)


def git_commit_and_push(message: str):
    subprocess.run(["git", "-C", str(REPO_DIR), "config", "user.email", GIT_EMAIL], check=False)
    subprocess.run(["git", "-C", str(REPO_DIR), "config", "user.name", GIT_USER], check=False)
    # Always refresh the remote URL from the current env var so stale clones
    # (or volumes persisted across rebuilds) don't lose their credentials.
    if REPO_URL:
        subprocess.run(["git", "-C", str(REPO_DIR), "remote", "set-url", "origin", REPO_URL], check=False)
    subprocess.run(["git", "-C", str(REPO_DIR), "add", "."], check=True)
    result = subprocess.run(["git", "-C", str(REPO_DIR), "commit", "-m", message], capture_output=True, text=True)
    if result.returncode != 0 and "nothing to commit" not in result.stdout.lower():
        pass
    subprocess.run(["git", "-C", str(REPO_DIR), "pull", "--rebase", "origin", REPO_BRANCH], check=True)
    subprocess.run(["git", "-C", str(REPO_DIR), "push", "origin", REPO_BRANCH], check=True)


def load_meal_plan() -> dict:
    plan_path = REPO_DIR / "_data" / "meal_plan.yml"
    if not plan_path.exists():
        return {}
    try:
        return yaml.safe_load(plan_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


def write_frontmatter_field(path: Path, key: str, value: str):
    raw = path.read_text(encoding="utf-8")
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return
    fm_text = parts[1]
    pattern = re.compile(r"^(" + re.escape(key) + r":\s*)(.*)$", re.MULTILINE)
    if pattern.search(fm_text):
        new_fm = pattern.sub(rf"\g<1>{value}", fm_text)
    else:
        new_fm = fm_text.rstrip("\n") + f"\n{key}: {value}\n"
    path.write_text("---" + new_fm + "---" + parts[2], encoding="utf-8")


# ---------------------------------------------------------------------------
# Import UI -- the only HTML page served by the importer
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def form():
    return """<!DOCTYPE html>
<html>
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Chowdown Importer</title>
  <style>
    body { font-family: system-ui, -apple-system, sans-serif; max-width: 540px; margin: 2rem auto; padding: 1rem; background: #fafafa; }
    h1 { font-size: 1.5rem; margin: 0 0 0.25rem; }
    .subtitle { color: #666; margin-bottom: 1.5rem; font-size: 0.9rem; }
    label { display: block; font-size: 0.85rem; font-weight: 600; margin-bottom: 0.25rem; color: #444; }
    input, select, button { width: 100%; padding: 0.75rem; font-size: 1rem; border-radius: 8px; border: 1px solid #ccc; box-sizing: border-box; margin-bottom: 0.75rem; background: #fff; }
    button { background: #F53200; color: white; border: none; cursor: pointer; font-weight: 600; }
    button:disabled { background: #999; cursor: default; }
    .row { display: flex; gap: 0.75rem; }
    .row > * { flex: 1; }
    .check-row { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.75rem; }
    .check-row input { width: auto; margin: 0; }
    .check-row label { margin: 0; font-weight: 400; }
    .section-title { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.1em; color: #999; font-weight: 700; margin: 1rem 0 0.5rem; border-top: 1px solid #eee; padding-top: 1rem; }
    #result { margin-top: 1rem; padding: 1rem; border-radius: 8px; display: none; }
    .success { background: #d4edda; color: #155724; }
    .error   { background: #f8d7da; color: #721c24; }
  </style>
</head>
<body>
  <h1>Chowdown Importer</h1>
  <p class="subtitle">Paste a recipe URL to scrape and save it to your cookbook.</p>
  <form id="recipeForm">
    <label>Recipe URL</label>
    <input type="url" name="url" placeholder="https://cooking.nytimes.com/recipes/..." required>
    <label>Extra tags</label>
    <input type="text" name="tags" placeholder="space separated">
    <div class="section-title">Meal planning metadata</div>
    <label>Season</label>
    <input type="text" name="season" placeholder="all  or  fall, winter" value="all">
    <div class="row">
      <div>
        <label>Rating (1-5)</label>
        <input type="number" name="rating" min="1" max="5" placeholder="4">
      </div>
      <div>
        <label>Difficulty</label>
        <select name="difficulty">
          <option value="easy">Easy</option>
          <option value="medium">Medium</option>
          <option value="hard">Hard</option>
        </select>
      </div>
    </div>
    <div class="check-row">
      <input type="checkbox" name="kid_friendly" id="kid_friendly" value="true">
      <label for="kid_friendly">Kid-friendly</label>
    </div>
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
      if (!form.has("kid_friendly")) form.set("kid_friendly", "false");
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


# ---------------------------------------------------------------------------
# Import endpoint
# ---------------------------------------------------------------------------

class ImportResponse(BaseModel):
    message: str
    filename: str
    title: str
    tags: list
    image: str


@app.post("/import", response_model=ImportResponse)
async def import_recipe(
    url: str = Form(...),
    tags: str = Form(""),
    season: str = Form("all"),
    rating: str = Form(""),
    difficulty: str = Form("easy"),
    kid_friendly: str = Form("false"),
):
    if not REPO_URL:
        raise HTTPException(status_code=500, detail="CHOWDOWN_REPO_URL not configured")

    user_tags = [t.strip() for t in tags.split() if t.strip()]
    rating_int = int(rating) if rating.strip().isdigit() and 1 <= int(rating.strip()) <= 5 else None
    kid_bool = kid_friendly.lower() in ("true", "1", "yes", "on")
    difficulty_clean = difficulty.strip().lower() if difficulty.strip().lower() in ("easy", "medium", "hard") else "easy"

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

    images_dir = REPO_DIR / "images"
    image_filename = f"{slug}.jpg"
    image_save_path = images_dir / image_filename
    image_ref = image_filename

    if data.get("image_url"):
        image_downloaded = download_image(data["image_url"], url, image_save_path)
        if image_downloaded:
            actual_ext = Path(image_save_path).suffix
            if actual_ext and actual_ext != ".jpg":
                image_filename = f"{slug}{actual_ext}"
                image_save_path.rename(images_dir / image_filename)
                image_ref = image_filename
        else:
            image_ref = ""
            if image_save_path.exists():
                image_save_path.unlink()

    md_content = build_markdown(
        data, image_ref, user_tags,
        season=season, rating=rating_int,
        difficulty=difficulty_clean, kid_friendly=kid_bool,
    )
    recipes_dir = REPO_DIR / "_recipes"
    recipes_dir.mkdir(exist_ok=True)
    (recipes_dir / filename).write_text(md_content, encoding="utf-8")

    try:
        git_commit_and_push(f"Add recipe: {data['title']}")
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Git push failed: {e}")

    return ImportResponse(
        message="Recipe imported successfully",
        filename=filename,
        title=data["title"],
        tags=data.get("tags", []),
        image=image_ref,
    )


# ---------------------------------------------------------------------------
# Planning API -- headless JSON only; all UI lives on the Jekyll site
# ---------------------------------------------------------------------------

@app.post("/plan/save")
async def plan_save(request: Request):
    """Save a full week plan. Form fields: week_of, monday...sunday (each a recipe slug)."""
    form = await request.form()
    week_of = str(form.get("week_of", ""))
    plan: dict = {"week_of": week_of}
    for day in DAYS:
        slug = str(form.get(day, "")).strip()
        plan[day] = {"recipe": slug if slug else None, "notes": None}

    try:
        ensure_repo()
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Git error: {e}")

    plan_path = REPO_DIR / "_data" / "meal_plan.yml"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        yaml.dump(plan, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    try:
        git_commit_and_push(f"Update meal plan for week of {week_of}")
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Git push failed: {e}")

    return {"message": f"Meal plan saved for week of {week_of}", "week_of": week_of}


@app.post("/plan/add")
async def plan_add(slug: str = Form(...)):
    """Append a recipe slug to the first empty slot in the current week's plan."""
    from datetime import date, timedelta

    try:
        ensure_repo()
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Git error: {e}")

    if not (REPO_DIR / "_recipes" / f"{slug}.md").exists():
        raise HTTPException(status_code=404, detail=f"Recipe not found: {slug}")

    plan = load_meal_plan()
    week_of = date.today() - timedelta(days=date.today().weekday())
    if str(plan.get("week_of", "")) != str(week_of):
        plan = {"week_of": str(week_of)}
        for day in DAYS:
            plan[day] = {"recipe": None, "notes": None}

    added_to = None
    for day in DAYS:
        day_data = plan.get(day, {})
        existing = day_data.get("recipe") if isinstance(day_data, dict) else day_data
        if not existing or str(existing) in ("None", "", "null", "~"):
            if isinstance(plan.get(day), dict):
                plan[day]["recipe"] = slug
            else:
                plan[day] = {"recipe": slug, "notes": None}
            added_to = day
            break

    if not added_to:
        raise HTTPException(status_code=409, detail="All days this week are already planned")

    plan_path = REPO_DIR / "_data" / "meal_plan.yml"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        yaml.dump(plan, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    try:
        git_commit_and_push(f"Add {slug} to meal plan ({added_to})")
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Git push failed: {e}")

    return {"message": f"Added '{slug}' to {added_to}", "day": added_to}


@app.post("/update-made")
async def update_made(slug: str = Form(...)):
    """Set last_made to today for a recipe slug."""
    from datetime import date

    try:
        ensure_repo()
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Git error: {e}")

    path = REPO_DIR / "_recipes" / f"{slug}.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Recipe not found: {slug}")

    today = date.today().isoformat()
    write_frontmatter_field(path, "last_made", today)

    try:
        git_commit_and_push(f"Log last_made for {slug}: {today}")
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Git push failed: {e}")

    return {"message": f"Updated last_made for '{slug}' to {today}"}


@app.post("/grocery/save")
async def grocery_save():
    """Aggregate ingredients from the current meal plan and save to grocery_list.yml."""
    try:
        ensure_repo()
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Git error: {e}")

    plan = load_meal_plan()
    if not plan:
        raise HTTPException(status_code=404, detail="No meal plan found")

    slugs = []
    for day in DAYS:
        day_data = plan.get(day, {})
        slug = day_data.get("recipe") if isinstance(day_data, dict) else day_data
        if slug and str(slug) not in ("None", "", "null", "~"):
            slugs.append(str(slug))

    all_ingredients: list = []
    for slug in slugs:
        path = REPO_DIR / "_recipes" / f"{slug}.md"
        if not path.exists():
            continue
        parts = path.read_text(encoding="utf-8").split("---", 2)
        if len(parts) < 3:
            continue
        try:
            fields = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError:
            continue
        ings = fields.get("ingredients", [])
        if isinstance(ings, list):
            all_ingredients.extend([str(i).strip() for i in ings if str(i).strip()])
        elif isinstance(ings, str):
            all_ingredients.extend([ln.strip().lstrip("-").strip() for ln in ings.splitlines() if ln.strip()])

    seen: set = set()
    unique: list = []
    for ing in all_ingredients:
        key = re.sub(r"[^a-z]", "", ing.lower())
        if key not in seen:
            seen.add(key)
            unique.append(ing)

    out = {"week_of": str(plan.get("week_of", "")), "items": unique}
    out_path = REPO_DIR / "_data" / "grocery_list.yml"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml.dump(out, default_flow_style=False, allow_unicode=True), encoding="utf-8")

    try:
        git_commit_and_push(f"Update grocery list for week of {out['week_of']}")
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Git push failed: {e}")

    return {"message": f"Saved {len(unique)} items", "count": len(unique), "week_of": out["week_of"]}
