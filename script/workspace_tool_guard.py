"""Official native PreToolUse/PostToolUse entry point; never fail open."""
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from workspace_guard import gate, post, lifecycle


def main():
    try:
        policy_path = Path(sys.argv[1]).resolve()
        if policy_path.is_dir():
            policy_path /= 'workspace-policy.json'
        policy = json.loads(policy_path.read_text())
        payload = json.load(sys.stdin)
        if len(sys.argv)>3 and sys.argv[2]=='lifecycle':
            result=lifecycle(policy,payload,sys.argv[3])
        else:
            result = post(policy, payload) if len(sys.argv) > 2 and sys.argv[2] == 'post' else gate(policy, payload)
    except Exception as error:
        result = {'decision': 'deny', 'reason': 'Cannot load workspace policy/payload: ' + type(error).__name__}
    print(json.dumps(result))


if __name__ == '__main__':
    main()
