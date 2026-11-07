"""Bounded inline views over host-frozen, job-local project tool receipts.

No network, arbitrary file paths, CLI spool files, or ambient capabilities.
The host validates current scope/root identity before and after calling read().
"""
import hashlib
import json
from pathlib import Path
import re
import uuid

# Account for JSON escaping *inside* the MCP TextContent JSON string, leaving
# a conservative margin after real CLI trials spooled even 17 KiB outputs.
MAX_INLINE_BYTES = 6_000
DEFAULT_CHARS = 4_000
MAX_CHARS = 4_000
MAX_RECEIPT_BYTES = 1024 * 1024
MAX_AUDIT_BYTES = 8 * 1024 * 1024
TOOL_DEFINITION = {'name': 'result_read',
    'description': 'Read the next bounded text chunk of a frozen result from this task. Continue until next_offset is null before claiming the complete source was read. No network or arbitrary paths.',
    'inputSchema': {'type': 'object', 'properties': {
        'result_id': {'type': 'string', 'format': 'uuid'},
        'offset': {'type': 'integer', 'minimum': 0, 'default': 0},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': MAX_CHARS, 'default': DEFAULT_CHARS}},
        'required': ['result_id'], 'additionalProperties': False}}


def dumps(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def wire_bytes(result):
    # Measure both the inner tool JSON and the outer TextContent escaping.
    return len(dumps({'jsonrpc': '2.0', 'id': 'x' * 128, 'result': {'isError': not result.get('ok', False),
        'content': [{'type': 'text', 'text': dumps(result)}]}}).encode('utf-8'))


def validate_args(args):
    if not isinstance(args, dict) or set(args) - {'result_id', 'offset', 'limit'}:
        raise ValueError('Only result_id, offset and limit are accepted')
    result_id = args.get('result_id')
    try:
        valid = isinstance(result_id, str) and str(uuid.UUID(result_id)) == result_id
    except (ValueError, AttributeError):
        valid = False
    if not valid:
        raise ValueError('A canonical UUID result_id is required')
    offset, limit = args.get('offset', 0), args.get('limit', DEFAULT_CHARS)
    if type(offset) is not int or offset < 0 or offset > MAX_RECEIPT_BYTES:
        raise ValueError('Invalid result offset')
    if type(limit) is not int or not 1 <= limit <= MAX_CHARS:
        raise ValueError('Invalid result limit')
    return {'result_id': result_id, 'offset': offset, 'limit': limit}


def content(result):
    if result.get('tool') == 'public_page' and isinstance(result.get('text'), str):
        return result['text'], 'text/plain'
    units = result.get('units')
    if result.get('tool') == 'document_extract' and isinstance(units, list) and all(isinstance(unit, dict) and isinstance(unit.get('text'), str) for unit in units):
        return '\n\n'.join('[' + str(unit.get('kind', 'unit')) + ' ' + str(unit.get('index', i + 1)) + ']\n' + unit['text'] for i, unit in enumerate(units)), 'text/document-units'
    return dumps(result), 'application/json'


def page(result, receipt, *, offset=0, limit=DEFAULT_CHARS, reading=False, announcement=False):
    body, kind = content(result)
    if offset > len(body):
        raise ValueError('Result offset is outside source text')
    relative = Path(receipt['result_path'])
    result_id = relative.stem
    validate_args({'result_id': result_id, 'offset': offset, 'limit': limit})
    view = {'ok': True, 'tool': 'result_read' if reading else result.get('tool'),
        'source_tool': result.get('tool'), 'chunked': True, 'result_id': result_id,
        'content_format': kind, 'source_result_sha256': receipt['result_sha256'],
        'content_sha256': hashlib.sha256(body.encode('utf-8')).hexdigest(),
        'source_sha256': result.get('source_sha256'), 'offset': offset, 'total_chars': len(body),
        'read_instructions': 'Call result_read with this result_id and next_offset until next_offset is null. next_offset is a character cursor in this frozen result; next_start is a separate document-unit cursor. The announcement contains no source text.'}
    # Preserve bounded citation/extraction context, never private receipt paths.
    for key in ('url', 'status', 'content_type', 'fetched_at', 'format', 'total_units', 'next_start', 'truncated'):
        value = result.get(key)
        if isinstance(value, str):
            view[key] = value[:512 if key == 'url' else 128]
        elif type(value) in (int, bool) or value is None:
            if key in result:
                view[key] = value
    units = result.get('units')
    if result.get('tool') == 'document_extract' and isinstance(units, list) and units:
        for name, unit in (('unit_start', units[0]), ('unit_end', units[-1])):
            if isinstance(unit, dict) and type(unit.get('index')) is int:
                view[name] = unit['index']
    def candidate(count):
        end = offset + count
        return dict(view, text=body[offset:end], next_offset=end if end < len(body) else None)
    low, high = 0, min(limit, len(body) - offset)
    if wire_bytes(candidate(0)) > MAX_INLINE_BYTES:
        raise ValueError('Result metadata exceeds inline limit')
    if announcement:
        return candidate(0)
    while low < high:
        middle = (low + high + 1) // 2
        if wire_bytes(candidate(middle)) <= MAX_INLINE_BYTES:
            low = middle
        else:
            high = middle - 1
    if low == 0 and offset < len(body):
        raise ValueError('No source text fits inline limit')
    return candidate(low)


def bounded(result, receipt):
    if wire_bytes(result) <= MAX_INLINE_BYTES:
        return result  # Preserve direct small-tool semantics and receipt equality.
    if result.get('ok') is not True:
        refusal = {'ok': False, 'error': str(result.get('error', 'Project tool failed'))[:2000]}
        while wire_bytes(refusal) > MAX_INLINE_BYTES:
            refusal['error'] = refusal['error'][:len(refusal['error']) // 2]
        return refusal
    # Always expose the cursor before any body text can trigger native spooling.
    return page(result, receipt, announcement=True)


def read(policy, args, rootid, read_file):
    """Load only a UUID named in this job's durable audit and verify raw hash.

    read_file must be the host's bounded, no-symlink, no-hardlink dirfd reader.
    Both audit and receipt remain inaccessible as general model file inputs.
    """
    args = validate_args(args)
    relative = 'project-tools-results/' + args['result_id'] + '.json'
    job = Path(policy['job_dir'])
    rows = []
    for line in read_file(job / 'project-tools-audit.jsonl', MAX_AUDIT_BYTES).splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError('Invalid result audit')
        if row.get('result_path') == relative:
            rows.append(row)
    if len(rows) != 1:
        raise ValueError('Result is not a unique receipt of this task')
    row = rows[0]
    summary = row.get('summary')
    if row.get('root_conversation_id') != rootid or row.get('tool') == 'result_read' or not isinstance(summary, dict) or summary.get('ok') is not True:
        raise ValueError('Result is not an original successful receipt of this conversation')
    if not isinstance(row.get('result_sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', row['result_sha256']) or type(row.get('result_bytes')) is not int:
        raise ValueError('Invalid result receipt hash')
    raw = read_file(job / relative, MAX_RECEIPT_BYTES)
    if len(raw) != row['result_bytes'] or hashlib.sha256(raw).hexdigest() != row['result_sha256']:
        raise ValueError('Frozen result receipt changed')
    result = json.loads(raw)
    if not isinstance(result, dict) or result.get('ok') is not True or result.get('tool') != row.get('tool') or result.get('source_sha256') != row.get('source_sha256'):
        raise ValueError('Result receipt disagrees with audit')
    return page(result, row, offset=args['offset'], limit=args['limit'], reading=True)
