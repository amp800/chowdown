"""
Parse grocery store receipt emails and update _data/pantry.yml on_hand section.

Supported stores: freshdirect, wholefoods, auto (detects from content)

Usage:
  python tools/parse_receipt.py --store freshdirect --email_file receipt.txt
  python tools/parse_receipt.py --store auto --email_file receipt.txt
"""

import argparse
import re
import sys
from datetime import date
from pathlib import Path

try:
    import yaml
except ImportError:
    print("pyyaml not installed. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent

PRICE_RE = re.compile(r'\$\s*\d+[\.,]\d{2}')

# Lines to always skip regardless of store
SKIP_RE = re.compile(
    r'^('
    r'subtotal|total|tax|delivery|tip|savings|coupon|promo|discount|'
    r'order (number|#|confirmation|date|summary)|estimated|'
    r'dear |hello |hi |thank|your order|placed on|'
    r'arriving|scheduled|address|payment|card ending|'
    r'view order|manage|cancel|unsubscribe|privacy|help|contact|'
    r'shop now|reorder|freshdirect|whole foods|amazon|copyright|'
    r'subtotal|checkout|bag|cart|refund|credit|gift'
    r')',
    re.IGNORECASE,
)

# Size/weight patterns: "16oz", "1.5 lb", "750 ml", "12 ct", "6 pack"
_SIZE_RE = re.compile(
    r'\b\d+(\.\d+)?\s*(fl\.?\s*)?(oz|lb|lbs|g|kg|ml|l\b|ct|count|pk|pack|pieces?)\b',
    re.IGNORECASE,
)
# "per lb", "per oz", etc.
_PER_UNIT_RE = re.compile(r'\bper\s+\w+', re.IGNORECASE)
# Container/packaging words that add no food identity
_CONTAINER_RE = re.compile(
    r'\b(package|container|carton|bunch|bundle|tray|sleeve|wrap|roll|sheet)\b',
    re.IGNORECASE,
)
# Pure marketing/quality words that carry no ingredient meaning
_MARKETING_RE = re.compile(
    r'\b(premium|select|choice|artisan|craft|homestyle|original|classic|traditional|'
    r'all[- ]natural|non[- ]?gmo|usda|grade\s+a|store\s+brand|private\s+label)\b',
    re.IGNORECASE,
)
# Store brand prefixes
_STORE_BRAND_RE = re.compile(
    r'^(365 by whole foods market|365\b|amazon fresh\b|whole foods market\b|'
    r'freshdirect\b|kirkland\b|trader joe\'?s?\b|good\s+&\s+gather\b)',
    re.IGNORECASE,
)


def clean_item_name(raw: str) -> str:
    """Strip everything except the core ingredient name from a receipt line."""
    # Remove price patterns
    raw = PRICE_RE.sub('', raw)
    # Cut at first comma — receipts format as "Item Name, Size/Detail"
    raw = raw.split(',')[0]
    # Remove parenthetical content: (approx 1 lb), (16 oz), etc.
    raw = re.sub(r'\([^)]*\)', '', raw)
    # Remove size/weight
    raw = _SIZE_RE.sub('', raw)
    # Remove "per lb" style
    raw = _PER_UNIT_RE.sub('', raw)
    # Remove container words
    raw = _CONTAINER_RE.sub('', raw)
    # Remove marketing qualifiers
    raw = _MARKETING_RE.sub('', raw)
    # Remove store brand prefixes
    raw = _STORE_BRAND_RE.sub('', raw)
    # Remove "Qty: N" or "qty N"
    raw = re.sub(r'\bqty\s*:?\s*\d+', '', raw, flags=re.IGNORECASE)
    # Remove "N @" patterns
    raw = re.sub(r'\d+\s*@', '', raw)
    # Remove trailing "x N" or "xN"
    raw = re.sub(r'\bx\s*\d+\b', '', raw, flags=re.IGNORECASE)
    # Remove leading quantity "N x " or just "N "
    raw = re.sub(r'^\s*\d+\s+x?\s+', '', raw)
    # Remove bullet points / dashes at start
    raw = re.sub(r'^[•·\-\*]\s*', '', raw)
    # Collapse whitespace and strip punctuation noise
    raw = re.sub(r'\s+', ' ', raw).strip().strip('.,;:|-/')
    return raw


def is_valid_item(name: str) -> bool:
    """Return True if the cleaned string looks like a real product name."""
    if not name or len(name) < 3:
        return False
    # Reject pure numbers/prices
    if re.match(r'^[\$\d\.\s]+$', name):
        return False
    # Reject very short all-caps abbreviations (like "EA", "LB")
    if re.match(r'^[A-Z]{1,3}$', name):
        return False
    return True


def parse_freshdirect(text: str) -> list:
    """
    Extract item names from a FreshDirect order confirmation email (plain text).

    FreshDirect emails list items as:
        Item Name, Size          Qty    $X.XX
    or sometimes just item + price on the same line.
    """
    items = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if SKIP_RE.match(line):
            continue
        # FreshDirect item lines almost always have a price on the same line
        if PRICE_RE.search(line):
            name = clean_item_name(line)
            if is_valid_item(name):
                items.append(name.lower())
    return items


def parse_wholefoods(text: str) -> list:
    """
    Extract item names from a Whole Foods / Amazon Fresh order email (plain text).

    Two common layouts:
      1. Bullet list:  "• Item Name - $X.XX"
      2. Item on one line, "N @ $X.XX" on the next line
      3. "Item Name  $X.XX" inline
    """
    items = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if SKIP_RE.match(line):
            i += 1
            continue

        # Layout 1 – bullet points
        if line.startswith(('•', '·')):
            name = clean_item_name(line)
            if is_valid_item(name):
                items.append(name.lower())
            i += 1
            continue

        # Layout 2 – next line is "N @ $X.XX"
        if i + 1 < len(lines):
            next_line = lines[i + 1].strip()
            if re.match(r'^\d+\s*@\s*\$', next_line):
                name = clean_item_name(line)
                if is_valid_item(name):
                    items.append(name.lower())
                i += 2
                continue

        # Layout 3 – price inline
        if PRICE_RE.search(line):
            name = clean_item_name(line)
            if is_valid_item(name):
                items.append(name.lower())

        i += 1
    return items


def parse_generic(text: str) -> list:
    """Fallback: grab any line that contains a price and looks like a product."""
    items = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if SKIP_RE.match(line):
            continue
        if PRICE_RE.search(line):
            name = clean_item_name(line)
            if is_valid_item(name):
                items.append(name.lower())
    return items


def detect_store(text: str) -> str:
    text_lower = text.lower()
    if 'freshdirect' in text_lower:
        return 'freshdirect'
    if 'whole foods' in text_lower or 'wholefoods' in text_lower or 'wholefoodsmarket' in text_lower:
        return 'wholefoods'
    return 'generic'


def update_pantry(items: list, store: str, pantry_path: Path):
    """Merge items into the on_hand section of pantry.yml."""
    if pantry_path.exists():
        data = yaml.safe_load(pantry_path.read_text(encoding='utf-8')) or {}
    else:
        data = {}

    data.setdefault('staples', [])
    data.setdefault('on_hand', [])

    today = str(date.today())
    existing = {entry['item'] for entry in data['on_hand']}

    added = 0
    for item in items:
        if item not in existing:
            data['on_hand'].append({
                'item': item,
                'source': store,
                'added': today,
            })
            existing.add(item)
            added += 1

    pantry_path.parent.mkdir(parents=True, exist_ok=True)
    pantry_path.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding='utf-8',
    )
    print(f"Added {added} new item(s) from {store} receipt (skipped {len(items) - added} duplicates).")
    print(f"Total on_hand: {len(data['on_hand'])}")


def main():
    parser = argparse.ArgumentParser(description="Parse a grocery receipt email and update _data/pantry.yml")
    parser.add_argument(
        '--store', required=True,
        choices=['freshdirect', 'wholefoods', 'auto', 'generic'],
        help='Store that sent the receipt email',
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--email_text', help='The raw email text (pass as string)')
    group.add_argument('--email_file', help='Path to a file containing the email text')
    args = parser.parse_args()

    if args.email_file:
        text = Path(args.email_file).read_text(encoding='utf-8')
    else:
        text = args.email_text

    store = args.store
    if store == 'auto':
        store = detect_store(text)
        print(f"Auto-detected store: {store}")

    if store == 'freshdirect':
        items = parse_freshdirect(text)
    elif store == 'wholefoods':
        items = parse_wholefoods(text)
    else:
        items = parse_generic(text)

    print(f"Parsed {len(items)} item(s) from {store} receipt:")
    for item in items:
        print(f"  - {item}")

    if not items:
        print("WARNING: No items found. The email format may not be recognized.", file=sys.stderr)
        sys.exit(0)

    pantry_path = REPO_ROOT / '_data' / 'pantry.yml'
    update_pantry(items, store, pantry_path)


if __name__ == '__main__':
    main()
