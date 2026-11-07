"""Crossref public metadata adapter. No credentials, inference, or full-text fetching.

All HTTP goes through the caller's bounded, authorized fetch implementation.
Crossref links are untrusted metadata, not evidence of open-access availability.
"""
from html.parser import HTMLParser
import json
import re
import time
from urllib.parse import quote, urlencode, urlsplit

HOST = 'api.crossref.org'
MAX_TEXT = 100_000
TOOL_DEF = {
    'name': 'literature_search',
    'description': 'Search free public Crossref journal-article metadata, or retrieve metadata for an exact DOI. Metadata is not full text; abstracts and OA status may be missing.',
    'inputSchema': {'type': 'object', 'properties': {
        'query': {'type': 'string', 'minLength': 1, 'maxLength': 2000},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 10, 'default': 5},
        'source': {'type': 'string', 'enum': ['crossref'], 'default': 'crossref'}},
        'required': ['query'], 'additionalProperties': False}}
TOOL_DEFINITIONS = [TOOL_DEF]


def _check(check, deadline):
    if check() is False:
        raise PermissionError('Research authorization revoked')
    if time.monotonic() >= deadline:
        raise TimeoutError('Research time budget exhausted')


def validate_args(args, hosts):
    if not isinstance(hosts, (list, tuple, set, frozenset)) or HOST not in hosts:
        raise ValueError('Crossref API host is not authorized for this task')
    if not isinstance(args, dict) or set(args) - {'query', 'limit', 'source'}:
        raise ValueError('Only query, limit and source are accepted')
    query, limit = args.get('query'), args.get('limit', 5)
    if not isinstance(query, str) or not query.strip() or len(query) > 2000 or any(ord(c) < 32 for c in query):
        raise ValueError('A bounded nonempty query is required')
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError('limit must be an integer from 1 to 10')
    if args.get('source', 'crossref') != 'crossref':
        raise ValueError('Unknown literature source')
    return {'query': query.strip(), 'limit': limit, 'source': 'crossref'}


class _Plain(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0
    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.skip += 1
        elif not self.skip:
            self.parts.append(' ')
    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self.skip:
            self.skip -= 1
        elif not self.skip:
            self.parts.append(' ')
    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def _plain(value):
    if not isinstance(value, str):
        return ''
    parser = _Plain()
    parser.feed(value)
    parser.close()
    return ' '.join(''.join(parser.parts).split())


def _safe_link(value):
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) <= 32 for c in value):
        return ''
    try:
        url = urlsplit(value)
        if url.scheme not in ('http', 'https') or not url.hostname or url.username is not None or url.password is not None:
            return ''
        return value
    except ValueError:
        return ''


def _year(item):
    for field in ('published', 'published-print', 'published-online', 'issued'):
        parts = item.get(field, {})
        dates = parts.get('date-parts') if isinstance(parts, dict) else None
        if isinstance(dates, list) and dates and isinstance(dates[0], list) and dates[0] and type(dates[0][0]) is int:
            return str(dates[0][0])
    return ''


def _record(item, meta):
    doi = item.get('DOI', '')
    if not isinstance(doi, str) or not re.fullmatch(r'10\.\d{4,9}/\S+', doi, re.I):
        raise ValueError('Invalid DOI in Crossref record')
    titles = item.get('title', [])
    title = _plain(titles[0]) if isinstance(titles, list) and titles else ''
    authors = []
    for author in item.get('author', []) if isinstance(item.get('author'), list) else []:
        if not isinstance(author, dict):
            continue
        name = _plain(author.get('name')) or ' '.join(filter(None, (_plain(author.get('given')), _plain(author.get('family')))))
        if name:
            authors.append(name)
    abstract = _plain(item.get('abstract'))
    links = []
    for entry in item.get('link', []) if isinstance(item.get('link'), list) else []:
        if not isinstance(entry, dict):
            continue
        url = _safe_link(entry.get('URL'))
        if url:
            links.append({'url': url, 'content_type': _plain(entry.get('content-type')), 'content_version': _plain(entry.get('content-version')), 'intended_application': _plain(entry.get('intended-application')), 'access': 'unknown', 'retrieved': False})
    licenses = []
    for entry in item.get('license', []) if isinstance(item.get('license'), list) else []:
        if not isinstance(entry, dict):
            continue
        url = _safe_link(entry.get('URL'))
        if url:
            licenses.append({'url': url, 'content_version': _plain(entry.get('content-version'))})
    return {'title': title, 'authors': authors, 'year': _year(item), 'doi': doi,
            'abstract': abstract, 'abstract_status': 'present' if abstract else 'missing',
            'publisher': _plain(item.get('publisher')), 'source_url': meta['url'],
            'url': _safe_link(item.get('URL')) or 'https://doi.org/' + quote(doi, safe='/'),
            'links': links, 'licenses': licenses, 'oa_status': 'unknown', 'full_text_retrieved': False,
            'sourcehash': meta['sha256'], 'source_sha256': meta['sha256'], 'truncated': False}


def search(args, hosts, check, deadline, fetch):
    """Return metadata records; fetch(url, hosts, check, deadline)->(metadata,bytes).

    The caller supplies an absolute monotonic deadline and its guarded fetch.
    Exact DOI queries use /works/{doi}; other queries use /works?query=... .
    The adapter does not visit returned DOI, license, or publisher links.
    """
    _check(check, deadline)
    try:
        args = validate_args(args, hosts)
        query = args['query']
        doi = query
        if doi.lower().startswith('https://doi.org/'):
            doi = doi[len('https://doi.org/'):]
        if doi.lower().startswith('doi:'):
            doi = doi[4:].strip()
        exact = bool(re.fullmatch(r'10\.\d{4,9}/\S+', doi, re.I))
        url = 'https://' + HOST + '/works'
        if exact:
            url += '/' + quote(doi, safe='')
        else:
            url += '?' + urlencode({'query': query, 'rows': args['limit'], 'filter': 'type:journal-article'})
        _check(check, deadline)
        meta, body = fetch(url, hosts, check, deadline)
        _check(check, deadline)
        if meta['status'] != 200:
            raise ValueError('Crossref returned HTTP ' + str(meta['status']))
        payload = json.loads(body)
        if not isinstance(payload, dict) or payload.get('status') != 'ok':
            raise ValueError('Invalid Crossref response')
        message = payload.get('message')
        if not isinstance(message, dict):
            raise ValueError('Invalid Crossref message')
        if exact:
            items = [message]
        else:
            items = message.get('items')
            if not isinstance(items, list) or len(items) > args['limit']:
                raise ValueError('Invalid Crossref items')
        if any(not isinstance(item, dict) for item in items):
            raise ValueError('Invalid Crossref record')
        records = [_record(item, meta) for item in items]
        budget = MAX_TEXT
        truncated = False
        for record in records:
            for key in ('title', 'year', 'doi', 'abstract', 'publisher'):
                value = record[key]
                record[key] = value[:budget]
                budget -= len(record[key])
                if record[key] != value:
                    record['truncated'] = truncated = True
            names = []
            for name in record['authors']:
                names.append(name[:budget])
                budget -= len(names[-1])
                if names[-1] != name:
                    record['truncated'] = truncated = True
            record['authors'] = names
        return {'source': 'crossref', 'query': query, 'metadata_only': True, 'records': records,
                'sources': [meta], 'truncated': truncated}
    finally:
        _check(check, deadline)
