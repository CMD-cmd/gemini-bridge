"""Collect trusted preview captures separately from model-authored files."""
import hashlib
import json
from pathlib import Path
import re
import struct

from image_artifacts import no_symlinks


def _read(path, maximum):
    path=no_symlinks(path)
    if not path.is_file() or path.stat().st_nlink!=1 or path.stat().st_size>maximum:
        raise ValueError('Preview evidence must be a bounded regular file')
    with path.open('rb') as stream:data=stream.read(maximum+1)
    if len(data)>maximum:raise ValueError('Preview evidence exceeds byte limit')
    return data


def collect(job, records, audit, deliveries):
    job=Path(job).resolve();summaries=[]
    for record in records:
        preview=record.get('preview')
        if not preview:continue
        rid=record.get('record_id')
        if not isinstance(rid,str) or not re.fullmatch(r'[0-9a-f]{64}',rid):
            raise ValueError('Invalid preview record identity')
        directory=job/'command-results'/rid
        expected=directory/'manifest.json'
        if preview.get('manifest_path')!=str(expected):raise ValueError('Preview manifest escaped task record')
        manifest=json.loads(_read(expected,2*1024*1024))
        if (manifest.get('record_id')!=rid or manifest.get('args_sha256')!=record.get('args_sha256')
                or record.get('runner_exit_code')!=0 or record.get('logs_complete') is not True
                or manifest.get('cleanup',{}).get('browser_closed') is not True
                or manifest.get('cleanup',{}).get('server_closed') is not True):
            raise ValueError('Preview rendering or cleanup is not verified')
        screenshots=manifest.get('screenshots')
        if not isinstance(screenshots,list) or not 1<=len(screenshots)<=4:
            raise ValueError('Invalid preview screenshot manifest')
        shots=[];names=set()
        for shot in screenshots:
            name=shot.get('name')
            if name not in ('desktop.png','mobile.png') or name in names:
                raise ValueError('Unknown or duplicate screenshot')
            names.add(name);source=directory/name
            if shot.get('path')!=str(source):raise ValueError('Screenshot escaped preview record')
            data=_read(source,16*1024*1024);digest=hashlib.sha256(data).hexdigest()
            if len(data)!=shot.get('bytes') or digest!=shot.get('sha256'):
                raise ValueError('Preview screenshot changed after capture')
            if len(data)<24 or data[:8]!=b'\x89PNG\r\n\x1a\n' or data[12:16]!=b'IHDR':
                raise ValueError('Preview screenshot is not PNG')
            width,height=struct.unpack('>II',data[16:24])
            if not 0<width<=8192 or not 0<height<=12000 or width*height>32000000:
                raise ValueError('Preview screenshot dimensions exceed limits')
            pre=[r for r in audit if r.get('tool')=='view_file' and r.get('kind')=='pre'
                 and r.get('decision')=='auto_approve' and r.get('path')==str(source)
                 and r.get('file_sha256')==digest and r.get('file_size')==len(data)]
            completed=any(any(p.get('conversationId')==r.get('conversationId')
                              and p.get('stepIdx')==r.get('stepIdx')
                              and p.get('args_sha256')==r.get('args_sha256') for p in pre)
                          for r in audit if r.get('tool')=='view_file' and r.get('kind')=='post'
                          and r.get('decision')=='post' and r.get('path')==str(source))
            dest=job/'deliverables'/'previews'/rid/name;dest.parent.mkdir(parents=True,exist_ok=True)
            if dest.exists():
                if _read(dest,16*1024*1024)!=data:raise ValueError('Frozen preview delivery changed')
            else:dest.write_bytes(data);dest.chmod(0o400)
            deliveries.append({'id':str(len(deliveries)+1),'name':'preview-'+rid[:8]+'-'+name,
                               'path':str(dest.relative_to(job)),'bytes':len(data),'sha256':digest,
                               'source':'trusted_preview','preview_record_id':rid})
            shots.append({'name':name,'sha256':digest,'width':width,'height':height,
                          'read_requested':bool(pre),'read_completion_observed':completed})
        summaries.append({'record_id':rid,'entry':manifest.get('entry'),
                          'entry_sha256':manifest.get('entry_sha256'),
                          'rendered':True,'screenshots':shots,
                          'evidence':'Trusted browser capture; view_file request/completion are observations, not content acceptance'})
    if summaries:
        (job/'deliverables.json').write_text(json.dumps(deliveries,ensure_ascii=False,indent=2))
    return summaries
