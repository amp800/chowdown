"""
Update pantry staples or on_hand items in _data/pantry.yml.

Actions:
  add_staple     – add an item to the permanent pantry staples list
  remove_staple  – remove an item from pantry staples
  remove_on_hand – remove a single on_hand item
  clear_on_hand  – remove all on_hand items (fresh start after shopping)

Usage:
  python tools/pantry_update.py --action add_staple --item "olive oil"
  python tools/pantry_update.py --action remove_on_hand --item "baby spinach"
  python tools/pantry_update.py --action clear_on_hand
"""

import argparse
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("pyyaml not installed. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_pantry(path: Path) -> dict:
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    else:
        data = {}
    data.setdefault('staples', [])
    data.setdefault('on_hand', [])
    return data


def save_pantry(data: dict, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding='utf-8',
    )


def main():
    parser = argparse.ArgumentParser(description="Update _data/pantry.yml")
    parser.add_argument(
        '--action', required=True,
        choices=['add_staple', 'remove_staple', 'remove_on_hand', 'clear_on_hand'],
    )
    parser.add_argument('--item', default='', help='Item name (not required for clear_on_hand)')
    args = parser.parse_args()

    pantry_path = REPO_ROOT / '_data' / 'pantry.yml'
    data = load_pantry(pantry_path)
    item = args.item.strip().lower()

    if args.action == 'add_staple':
        if not item:
            print("ERROR: --item is required for add_staple", file=sys.stderr)
            sys.exit(1)
        existing = [s.lower() for s in data['staples']]
        if item not in existing:
            data['staples'].append(item)
            print(f"Added staple: {item}")
        else:
            print(f"Already a staple: {item}")

    elif args.action == 'remove_staple':
        if not item:
            print("ERROR: --item is required for remove_staple", file=sys.stderr)
            sys.exit(1)
        before = len(data['staples'])
        data['staples'] = [s for s in data['staples'] if s.lower() != item]
        if len(data['staples']) < before:
            print(f"Removed staple: {item}")
        else:
            print(f"Staple not found: {item}")

    elif args.action == 'remove_on_hand':
        if not item:
            print("ERROR: --item is required for remove_on_hand", file=sys.stderr)
            sys.exit(1)
        before = len(data['on_hand'])
        data['on_hand'] = [e for e in data['on_hand'] if e.get('item', '').lower() != item]
        if len(data['on_hand']) < before:
            print(f"Removed on_hand: {item}")
        else:
            print(f"On-hand item not found: {item}")

    elif args.action == 'clear_on_hand':
        count = len(data['on_hand'])
        data['on_hand'] = []
        print(f"Cleared {count} on_hand item(s)")

    save_pantry(data, pantry_path)
    print(f"Pantry saved — {len(data['staples'])} staple(s), {len(data['on_hand'])} on_hand item(s)")


if __name__ == '__main__':
    main()
