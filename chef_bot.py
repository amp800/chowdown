import sys
import os
import json
import random
import re
import argparse
from curl_cffi import requests
from bs4 import BeautifulSoup
from datetime import datetime, date, timedelta

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

# --- CONFIGURATION ---
RECIPE_FOLDER = "_recipes"
IMAGE_FOLDER = "assets/img"
# ---------------------

def save_image(image_url, filename):
    if not image_url: return None
    try:
        # Impersonate Chrome to download the image
        response = requests.get(image_url, impersonate="chrome110")
        if response.status_code == 200:
            filepath = os.path.join(IMAGE_FOLDER, filename)
            with open(filepath, 'wb') as f:
                f.write(response.content)
            return f"/{IMAGE_FOLDER}/{filename}"
    except Exception as e:
        print(f"⚠️  Could not download image: {e}")
    return None

def scrape_food_site(url):
    print(f"🕵️  Attempting stealth scrape: {url}")
    try:
        response = requests.get(url, impersonate="chrome110", timeout=15)
        
        if response.status_code != 200:
            print(f"❌ Server blocked us with code: {response.status_code}")
            return None

        soup = BeautifulSoup(response.content, 'html.parser')
        
        # 1. Hunt for JSON-LD
        scripts = soup.find_all('script', type='application/ld+json')
        data = None
        
        for script in scripts:
            try:
                json_data = json.loads(script.string)
                if isinstance(json_data, list):
                    for item in json_data:
                        if 'Recipe' in item.get('@type', ''):
                            data = item
                            break
                elif 'Recipe' in json_data.get('@type', ''):
                    data = json_data
                if data: break
            except:
                continue

        if not data:
            # Fallback: Try to grab title manually
            title = soup.find('h1').get_text().strip() if soup.find('h1') else "Unknown Recipe"
            print(f"⚠️  JSON-LD not found. Creating skeleton for '{title}'...")
            return {
                "title": title,
                "ingredients": ["  (Please fill in manually)"],
                "instructions": "(Please fill in manually)",
                "image_url": None
            }

        # 2. Extract Data
        title = data.get('name', 'Unknown Recipe')
        
        # Ingredients
        ing_data = data.get('recipeIngredient', [])
        ingredients = []
        for i in ing_data:
            ingredients.append(f"  {i}")
            
        # Instructions
        instr_data = data.get('recipeInstructions', [])
        instructions = ""
        if isinstance(instr_data, list):
            for step in instr_data:
                if isinstance(step, dict):
                    instructions += f"{step.get('text', '')}\n\n"
                else:
                    instructions += f"{step}\n\n"
        else:
            instructions = instr_data

        # Image
        img_data = data.get('image', [])
        image_url = ""
        if isinstance(img_data, list):
            image_url = img_data[0]
        elif isinstance(img_data, dict):
            image_url = img_data.get('url', '')
        else:
            image_url = img_data

        return {
            "title": title,
            "ingredients": ingredients,
            "instructions": instructions,
            "image_url": image_url
        }

    except Exception as e:
        print(f"❌ Error: {e}")
        return None

def make_recipe(url):
    data = scrape_food_site(url)

    if not data:
        print("❌ Failed to get recipe data.")
        return

    title = data['title']
    slug = title.lower().replace(" ", "-").replace("'", "").replace("’", "").replace('"', "")
    date_now = datetime.now().isoformat()
    
    image_path = ""
    if data['image_url']:
        image_filename = f"{slug}.jpg"
        image_path = save_image(data['image_url'], image_filename) or ""

    formatted_ingredients = "\n".join(data['ingredients'])

    # Prompt for planning fields
    print("\n📋 Meal planning metadata (press Enter to skip):")

    season_input = input("  Seasons (e.g. fall, winter / all): ").strip()
    if season_input:
        if season_input.lower() == "all":
            season_val = "[all]"
        else:
            parts = [s.strip() for s in season_input.split(",")]
            season_val = "[" + ", ".join(parts) + "]"
    else:
        season_val = "[all]"

    rating_input = input("  Rating 1-5: ").strip()
    rating_val = int(rating_input) if rating_input.isdigit() and 1 <= int(rating_input) <= 5 else "~"

    difficulty_input = input("  Difficulty (easy / medium / hard): ").strip().lower()
    difficulty_val = difficulty_input if difficulty_input in ("easy", "medium", "hard") else "easy"

    kid_input = input("  Kid-friendly? (y/n): ").strip().lower()
    kid_val = "true" if kid_input == "y" else "false"

    # FOOD-SPECIFIC FRONTMATTER
    content = f"""---
title: "{title}"
date: {date_now}
category: Main
ingredients: |-
{formatted_ingredients}
source-url: {url}
image: "{image_path}"
layout: recipe
season: {season_val}
last_made: ~
rating: {rating_val}
difficulty: {difficulty_val}
kid_friendly: {kid_val}
---
{data['instructions']}
"""

    filename = f"{RECIPE_FOLDER}/{datetime.now().strftime('%Y-%m-%d')}-{slug}.md"
    with open(filename, "w") as f:
        f.write(content)

    print(f"✅ Success! Created: {filename}")


