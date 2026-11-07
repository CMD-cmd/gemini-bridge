"""Freeze host-produced MCP replies and summarize their actual use."""
import hashlib
import json
from pathlib import Path
from project_tools import _read


def collect(job,deliveries):
    job=Path(job).resolve();path=job/'project-tools-audit.jsonl'
    if not path.exists():return {'calls':0,'succeeded':0,'refused':0,'records':[]}
    rows=[json.loads(x) for x in _read(path,8*1024*1024).splitlines() if x.strip()]
    records=[]
    for row in rows:
        relative=Path(row['result_path'])
        if relative.is_absolute() or len(relative.parts)!=2 or relative.parts[0]!='project-tools-results' or '..' in relative.parts:
            raise ValueError('Project reply escaped task result directory')
        data=_read(job/relative,1024*1024)
        if len(data)!=row['result_bytes'] or hashlib.sha256(data).hexdigest()!=row['result_sha256']:
            raise ValueError('Project tool reply changed')
        result=json.loads(data)
        if result.get('source_sha256')!=row.get('source_sha256'):
            raise ValueError('Project source evidence differs')
        if result.get('ok'):
            dest=job/'deliverables'/'project-tools'/relative.name;dest.parent.mkdir(parents=True,exist_ok=True)
            if not dest.exists():dest.write_bytes(data);dest.chmod(0o400)
            elif _read(dest,1024*1024)!=data:raise ValueError('Frozen project reply changed')
            deliveries.append({'id':str(len(deliveries)+1),'name':row['tool']+'-'+relative.name,
                'path':str(dest.relative_to(job)),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),
                'source':'trusted_project_tool'})
        records.append({'tool':row['tool'],'ok':result.get('ok') is True,'source_sha256':row.get('source_sha256'),
                        'result_sha256':row['result_sha256'],'root_conversation_id':row.get('root_conversation_id')})
    (job/'deliverables.json').write_text(json.dumps(deliveries,ensure_ascii=False,indent=2))
    return {'calls':len(records),'succeeded':sum(x['ok'] for x in records),
            'refused':sum(not x['ok'] for x in records),'records':records}
