"""Skill entrypoint: print one short daily update notice, or stay quiet."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli_updates


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true', help='Return local diagnostics instead of a plain notice')
    args = parser.parse_args(argv)
    result = cli_updates.check_for_updates()
    if args.json:
        print(json.dumps({'cli_update': result}, ensure_ascii=False))
    elif result and result.get('notify') and result.get('notice'):
        print(result['notice'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