# ---------------------------------------------------------------------------
# Helpers for planning commands
# ---------------------------------------------------------------------------

def _parse_frontmatter(filepath):
    """Return (frontmatter_text, body_text, fields_dict) for a recipe file."""
    with open(filepath, "r") as f:
        raw = f.read()

    parts = raw.split("---", 2)
    if len(parts) < 3:
        return None, None, {}

    fm_text = parts[1]
    body = parts[2]

    if HAS_YAML:
        try:
            fields = yaml.safe_load(fm_text) or {}
        except yaml.YAMLError:
            fields = {}
    else:
        fields = {}
        for line in fm_text.splitlines():
            m = re.match(r'^(\w[\w_-]*):\s*(.*)', line)
            if m:
                key, val = m.group(1), m.group(2).strip()
                if val.lower() in ("true", "false"):
                    fields[key] = val.lower() == "true"
                elif re.match(r'^\d+$', val):
                    fields[key] = int(val)
                else:
                    fields[key] = val

    return fm_text, body, fields


def _write_frontmatter_field(filepath, key, value):
    """Update a single frontmatter field in a recipe file in-place."""
    with open(filepath, "r") as f:
        raw = f.read()

    parts = raw.split("---", 2)
    if len(parts) < 3:
        print(f"❌ Could not parse frontmatter in {filepath}")
        return False

    fm_text = parts[1]

    # Replace existing field or append
    pattern = re.compile(r'^(' + re.escape(key) + r':\s*)(.*)$', re.MULTILINE)
    if pattern.search(fm_text):
        new_fm = pattern.sub(rf'\g<1>{value}', fm_text)
    else:
        new_fm = fm_text.rstrip("\n") + f"\n{key}: {value}\n"

    with open(filepath, "w") as f:
        f.write("---" + new_fm + "---" + parts[2])
    return True


def _load_all_recipes():
    """Return list of dicts with slug, title, fields, path for every recipe."""
    recipes = []
    for fname in os.listdir(RECIPE_FOLDER):
        if not fname.endswith(".md"):
            continue
        slug = fname[:-3]
        path = os.path.join(RECIPE_FOLDER, fname)
        _, _, fields = _parse_frontmatter(path)
        if fields:
            fields["_slug"] = slug
            fields["_path"] = path
            recipes.append(fields)
    return recipes


def _load_meal_plan():
    """Return the current meal_plan.yml as a dict, or empty dict."""
    plan_path = os.path.join("_data", "meal_plan.yml")
    if not os.path.exists(plan_path):
        return {}
    if not HAS_YAML:
        print("⚠️  PyYAML not installed — install with: pip install pyyaml")
        return {}
    with open(plan_path, "r") as f:
        return yaml.safe_load(f) or {}


def _save_meal_plan(plan):
    plan_path = os.path.join("_data", "meal_plan.yml")
    if not HAS_YAML:
        print("⚠️  PyYAML not installed — install with: pip install pyyaml")
        return
    with open(plan_path, "w") as f:
        yaml.dump(plan, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


# ---------------------------------------------------------------------------
# Planning commands
# ---------------------------------------------------------------------------

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def cmd_plan():
    """Randomly suggest a week of dinners, avoiding recipes made in the past 2 weeks."""
    recipes = _load_all_recipes()
    cutoff = date.today() - timedelta(days=14)

    # Filter: exclude recently made, prefer dinner-ish recipes (not desserts/sides/dressings)
    skip_tags = {"dessert", "side-dish", "breakfast", "baking", "dressing"}

    eligible = []
    for r in recipes:
        # Skip if made recently
        lm = r.get("last_made", None)
        if lm and str(lm) not in ("~", "null", "None", ""):
            try:
                made_date = date.fromisoformat(str(lm))
                if made_date >= cutoff:
                    continue
            except ValueError:
                pass

        # Soft-filter out obvious non-mains by tag
        tags = r.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.replace(",", " ").split()]
        if any(t.lower() in skip_tags for t in tags):
            continue

        eligible.append(r)

    if len(eligible) < 7:
        print(f"ℹ️  Only {len(eligible)} eligible recipes — using all of them and repeating if needed.")
        pool = eligible * 2
    else:
        pool = eligible

    random.shuffle(pool)
    week_of = date.today() - timedelta(days=date.today().weekday())  # this Monday

    print(f"\n🗓  Suggested meal plan for the week of {week_of}:\n")
    plan = {"week_of": str(week_of)}
    for i, day in enumerate(DAYS):
        if i < len(pool):
            r = pool[i]
            slug = r["_slug"]
            title = r.get("title", slug)
            diff = r.get("difficulty", "")
            print(f"  {day.capitalize():10s}  {title}  [{diff}]")
            plan[day] = {"recipe": slug, "notes": None}
        else:
            print(f"  {day.capitalize():10s}  — no plan —")
            plan[day] = {"recipe": None, "notes": None}

    save = input("\n💾 Write this plan to _data/meal_plan.yml? (y/n): ").strip().lower()
    if save == "y":
        _save_meal_plan(plan)
        print("✅ Saved to _data/meal_plan.yml")
    else:
        print("↩️  Not saved.")


