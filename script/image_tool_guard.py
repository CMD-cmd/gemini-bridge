"""Workspace PreToolUse hook: explicit image budget and selected-file access."""
import fcntl
import json
from pathlib import Path
import re
import sys
import time
import hashlib

def gate(job, value):
    policy = json.loads((job / 'image-permissions.json').read_text())
    call = value.get('toolCall', {})
    name, args = call.get('name'), call.get('args', {})
    conversation = value.get('conversationId', '')
    native = Path.home() / '.gemini/antigravity-cli/brain' / conversation
    if not re.fullmatch(r'[0-9a-f-]{36}', conversation):
        raise ValueError('Missing conversation identity')
    if value.get('modelName') != policy['model']:
        raise ValueError('Unexpected orchestration model')
    if str(job) not in value.get('workspacePaths', []) or value.get('artifactDirectoryPath') != str(native):
        raise ValueError('Unexpected workspace or artifact directory')
    if name == 'generate_image':
        settings_path = Path.home() / '.gemini/antigravity-cli/settings.json'
        settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
        if settings.get('modelProvider') not in (None,''):
            raise ValueError('Existing account route is required; API and other providers refused')
        image_name = args.get('ImageName', '')
        match = re.fullmatch(r'bridge_image_([1-8])_try_([12])', image_name) if isinstance(image_name,str) else None
        if not match:
            raise ValueError('ImageName must identify an image slot and attempt')
        slot, attempt = map(int,match.groups())
        count = policy.get('image_count',policy['max_attempts'])
        retry_limit = policy.get('retry_limit',0)
        if slot>count or attempt>1+retry_limit:
            raise ValueError('Image slot or retry is outside task scope')
        references = args.get('ImagePaths', [])
        if not isinstance(references, list) or any(p not in policy['image_paths'] for p in references):
            raise ValueError('Image references outside selected snapshots')
        if not isinstance(args.get('Prompt'), str) or not 1 <= len(args['Prompt']) <= 16000:
            raise ValueError('Image prompt exceeds scope')
        # Reserve before running the tool; even a failed attempt consumes budget.
        with (job / 'image-budget.lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            counter = job / 'image-budget.json'
            budget = json.loads(counter.read_text()) if counter.exists() else {'attempts':[], 'conversation_id':conversation}
            if budget['conversation_id'] != conversation or len(budget['attempts']) >= count*(1+retry_limit):
                raise ValueError('Requested image attempt budget exhausted; no automatic retry')
            if any(x['image_name']==image_name for x in budget['attempts']):
                raise ValueError('ImageName already used; no replacement or retry')
            results = job / 'image-tool-results.jsonl'
            completed = {x['image_name']:x for x in (json.loads(x) for x in results.read_text().splitlines())} if results.exists() else {}
            if any(x['image_name'] not in completed for x in budget['attempts']):
                raise ValueError('Previous image attempt has no completion record; do not start another')
            if any(x['attempt']==2 and completed[x['image_name']].get('error') for x in budget['attempts']):
                raise ValueError('Second attempt failed; stop image task')
            same = [x for x in budget['attempts'] if x['slot']==slot]
            if attempt==1 and same:
                raise ValueError('Image slot already attempted')
            if attempt==2 and (len(same)!=1 or not completed[same[0]['image_name']].get('error')):
                raise ValueError('Retry requires one explicitly failed first attempt')
            args_fingerprint=hashlib.sha256(json.dumps({'Prompt':args['Prompt'],'ImagePaths':references},sort_keys=True).encode()).hexdigest()
            if attempt==2 and same[0].get('args_fingerprint')!=args_fingerprint:
                raise ValueError('Retry must use the same image prompt and selected references')
            # A reported failure is retried only in its own slot before any new
            # slot; successful images can never be regenerated as a retry.
            pending = [x for x in budget['attempts'] if completed[x['image_name']].get('error') and
                       not any(y['slot']==x['slot'] and y['attempt']==2 for y in budget['attempts'])]
            if pending and (attempt!=2 or slot!=pending[-1]['slot']):
                raise ValueError('Only the failed image slot may be retried once')
            budget['attempts'].append({'slot':slot,'attempt':attempt,'image_name':image_name,'args_fingerprint':args_fingerprint})
            counter.write_text(json.dumps(budget))
            counter.chmod(0o600)
        return {'image_name':image_name, 'slot':slot, 'attempt':attempt}
    if name in ('view_file','read_file'):
        p = args.get('AbsolutePath' if name=='view_file' else 'Path')
        path = Path(p) if isinstance(p, str) else Path('invalid')
        if p in policy['image_paths']:
            return {}
        if path.is_absolute() and path.parent == native and path.suffix.lower() in ('.png','.jpg','.jpeg','.webp') and '..' not in path.parts and not path.is_symlink():
            return {}
        raise ValueError('Only selected or newly generated images may be viewed')
    raise ValueError('Tool not authorized for image task: ' + str(name))

def main():
    job = Path(sys.argv[1]).resolve()
    value = json.load(sys.stdin)
    if len(sys.argv)>2 and sys.argv[2]=='post':
        with (job / 'image-tool-results.jsonl').open('a') as stream:
            stream.write(json.dumps({'step_index':value.get('stepIdx'), 'error':value.get('error'),
                        'image_name':value.get('toolCall',{}).get('args',{}).get('ImageName'),
                        'conversation_id':value.get('conversationId')})+'\n')
        (job / 'image-tool-results.jsonl').chmod(0o600)
        print('{}')
        return
    extra = {}
    try:
        extra = gate(job, value)
        decision, reason = 'allow', 'Scoped image task permission'
    except (ValueError, OSError, TypeError, KeyError) as error:
        decision, reason = 'deny', str(error)
    record = dict(at=time.time(), tool=value.get('toolCall',{}).get('name'), decision=decision,
                  reason=reason, conversation_id=value.get('conversationId'),
                  artifact_directory=value.get('artifactDirectoryPath'), **extra)
    with (job / 'image-tool-audit.jsonl').open('a') as stream:
        stream.write(json.dumps(record) + '\n')
    (job / 'image-tool-audit.jsonl').chmod(0o600)
    print(json.dumps({'decision':decision, 'reason':reason}))

if __name__ == '__main__':
    main()
