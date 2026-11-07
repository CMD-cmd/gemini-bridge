"""Bounded local document text extraction; no OCR, network, or source writes."""
from __future__ import annotations
import io
import sys
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import shutil
import subprocess
import time
import zipfile
import xml.etree.ElementTree as ET

MAX_UNPACKED = 64 * 1024 * 1024
MAX_ENTRY = 8 * 1024 * 1024
MAX_PDF_OUTPUT = 8 * 1024 * 1024
PDF_TIMEOUT = 20
BUNDLED_PYTHON = os.environ.get('GEMINI_BRIDGE_DOCUMENT_PYTHON', sys.executable)
_PDF_SCRIPT = """import io, sys
from pypdf import PdfReader
reader = PdfReader(io.BytesIO(sys.stdin.buffer.read()))
if reader.is_encrypted:
    raise ValueError('Encrypted/password PDFs unsupported, including empty-password PDFs')
for page in reader.pages:
    sys.stdout.write((page.extract_text() or '') + '\f')
    sys.stdout.flush()
"""
TOOL_DEFINITIONS = [{'name': 'document_extract',
    'description': 'Read local PDF pages, DOCX paragraphs, PPTX slides or UTF-8 text lines. No OCR or external resources.',
    'inputSchema': {'type': 'object', 'properties': {
        'path': {'type': 'string', 'pattern': '^/'},
        'start': {'type': 'integer', 'minimum': 1, 'default': 1},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 500, 'default': 30},
        'max_chars': {'type': 'integer', 'minimum': 1, 'maximum': 60000, 'default': 60000}},
        'required': ['path'], 'additionalProperties': False}}]


def validate_args(args):
    if not isinstance(args, dict) or set(args) - {'path', 'start', 'limit', 'max_chars'}:
        raise ValueError('Invalid document_extract argument keys')
    path = args.get('path')
    if (not isinstance(path, str) or not Path(path).is_absolute() or '\x00' in path
            or '..' in Path(path).parts):
        raise ValueError('An absolute document path without traversal is required')
    result = dict(path=path, start=args.get('start', 1), limit=args.get('limit', 30), max_chars=args.get('max_chars', 60000))
    for name, maximum in (('start', 10000000), ('limit', 500), ('max_chars', 60000)):
        if type(result[name]) is not int or not 1 <= result[name] <= maximum:
            raise ValueError('Invalid document_extract ' + name)
    return result


def _xml(data):
    # NUL removal also catches UTF-16/32 declarations before ElementTree sees them.
    probe = data.replace(b'\x00', b'').upper()
    if b'<!DOCTYPE' in probe or b'<!ENTITY' in probe:
        raise ValueError('Document XML declarations/entities are forbidden')
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError('Malformed document XML') from exc


def _archive(data, check):
    check()
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError('Malformed Office ZIP document') from exc
    total, names = 0, set()
    try:
        for entry in archive.infolist():
            check()
            name = entry.filename
            parts = PurePosixPath(name).parts
            if (not name or '\\' in name or name.startswith('/') or '..' in parts
                    or ':' in name or name in names or ((entry.external_attr >> 16) & 0o170000) == 0o120000):
                raise ValueError('Unsafe or duplicate Office ZIP member')
            names.add(name)
            total += entry.file_size
            if entry.file_size > MAX_ENTRY or total > MAX_UNPACKED or entry.flag_bits & 1:
                raise ValueError('Office ZIP size limit exceeded or encrypted member')
            if 'vbaproject' in name.lower() or name.lower().endswith(('.vba', '.vbs')):
                raise ValueError('Macro documents are unsupported')
        check()
        return archive
    except BaseException:
        archive.close()
        raise


