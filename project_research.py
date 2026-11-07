"""Read-only public research tools; stdlib only, no ambient credentials or proxies.

The caller supplies exact task-authorized hosts and a live authorization callback.
Network/page text is untrusted source material, never an instruction to the caller.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import queue
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

MAX_BYTES = 4 * 1024 * 1024
MAX_TEXT = 100_000
REQUEST_TIMEOUT = 20.0
TOTAL_TIMEOUT = 60.0
MAX_REDIRECTS = 3
EUTILS_HOST = 'eutils.ncbi.nlm.nih.gov'
TOOL_DEFINITIONS = [
    {'name': 'pubmed_search', 'description': 'Search PubMed and return source-backed bibliographic records and abstracts; no scientific inference.',
     'inputSchema': {'type': 'object', 'properties': {'query': {'type': 'string', 'minLength': 1, 'maxLength': 2000}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 10, 'default': 5}, 'source': {'type':'string','enum':['pubmed','europe_pmc'],'description':'Public API source; Europe PMC MED records are PubMed-indexed.'}}, 'required': ['query'], 'additionalProperties': False}},
    {'name': 'public_page', 'description': 'Read text from one explicitly authorized public HTTPS host. No JavaScript, credentials, or subresources. PDFs unsupported.',
     'inputSchema': {'type': 'object', 'properties': {'url': {'type': 'string', 'maxLength': 8192}}, 'required': ['url'], 'additionalProperties': False}},
]


def _public_ip(value):
    ip = ipaddress.ip_address(value)
    if not ip.is_global or ip.is_multicast or ip.is_unspecified or ip.is_reserved or (getattr(ip, 'ipv4_mapped', None) and not ip.ipv4_mapped.is_global):
        raise ValueError('Only public unicast IP addresses are allowed')
    return str(ip)


def _host(value):
    if not isinstance(value, str) or not value or value != value.strip() or value.endswith('.') or '%' in value:
        raise ValueError('Invalid exact host')
    value = value.lower()
    try:
        return _public_ip(value)
    except ValueError:
        pass
    if ':' in value or re.fullmatch(r'[0-9.]+', value):
        raise ValueError('Invalid public host')
    value = value.encode('idna').decode('ascii')
    if len(value) > 253 or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part) for part in value.split('.')) or '.' not in value:
        raise ValueError('Invalid public host')
    if value == 'localhost' or value.endswith(('.localhost', '.local', '.internal')):
        raise ValueError('Local hosts are not allowed')
    return value


def _hosts(hosts):
    if not isinstance(hosts, (list, tuple, set, frozenset)):
        raise ValueError('Explicit authorized hosts are required')
    return {_host(host) for host in hosts}


def _url(url, hosts):
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) <= 32 or ord(c) == 127 for c in url) or '\\' in url:
        raise ValueError('Invalid HTTPS URL')
    try:
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443) or parsed.fragment:
            raise ValueError('Only credential-free HTTPS port 443 URLs are allowed')
        host = _host(parsed.hostname)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValueError('Invalid HTTPS URL') from exc
    if host not in hosts:
        raise ValueError('Host is not authorized for this task')
    # Require ASCII paths; callers may percent-encode Unicode explicitly.
    (parsed.path + parsed.query).encode('ascii')
    authority = '[' + host + ']' if ':' in host else host
    return urlunsplit(('https', authority, parsed.path or '/', parsed.query, '')), host


def validate_args(name, args, hosts):
    allowed = _hosts(hosts)
    if not isinstance(args, dict):
        raise ValueError('Arguments must be an object')
    if name == 'public_page':
        if set(args) != {'url'}:
            raise ValueError('Only url is accepted')
        return {'url': _url(args['url'], allowed)[0]}
    if name == 'pubmed_search':
        if set(args) - {'query', 'limit','source'} or 'query' not in args:
            raise ValueError('Only query and limit are accepted')
        query = args['query']
        limit = args.get('limit', 5)
        if not isinstance(query, str) or not query.strip() or len(query) > 2000 or any(ord(c) < 32 for c in query):
            raise ValueError('A bounded nonempty query is required')
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError('limit must be an integer from 1 to 10')
        provider=args.get('source','pubmed')
        if provider not in ('pubmed','europe_pmc'):raise ValueError('Unknown literature source')
        host=EUTILS_HOST if provider=='pubmed' else 'www.ebi.ac.uk'
        if host not in allowed:raise ValueError('Selected literature API host is not authorized for this task')
        return {'query':query.strip(),'limit':limit,**({'source':provider} if provider!='pubmed' else {})}
    raise ValueError('Unknown research tool')


def _authorize(check):
    if check() is False:
        raise PermissionError('Research authorization revoked')


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('Research request time budget exhausted')
    return remaining


def _resolve(host, deadline, check=lambda:None):
    # getaddrinfo has no timeout API. A daemon prevents it blocking the caller's budget.
    results = queue.Queue(maxsize=1)
    def resolve():
        try:
            results.put((True, socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
        except Exception as exc:
            results.put((False, exc))
    threading.Thread(target=resolve, daemon=True).start()
    try:
        ok, addresses = results.get(timeout=_remaining(deadline))
    except queue.Empty as exc:
        raise TimeoutError('DNS time budget exhausted') from exc
    if not ok:
        raise addresses
    if not addresses:
        raise ValueError('No public DNS addresses')
    # Transparent proxy DNS can return benchmarking-range Fake-IP addresses.
    # Resolve the authorized hostname through pinned, certificate-verified DoH;
    # never connect to the Fake-IP or relax the public-address check.
    fake=ipaddress.ip_network('198.18.0.0/15')
    if all(ipaddress.ip_address(a[4][0]) in fake for a in addresses):
        url='https://dns.google/resolve?'+urlencode({'name':host,'type':'A','edns_client_subnet':'0.0.0.0/0'})
        status,headers,body=_request_once(url,{'dns.google'},check,deadline,
            pinned=(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443)))
        if status!=200 or len(body)>65536:raise ValueError('Public DNS resolution failed')
        reply=json.loads(body)
        if reply.get('Status')!=0 or reply.get('TC'):raise ValueError('Public DNS resolution failed')
        if not any(q.get('name','').rstrip('.').lower()==host and q.get('type')==1 for q in reply.get('Question',[])):
            raise ValueError('Public DNS response does not match host')
        answers=[_public_ip(r['data']) for r in reply.get('Answer',[]) if r.get('type')==1]
        if not answers:raise ValueError('No public DNS address')
        return socket.AF_INET,socket.SOCK_STREAM,6,'',(answers[0],443)
    for family, socktype, proto, canonname, sockaddr in addresses:
        _public_ip(sockaddr[0])
        if family not in (socket.AF_INET, socket.AF_INET6):
            raise ValueError('Unsupported address family')
    return addresses[0]


def _request_once(url, hosts, check, total_deadline, *, pinned=None):
    url, host = _url(url, hosts)
    _authorize(check)
    deadline = min(total_deadline, time.monotonic() + REQUEST_TIMEOUT)
    family, socktype, proto, _, sockaddr = pinned or _resolve(host, deadline, check)
    budget = _remaining(deadline)
    raw = socket.socket(family, socktype, proto)
    tls = None
    def interrupt():
        for connection in (tls, raw):
            if connection is not None:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
    # A hard deadline also bounds slow-drip headers/TLS, whose repeated recv
    # operations would otherwise reset the socket's individual read timeout.
    watchdog = threading.Timer(budget, interrupt)
    watchdog.daemon = True
    watchdog.start()
    try:
        raw.settimeout(_remaining(deadline))
        raw.connect(sockaddr)  # Fixed validated IP; never resolve hostname during connect.
        raw.settimeout(_remaining(deadline))
        tls = ssl.create_default_context().wrap_socket(raw, server_hostname=host, do_handshake_on_connect=False)
        tls.settimeout(_remaining(deadline))
        tls.do_handshake()
        tls.settimeout(_remaining(deadline))
        _authorize(check)
        parsed = urlsplit(url)
        target = parsed.path + ('?' + parsed.query if parsed.query else '')
        tls.sendall(('GET ' + target + ' HTTP/1.1\r\nHost: ' + parsed.netloc + '\r\nUser-Agent: GeminiBridge-PublicResearch/1.0\r\nAccept: text/html, application/json, application/xml, text/plain\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n').encode('ascii'))
        tls.settimeout(_remaining(deadline))
        response = http.client.HTTPResponse(tls)
        response.begin()
        headers = {key.lower(): value for key, value in response.getheaders()}
        length = headers.get('content-length')
        if length and (not length.isdigit() or int(length) > MAX_BYTES):
            raise ValueError('Response exceeds byte limit or has invalid length')
        if headers.get('content-encoding', 'identity').lower() != 'identity':
            raise ValueError('Compressed responses are unsupported')
        chunks = []
        size = 0
        while True:
            tls.settimeout(_remaining(deadline))
            _authorize(check)
            chunk = response.read1(min(65536, MAX_BYTES + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_BYTES:
                raise ValueError('Response exceeds byte limit')
            chunks.append(chunk)
        _remaining(deadline)
        _authorize(check)
        return response.status, headers, b''.join(chunks)
    finally:
        watchdog.cancel()
        if tls is not None:
            tls.close()
        raw.close()


def _fetch(url, hosts, check, deadline):
    for redirects in range(MAX_REDIRECTS + 1):
        url, _ = _url(url, hosts)
        status, headers, body = _request_once(url, hosts, check, deadline)
        if status in (301, 302, 303, 307, 308):
            if redirects == MAX_REDIRECTS or not headers.get('location'):
                raise ValueError('Redirect limit exceeded or location missing')
            url = _url(urljoin(url, headers['location']), hosts)[0]
            continue
        digest = hashlib.sha256(body).hexdigest()
        return {'url': url, 'status': status, 'content_type': headers.get('content-type', ''), 'sha256': digest, 'source_sha256': digest, 'fetched_at': datetime.now(timezone.utc).isoformat()}, body
    raise ValueError('Redirect limit exceeded')


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = []
        self.parts = []
    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript', 'template'):
            self.skip.append(tag)
        elif not self.skip and tag in ('p', 'div', 'br', 'li', 'h1', 'h2', 'h3', 'tr'):
            self.parts.append('\n')
    def handle_endtag(self, tag):
        if tag in self.skip:
            self.skip = self.skip[:self.skip.index(tag)]
        elif not self.skip:
            self.parts.append('\n')
    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def _page(meta, body):
    kind = meta['content_type'].split(';')[0].strip().lower()
    if kind == 'application/pdf' or body.startswith(b'%PDF-'):
        return dict(meta, supported=False, reason='PDF text extraction is unsupported; use an authorized document extractor.', text='', truncated=False)
    if kind not in ('text/html', 'application/xhtml+xml', 'application/json', 'text/plain', 'text/xml', 'application/xml'):
        return dict(meta, supported=False, reason='Unsupported content type', text='', truncated=False)
    charset = re.search(r'charset\s*=\s*[\"\']?([\w.-]+)', meta['content_type'], re.I)
    try:
        text = body.decode(charset.group(1) if charset else 'utf-8', errors='replace')
    except LookupError:
        text = body.decode('utf-8', errors='replace')
    if kind in ('text/html', 'application/xhtml+xml'):
        parser = _HTMLText()
        parser.feed(text)
        parser.close()
        text = '\n'.join(' '.join(line.split()) for line in ''.join(parser.parts).splitlines() if line.strip())
    return dict(meta, supported=True, text=text[:MAX_TEXT], truncated=len(text) > MAX_TEXT)


def _xml(body):
    # NCBI's own XML uses an external PubmedArticleSet DOCTYPE. Strip that
    # declaration only; reject internal subsets and all entity declarations.
    if b'\x00' in body or re.search(br'<!ENTITY|<!DOCTYPE[^>]*\[', body, re.I):
        raise ValueError('XML entities and internal DTDs are forbidden')
    body = re.sub(br'<!DOCTYPE\s+[^>]+>', b'', body, flags=re.I)
    return ET.fromstring(body)


def _text(element):
    return ''.join(element.itertext()).strip() if element is not None else ''


def _pubmed(args, hosts, check, deadline):
    base = 'https://' + EUTILS_HOST + '/entrez/eutils/'
    search_url = base + 'esearch.fcgi?' + urlencode({'db': 'pubmed', 'term': args['query'], 'retmax': args['limit'], 'retmode': 'json'})
    search_meta, body = _fetch(search_url, hosts, check, deadline)
    if search_meta['status'] != 200:
        raise ValueError('PubMed search returned HTTP ' + str(search_meta['status']))
    search = json.loads(body)
    result = search.get('esearchresult') if isinstance(search, dict) else None
    if not isinstance(result, dict) or 'idlist' not in result or result.get('ERROR') or result.get('errorlist'):
        raise ValueError('Invalid or failed PubMed search response')
    ids = result['idlist']
    if not isinstance(ids, list) or len(ids) > args['limit'] or any(not isinstance(x, str) or not re.fullmatch(r'\d{1,12}', x) for x in ids):
        raise ValueError('Invalid PubMed identifier response')
    sources = [search_meta]
    records = []
    if ids:
        fetch_url = base + 'efetch.fcgi?' + urlencode({'db': 'pubmed', 'id': ','.join(ids), 'retmode': 'xml'})
        fetch_meta, body = _fetch(fetch_url, hosts, check, deadline)
        sources.append(fetch_meta)
        if fetch_meta['status'] != 200:
            raise ValueError('PubMed fetch returned HTTP ' + str(fetch_meta['status']))
        root = _xml(body)
        for article in root.findall('PubmedArticle'):
            citation = article.find('MedlineCitation')
            pmid = _text(citation.find('PMID')) if citation is not None else ''
            if pmid not in ids:
                raise ValueError('Unexpected PubMed record')
            paper = citation.find('Article')
            if paper is None:
                continue
            abstract = '\n'.join((part.get('Label', '') + ': ' if part.get('Label') else '') + _text(part) for part in paper.findall('./Abstract/AbstractText'))
            doi = next((_text(x) for x in article.findall('./PubmedData/ArticleIdList/ArticleId') if x.get('IdType') == 'doi'), '')
            year = _text(paper.find('./Journal/JournalIssue/PubDate/Year')) or _text(paper.find('./Journal/JournalIssue/PubDate/MedlineDate'))
            records.append({'pmid': pmid, 'pmcid': next((_text(x) for x in article.findall('./PubmedData/ArticleIdList/ArticleId') if x.get('IdType')=='pmc'), ''), 'title': _text(paper.find('ArticleTitle')), 'journal': _text(paper.find('./Journal/Title')), 'year': year, 'doi': doi, 'abstract': abstract[:MAX_TEXT], 'truncated': len(abstract) > MAX_TEXT, 'url': 'https://pubmed.ncbi.nlm.nih.gov/' + pmid + '/', 'source_api_url': fetch_meta['url'], 'source_sha256': fetch_meta['sha256']})
    # Bound the aggregate text, including abnormally large titles/journal fields.
    budget = MAX_TEXT
    truncated = False
    for record in records:
        for key in ('title', 'journal', 'year', 'doi', 'abstract'):
            value = record[key]
            record[key] = value[:budget]
            budget -= len(record[key])
            if record[key] != value:
                record['truncated'] = truncated = True
        truncated = truncated or record['truncated']
    return {'query': args['query'], 'records': records, 'sources': sources, 'truncated': truncated}



def _europe_pmc(args,hosts,check,deadline):
    executed='SRC:MED AND ('+args['query']+')'
    url='https://www.ebi.ac.uk/europepmc/webservices/rest/search?'+urlencode({'query':executed,'format':'json','resultType':'core','pageSize':args['limit']})
    meta,body=_fetch(url,hosts,check,deadline)
    if meta['status']!=200:raise ValueError('Europe PMC returned HTTP '+str(meta['status']))
    value=json.loads(body);items=value.get('resultList',{}).get('result')
    if not isinstance(items,list) or len(items)>args['limit']:raise ValueError('Invalid Europe PMC response')
    records=[];budget=MAX_TEXT;truncated=False
    for item in items:
        pmid=item.get('id','')
        if item.get('source')!='MED' or not re.fullmatch(r'\d{1,12}',pmid):raise ValueError('Unexpected indexed record')
        paper={'pmid':pmid,'pmcid':item.get('pmcid',''),'title':item.get('title',''),
            'journal':item.get('journalInfo',{}).get('journal',{}).get('title',''),'year':item.get('pubYear',''),
            'doi':item.get('doi',''),'abstract':item.get('abstractText',''),'open_access':item.get('isOpenAccess')=='Y',
            'url':'https://pubmed.ncbi.nlm.nih.gov/'+pmid+'/','source_api_url':url,'source_sha256':meta['sha256'],'truncated':False}
        for key in ('title','journal','year','doi','abstract'):
            text=str(paper[key]);paper[key]=text[:budget];budget-=len(paper[key])
            if len(text)>len(paper[key]):paper['truncated']=truncated=True
        records.append(paper)
    return {'query':args['query'],'executed_query':executed,'provider':'Europe PMC (PubMed-indexed MED records)','records':records,'sources':[meta],'truncated':truncated}


def execute(name, args, hosts, check_authorized):
    """Raise ValueError/PermissionError/TimeoutError on refusal; never silently retry.

    check_authorized() must raise or return False after revocation (None is success).
    All hashes describe raw response bytes, before text extraction.
    """
    _authorize(check_authorized)
    try:
        args = validate_args(name, args, hosts)
        allowed = _hosts(hosts)
        deadline = time.monotonic() + TOTAL_TIMEOUT
        if name == 'public_page':
            meta, body = _fetch(args['url'], allowed, check_authorized, deadline)
            result=_page(meta,body)
            challenge=result.get('text','').strip().lower()
            if len(challenge)<5000 and challenge.startswith('checking your browser') and 'recaptcha' in challenge:
                return dict(result,ok=False,error='Public source requires browser verification; this is not article text',verification_required=True)
            if meta['status']!=200 or not result.get('supported'):
                result.update(ok=False,error=result.get('reason') or 'Public source returned HTTP '+str(meta['status']))
            return result
        return (_europe_pmc if args.get('source')=='europe_pmc' else _pubmed)(args, allowed, check_authorized, deadline)
    finally:
        _authorize(check_authorized)