def cmd_grocery():
    """Aggregate ingredients from the current week's meal plan into a shopping list."""
    plan = _load_meal_plan()
    if not plan:
        print("❌ No meal plan found. Run --plan first.")
        return

    slugs = []
    for day in DAYS:
        day_data = plan.get(day, {})
        if isinstance(day_data, dict):
            slug = day_data.get("recipe")
        else:
            slug = day_data
        if slug and str(slug) not in ("~", "null", "None", ""):
            slugs.append(str(slug))

    if not slugs:
        print("❌ Meal plan has no recipes assigned.")
        return

    all_ingredients = []
    missing = []
    for slug in slugs:
        path = os.path.join(RECIPE_FOLDER, f"{slug}.md")
        if not os.path.exists(path):
            missing.append(slug)
            continue
        _, _, fields = _parse_frontmatter(path)
        ings = fields.get("ingredients", [])
        if isinstance(ings, list):
            all_ingredients.extend(ings)
        elif isinstance(ings, str):
            # Block scalar — split on newlines
            all_ingredients.extend([l.strip().lstrip("-").strip() for l in ings.splitlines() if l.strip()])

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for ing in all_ingredients:
        key = re.sub(r'[^a-z]', '', ing.lower())  # normalise for dedup
        if key not in seen:
            seen.add(key)
            unique.append(ing)

    if missing:
        print(f"⚠️  Recipe files not found: {', '.join(missing)}")

    print(f"\n🛒 Grocery list for the week of {plan.get('week_of', '?')} ({len(slugs)} recipes):\n")
    for item in unique:
        print(f"  - {item}")

    save = input("\n💾 Save to _data/grocery_list.yml? (y/n): ").strip().lower()
    if save == "y":
        if not HAS_YAML:
            print("⚠️  PyYAML not installed — install with: pip install pyyaml")
        else:
            out = {"week_of": str(plan.get("week_of", "")), "items": unique}
            out_path = os.path.join("_data", "grocery_list.yml")
            with open(out_path, "w") as f:
                yaml.dump(out, f, default_flow_style=False, allow_unicode=True)
            print(f"✅ Saved to {out_path}")


def cmd_update_made(slug):
    """Update the last_made field in a recipe file to today's date."""
    path = os.path.join(RECIPE_FOLDER, f"{slug}.md")
    if not os.path.exists(path):
        print(f"❌ Recipe not found: {path}")
        return

    today = date.today().isoformat()
    success = _write_frontmatter_field(path, "last_made", today)
    if success:
        print(f"✅ Updated last_made for '{slug}' to {today}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Chowdown recipe tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python chef_bot.py https://example.com/recipe     # import a recipe
  python chef_bot.py --plan                          # suggest a week of dinners
  python chef_bot.py --grocery                       # build a shopping list from the current plan
  python chef_bot.py --update-made hot-honey-chicken # log that you made a recipe tonight
""",
    )
    parser.add_argument("url", nargs="?", help="Recipe URL to import")
    parser.add_argument("--plan", action="store_true", help="Suggest a week of dinners")
    parser.add_argument("--grocery", action="store_true", help="Build grocery list from current meal plan")
    parser.add_argument("--update-made", metavar="SLUG", help="Set last_made to today for a recipe slug")

    args = parser.parse_args()

    if args.plan:
        cmd_plan()
    elif args.grocery:
        cmd_grocery()
    elif args.update_made:
        cmd_update_made(args.update_made)
    elif args.url:
        make_recipe(args.url)
    else:
        url = input("Enter recipe URL: ")
        make_recipe(url)