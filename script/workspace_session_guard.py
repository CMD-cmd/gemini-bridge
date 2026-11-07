"""Fixed native hook for a host-owned stable provider session workspace."""
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from session_workspace import handle


def main():
    try:
        payload = json.load(sys.stdin)
        mode = sys.argv[2] if len(sys.argv) > 2 else 'pre'
        event = sys.argv[3] if mode == 'lifecycle' and len(sys.argv) > 3 else None
        result = handle(sys.argv[1], payload, mode=mode, event=event)
    except Exception as error:
        result = {'decision': 'deny', 'reason': 'Invalid workspace session lease: ' + type(error).__name__}
    print(json.dumps(result))


if __name__ == '__main__': main()
