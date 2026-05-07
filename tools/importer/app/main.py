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

    # Normalise season into a YAML list
    if season and season.strip().lower() != "all":
        season_list = [s.strip() for s in season.replace(",", " ").split() if s.strip()]
    else:
        season_list = ["all"]

    fm = {
        "layout": "recipe",
        "title": data.get("title", "Untitled Recipe"),
        "image": image_filename,
        "tags": all_tags,
        "ingredients": format_ingredients(data.get("ingredients", [])),
        "directions": format_instructions(data.get("instructions", [])),
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


NAV_HTML = """
<nav style="background:#F53200;padding:0.75rem 1rem;margin-bottom:1.5rem;border-radius:8px;display:flex;gap:1rem">
  <a href="/" style="color:#fff;text-decoration:none;font-weight:600">Import</a>
  <a href="/plan" style="color:#fff;text-decoration:none;font-weight:600">Meal Plan</a>
  <a href="/grocery" style="color:#fff;text-decoration:none;font-weight:600">Grocery List</a>
</nav>
"""


@app.get("/", response_class=HTMLResponse)
async def form():
    return """<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Chowdown Importer</title>
    <style>
        body { font-family: system-ui, -apple-system, sans-serif; max-width: 540px; margin: 2rem auto; padding: 1rem; background: #fafafa; }
        h1 { font-size: 1.5rem; margin-bottom: 0.5rem; }
        p { color: #666; margin-bottom: 1.5rem; }
        label { display:block; font-size:0.85rem; font-weight:600; margin-bottom:0.25rem; color:#444; }
        input, select, button { width: 100%; padding: 0.75rem; font-size: 1rem; border-radius: 8px; border: 1px solid #ccc; box-sizing: border-box; margin-bottom: 0.75rem; background:#fff; }
        button { background: #F53200; color: white; border: none; cursor: pointer; font-weight: 600; }
        button:disabled { background: #999; }
        .row { display:flex; gap:0.75rem; }
        .row > * { flex:1; }
        .check-row { display:flex; align-items:center; gap:0.5rem; margin-bottom:0.75rem; }
        .check-row input { width:auto; margin:0; }
        .check-row label { margin:0; font-weight:400; }
        #result { margin-top: 1rem; padding: 1rem; border-radius: 8px; display: none; }
        .success { background: #d4edda; color: #155724; }
        .error { background: #f8d7da; color: #721c24; }
        .section-title { font-size:0.7rem; text-transform:uppercase; letter-spacing:0.1em; color:#999; font-weight:700; margin:1rem 0 0.5rem; border-top:1px solid #eee; padding-top:1rem; }
    </style>
</head>
<body>
    """ + NAV_HTML + """
    <h1>📖 Import Recipe</h1>
    <p>Paste a recipe URL to import it into your cookbook.</p>
    <form id="recipeForm">
        <label>Recipe URL</label>
        <input type="url" name="url" placeholder="https://cooking.nytimes.com/recipes/..." required>
        <label>Tags</label>
        <input type="text" name="tags" placeholder="Optional extra tags (space separated)">

        <div class="section-title">Meal Planning</div>
        <label>Season</label>
        <input type="text" name="season" placeholder="all  —or—  fall, winter" value="all">
        <div class="row">
            <div>
                <label>Rating (1–5)</label>
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
            // Normalise checkbox — FormData omits unchecked boxes
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


class ImportResponse(BaseModel):
    message: str
    filename: str
    title: str
    tags: list
    image: str


# ---------------------------------------------------------------------------
# Planning helpers
# ---------------------------------------------------------------------------

def load_all_recipes() -> list[dict]:
    """Return parsed frontmatter dicts for every recipe in the repo."""
    recipes_dir = REPO_DIR / "_recipes"
    if not recipes_dir.exists():
        return []
    results = []
    for path in recipes_dir.glob("*.md"):
        text = path.read_text(encoding="utf-8")
        parts = text.split("---", 2)
        if len(parts) < 3:
            continue
        try:
            fields = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError:
            fields = {}
        fields["_slug"] = path.stem
        fields["_path"] = path
        results.append(fields)
    return results


def load_meal_plan() -> dict:
    plan_path = REPO_DIR / "_data" / "meal_plan.yml"
    if not plan_path.exists():
        return {}
    try:
        return yaml.safe_load(plan_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


def write_frontmatter_field(path: Path, key: str, value: str):
    """Update a single frontmatter field in a recipe file."""
    raw = path.read_text(encoding="utf-8")
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return
    fm_text = parts[1]
    pattern = re.compile(r'^(' + re.escape(key) + r':\s*)(.*)$', re.MULTILINE)
    if pattern.search(fm_text):
        new_fm = pattern.sub(rf'\g<1>{value}', fm_text)
    else:
        new_fm = fm_text.rstrip("\n") + f"\n{key}: {value}\n"
    path.write_text("---" + new_fm + "---" + parts[2], encoding="utf-8")


DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
SKIP_TAGS = {"dessert", "side-dish", "breakfast", "baking", "dressing"}


# ---------------------------------------------------------------------------
# Planning endpoints
# ---------------------------------------------------------------------------

@app.get("/plan", response_class=HTMLResponse)
async def plan_page():
    """Suggest a week of dinners, skipping anything made in the past 2 weeks."""
    from datetime import date, timedelta
    import random as _random

    try:
        ensure_repo()
    except subprocess.CalledProcessError as e:
        return HTMLResponse(f"<p>Git error: {e}</p>", status_code=500)

    recipes = load_all_recipes()
    cutoff = date.today() - timedelta(days=14)

    eligible = []
    for r in recipes:
        lm = r.get("last_made")
        if lm and str(lm) not in ("None", "", "null", "~"):
            try:
                if date.fromisoformat(str(lm)) >= cutoff:
                    continue
            except ValueError:
                pass
        tags = r.get("tags", []) or []
        if isinstance(tags, str):
            tags = tags.split()
        if any(t.lower() in SKIP_TAGS for t in tags):
            continue
        eligible.append(r)

    pool = (eligible * 2) if len(eligible) < 7 else eligible
    _random.shuffle(pool)

    week_of = date.today() - timedelta(days=date.today().weekday())
    rows = ""
    hidden_inputs = ""
    for i, day in enumerate(DAYS):
        if i < len(pool):
            r = pool[i]
            slug = r["_slug"]
            title = r.get("title", slug)
            diff = r.get("difficulty", "")
            badge = f'<span style="font-size:0.7rem;background:#eee;padding:0.2em 0.5em;border-radius:3px">{diff}</span>' if diff else ""
            rows += f'<tr><td style="font-weight:600;padding:0.5rem 0.75rem">{day.capitalize()}</td><td style="padding:0.5rem 0.75rem">{title} {badge}</td></tr>\n'
            hidden_inputs += f'<input type="hidden" name="{day}" value="{slug}">\n'
        else:
            rows += f'<tr><td style="padding:0.5rem 0.75rem">{day.capitalize()}</td><td style="padding:0.5rem 0.75rem;color:#aaa">— no plan —</td></tr>\n'
            hidden_inputs += f'<input type="hidden" name="{day}" value="">\n'

    return f"""<!DOCTYPE html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Meal Plan Suggestion</title>
<style>body{{font-family:system-ui,-apple-system,sans-serif;max-width:600px;margin:2rem auto;padding:1rem;background:#fafafa}}
h1{{font-size:1.5rem;margin-bottom:0.25rem}}p{{color:#666;margin-bottom:1rem}}
table{{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
tr:nth-child(even){{background:#f9f9f9}}button{{margin-top:1rem;width:100%;padding:0.75rem;font-size:1rem;border-radius:8px;border:none;background:#F53200;color:#fff;cursor:pointer;font-weight:600}}
</style></head><body>
{NAV_HTML}
<h1>🗓 Suggested Meal Plan</h1>
<p>Week of {week_of} &nbsp;·&nbsp; <a href="/plan">Shuffle again</a></p>
<table>{rows}</table>
<form method="post" action="/plan/save">
{hidden_inputs}
<input type="hidden" name="week_of" value="{week_of}">
<button type="submit">Save this plan to meal_plan.yml</button>
</form>
</body></html>"""


@app.post("/plan/save", response_class=HTMLResponse)
async def plan_save(request: Request):
    form = await request.form()
    week_of = str(form.get("week_of", ""))
    plan = {"week_of": week_of}
    for day in DAYS:
        slug = str(form.get(day, "")).strip()
        plan[day] = {"recipe": slug if slug else None, "notes": None}

    plan_path = REPO_DIR / "_data" / "meal_plan.yml"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(yaml.dump(plan, default_flow_style=False, allow_unicode=True, sort_keys=False), encoding="utf-8")

    try:
        git_commit_and_push(f"Update meal plan for week of {week_of}")
    except subprocess.CalledProcessError as e:
        return HTMLResponse(f"<p>Git push failed: {e}</p>", status_code=500)

    return HTMLResponse(f"""
    <!DOCTYPE html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Plan Saved</title>
    <style>body{{font-family:system-ui;max-width:500px;margin:2rem auto;padding:1rem;background:#fafafa}}</style></head><body>
    {NAV_HTML}
    <p style="background:#d4edda;color:#155724;padding:1rem;border-radius:8px">✅ Meal plan saved for week of {week_of}.</p>
    <p><a href="/grocery">Build grocery list →</a></p>
    </body></html>""")


@app.get("/grocery", response_class=HTMLResponse)
async def grocery_page():
    """Aggregate ingredients from the current week's meal plan."""
    try:
        ensure_repo()
    except subprocess.CalledProcessError as e:
        return HTMLResponse(f"<p>Git error: {e}</p>", status_code=500)

    plan = load_meal_plan()
    if not plan:
        return HTMLResponse(NAV_HTML + "<p>No meal plan found. <a href='/plan'>Create one →</a></p>")

    slugs = []
    for day in DAYS:
        day_data = plan.get(day, {})
        slug = day_data.get("recipe") if isinstance(day_data, dict) else day_data
        if slug and str(slug) not in ("None", "", "null", "~"):
            slugs.append(str(slug))

    all_ingredients = []
    missing = []
    for slug in slugs:
        path = REPO_DIR / "_recipes" / f"{slug}.md"
        if not path.exists():
            missing.append(slug)
            continue
        text = path.read_text(encoding="utf-8")
        parts = text.split("---", 2)
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
            all_ingredients.extend([l.strip().lstrip("-").strip() for l in ings.splitlines() if l.strip()])

    # Deduplicate preserving order
    seen: set = set()
    unique = []
    for ing in all_ingredients:
        key = re.sub(r'[^a-z]', '', ing.lower())
        if key not in seen:
            seen.add(key)
            unique.append(ing)

    items_html = "\n".join(f'<li style="padding:0.3rem 0">{i}</li>' for i in unique)
    warn_html = f'<p style="color:#856404">⚠️ Recipe files not found: {", ".join(missing)}</p>' if missing else ""
    week_label = str(plan.get("week_of", ""))

    return f"""<!DOCTYPE html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Grocery List</title>
<style>body{{font-family:system-ui,-apple-system,sans-serif;max-width:540px;margin:2rem auto;padding:1rem;background:#fafafa}}
h1{{font-size:1.5rem;margin-bottom:0.25rem}}p{{color:#666}}
ul{{background:#fff;border-radius:8px;padding:1rem 1rem 1rem 2rem;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
button{{margin-top:1rem;width:100%;padding:0.75rem;font-size:1rem;border-radius:8px;border:none;background:#F53200;color:#fff;cursor:pointer;font-weight:600}}
</style></head><body>
{NAV_HTML}
<h1>🛒 Grocery List</h1>
<p>Week of {week_label} · {len(slugs)} recipes · {len(unique)} items</p>
{warn_html}
<ul>{items_html}</ul>
<form method="post" action="/grocery/save">
  <button type="submit">Save to _data/grocery_list.yml</button>
</form>
</body></html>"""


@app.post("/grocery/save", response_class=HTMLResponse)
async def grocery_save():
    """Write current grocery list to _data/grocery_list.yml and push."""
    try:
        ensure_repo()
    except subprocess.CalledProcessError as e:
        return HTMLResponse(f"<p>Git error: {e}</p>", status_code=500)

    plan = load_meal_plan()
    slugs = []
    for day in DAYS:
        day_data = plan.get(day, {})
        slug = day_data.get("recipe") if isinstance(day_data, dict) else day_data
        if slug and str(slug) not in ("None", "", "null", "~"):
            slugs.append(str(slug))

    all_ingredients = []
    for slug in slugs:
        path = REPO_DIR / "_recipes" / f"{slug}.md"
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        parts = text.split("---", 2)
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
            all_ingredients.extend([l.strip().lstrip("-").strip() for l in ings.splitlines() if l.strip()])

    seen: set = set()
    unique = []
    for ing in all_ingredients:
        key = re.sub(r'[^a-z]', '', ing.lower())
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
        return HTMLResponse(f"<p>Git push failed: {e}</p>", status_code=500)

    return HTMLResponse(f"""
    <!DOCTYPE html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Grocery List Saved</title>
    <style>body{{font-family:system-ui;max-width:500px;margin:2rem auto;padding:1rem;background:#fafafa}}</style></head><body>
    {NAV_HTML}
    <p style="background:#d4edda;color:#155724;padding:1rem;border-radius:8px">✅ Saved {len(unique)} items to _data/grocery_list.yml.</p>
    </body></html>""")


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


# ---------------------------------------------------------------------------
# Import endpoint
# ---------------------------------------------------------------------------

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
    md_content = build_markdown(
        data, image_ref, user_tags,
        season=season,
        rating=rating_int,
        difficulty=difficulty_clean,
        kid_friendly=kid_bool,
    )
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