def _member(archive, name, check):
    check()
    try:
        with archive.open(name) as source:
            data = source.read(MAX_ENTRY + 1)
    except (KeyError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ValueError('Missing or unreadable Office document member: ' + name) from exc
    check()
    if len(data) > MAX_ENTRY:
        raise ValueError('Office ZIP entry exceeds size limit')
    return _xml(data)


def _text_nodes(node):
    pieces = []
    for child in node.iter():
        kind = child.tag.rsplit('}', 1)[-1]
        if kind == 't':
            pieces.append(child.text or '')
        elif kind == 'tab':
            pieces.append('\t')
        elif kind in ('br', 'cr'):
            pieces.append('\n')
    return ''.join(pieces)


def _office(data, extension, check):
    with _archive(data, check) as archive:
        if extension == 'docx':
            document = _member(archive, 'word/document.xml', check)
            return [{'index': i, 'kind': 'paragraph', 'text': _text_nodes(p)} for i, p in enumerate(
                document.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p'), 1)]
        presentation = _member(archive, 'ppt/presentation.xml', check)
        relationships = _member(archive, 'ppt/_rels/presentation.xml.rels', check)
        rels = {}
        for rel in relationships:
            if rel.get('TargetMode', '').lower() == 'external':
                continue
            target = rel.get('Target', '')
            if (not target or target.startswith('/') or '..' in PurePosixPath(target).parts
                    or '\\' in target or ':' in target):
                raise ValueError('Unsafe presentation relationship')
            rels[rel.get('Id')] = 'ppt/' + target
        result = []
        for slide in presentation.iter('{http://schemas.openxmlformats.org/presentationml/2006/main}sldId'):
            check()
            rid = slide.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
            target = rels.get(rid)
            if not target or not re.fullmatch(r'ppt/slides/slide[0-9]+\.xml', target):
                raise ValueError('Unsupported or external presentation slide relationship')
            xml = _member(archive, target, check)
            paragraphs = xml.iter('{http://schemas.openxmlformats.org/drawingml/2006/main}p')
            result.append({'index': len(result) + 1, 'kind': 'slide', 'text': '\n'.join(_text_nodes(p) for p in paragraphs)})
        return result


def _pdftotext():
    return shutil.which('pdftotext') or next((p for p in (
        '/opt/homebrew/bin/pdftotext', '/usr/local/bin/pdftotext', '/usr/bin/pdftotext') if os.path.isfile(p) and os.access(p, os.X_OK)), None)


def _pdf(data, check):
    if not data.startswith(b'%PDF-'):
        raise ValueError('Malformed PDF header')
    if re.search(rb'/Encrypt\b', data):
        raise ValueError('Encrypted/password PDFs are unsupported, including empty-password PDFs')
    binary = _pdftotext()
    if binary:
        command = [binary, '-enc', 'UTF-8', '-', '-']
    elif os.path.isfile(BUNDLED_PYTHON) and os.access(BUNDLED_PYTHON, os.X_OK):
        command = [BUNDLED_PYTHON, '-I', '-B', '-c', _PDF_SCRIPT]
    else:
        raise ValueError('PDF extraction requires an existing pdftotext or verified bundled pypdf runtime')
    sandbox = '/usr/bin/sandbox-exec'
    if os.path.isfile(sandbox) and os.access(sandbox, os.X_OK):
        command = [sandbox, '-p', '(version 1)(allow default)(deny network*)(deny file-write*)'] + command
    check()
    process = subprocess.Popen(command, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
    output, error, position = bytearray(), bytearray(), 0
    deadline = time.monotonic() + PDF_TIMEOUT
    try:
        with selectors.DefaultSelector() as selector:
            for stream, direction in ((process.stdin, selectors.EVENT_WRITE),
                                      (process.stdout, selectors.EVENT_READ), (process.stderr, selectors.EVENT_READ)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, direction)
            while selector.get_map():
                check()
                if time.monotonic() >= deadline:
                    raise ValueError('PDF text extraction timed out')
                for key, mask in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                    check()
                    stream = key.fileobj
                    if stream is process.stdin:
                        if position < len(data):
                            try:
                                position += os.write(stream.fileno(), data[position:position + 65536])
                            except BrokenPipeError:
                                position = len(data)
                        if position == len(data):
                            selector.unregister(stream)
                            stream.close()
                    else:
                        chunk = os.read(stream.fileno(), 65536)
                        check()
                        if not chunk:
                            selector.unregister(stream)
                            stream.close()
                        else:
                            target = output if stream is process.stdout else error
                            if len(target) + len(chunk) > (MAX_PDF_OUTPUT if target is output else 65536):
                                raise ValueError('PDF extraction output exceeds bounded limit')
                            target.extend(chunk)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError('PDF text extraction timed out')
            try:
                code = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                raise ValueError('PDF text extraction timed out') from exc
        check()
        if code:
            raise ValueError('PDF extraction failed (malformed, password-protected or unsupported PDF): ' + error.decode('utf-8', 'replace')[:1000])
        pages = output.decode('utf-8').split('\f')
        if pages and pages[-1] == '':
            pages.pop()
        return [{'index': i, 'kind': 'page', 'text': page, 'text_empty': not page.strip()} for i, page in enumerate(pages, 1)]
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream and not stream.closed:
                stream.close()


def extract(data, path, args, check_authorized):
    args = validate_args(args)
    if args['path'] != path or not isinstance(data, bytes):
        raise ValueError('Document extraction must use the verified source path and bytes')
    check_authorized()
    extension = Path(path).suffix.lower().lstrip('.')
    if extension in ('docx', 'pptx'):
        units = _office(data, extension, check_authorized)
    elif extension == 'pdf':
        units = _pdf(data, check_authorized)
    elif extension in ('txt', 'md'):
        try:
            text = data.decode('utf-8-sig')
        except UnicodeDecodeError as exc:
            raise ValueError('Text documents must be valid UTF-8') from exc
        check_authorized()
        units = [{'index': i, 'kind': 'line', 'text': line} for i, line in enumerate(text.splitlines(), 1)]
    else:
        raise ValueError('Unsupported document format; use PDF, DOCX, PPTX, TXT or MD')
    check_authorized()
    selected, used, next_start = [], 0, None
    start = args['start'] - 1
    for unit in units[start:start + args['limit']]:
        check_authorized()
        remaining = args['max_chars'] - used
        if len(unit['text']) > remaining:
            if not selected:
                selected.append(dict(unit, text=unit['text'][:remaining], text_truncated=True))
            next_start = unit['index']
            break
        selected.append(unit)
        used += len(unit['text'])
    if next_start is None and selected and selected[-1]['index'] < len(units):
        next_start = selected[-1]['index'] + 1
    check_authorized()
    result = {'format': extension, 'total_units': len(units), 'units': selected,
              'truncated': next_start is not None, 'next_start': next_start}
    if extension == 'pdf':
        result['ocr_performed'] = False
        result['note'] = 'Extracted existing text only. Empty pages are marked text_empty; no OCR was performed.'
    return result
