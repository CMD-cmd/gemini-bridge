"""Read-only daily CLI release checks for user-facing bridge responses.

Only probes the fixed --version flag when local identity needs refreshing.
Never starts a model, downloads an executable, or sends task material.
"""
import datetime as dt
import fcntl
import gzip
import hashlib
import io
import json
import os
import subprocess
from pathlib import Path
import platform
import re
import threading
from urllib.request import Request, urlopen
from bridge import ROOT, atomic_json

TZ = dt.timezone(dt.timedelta(hours=8), 'Asia/Shanghai')
BASE = 'https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/'
CHANGELOG = 'https://www.antigravity.google/docs/changelog?tab=cli'
MAX_BYTES = 65536
_THREAD_LOCK = threading.Lock()


def version_key(value):
    if not isinstance(value, str):
        raise ValueError('Missing release version')
    m = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?', value)
    if not m:
        raise ValueError('Invalid release version')
    pre = m.group(4)
    suffix = tuple((0, int(x)) if x.isdigit() else (1, x) for x in pre.split('.')) if pre else ()
    return (tuple(int(m.group(i)) for i in (1, 2, 3)), pre is None, suffix)


def manifest_url():
    machine = {'aarch64':'arm64', 'AMD64':'x64', 'x86_64':'x64'}.get(platform.machine(), platform.machine())
    system = {'Darwin':'darwin', 'Linux':'linux', 'Windows':'windows'}.get(platform.system())
    if system is None or machine not in ('arm64', 'x64'):
        raise ValueError('Unsupported CLI release platform')
    if system == 'Linux':
        raise ValueError('Linux manifest requires glibc/musl identification; not inferred')
    return BASE + system + '_' + machine + '.json'


def fetch_release():
    url = manifest_url()
    # A public metadata GET only. No account token, prompt, or input file.
    with urlopen(Request(url, headers={'Accept-Encoding':'identity'}), timeout=4) as response:
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('Release manifest too large')
    if raw.startswith(b'\x1f\x8b'):
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as handle:
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('Expanded manifest too large')
    value = json.loads(raw)
    version_key(value.get('version'))
    return {'version':value['version'], 'source_url':url}


def read_cache(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def probe_version(binary):
    """Read one fixed local flag; no prompt, account command, or upgrade."""
    env = {k: v for k, v in os.environ.items()
           if k in ('PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'SYSTEMROOT', 'WINDIR')}
    try:
        result = subprocess.run([str(binary), '--version'], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=2,
                                env=dict(env, NO_COLOR='1'))
        version = result.stdout.strip()
        if result.returncode != 0 or len(version) > 80:
            return None
        version_key(version)
        return version.lstrip('v')
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def local_identity(root, cache, daily, probe=None):
    binary = root / 'bin' / 'agy'
    stat = binary.stat()
    stamp = [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino]
    previous = cache.get('local', {})
    if not daily and previous.get('schema') == 2 and previous.get('stamp') == stamp:
        return previous
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    record = read_cache(root / 'install-evidence.json')
    recorded = record.get('manifest', {}).get('version')
    installed, identity = None, 'unverified_binary'
    if digest == record.get('binary_sha256'):
        try:
            version_key(recorded)
            installed, identity = recorded.lstrip('v'), 'verified_install_record'
        except ValueError:
            pass
    if installed is None and previous.get('schema') == 2 and previous.get('sha256') == digest:
        candidate = previous.get('installed_version')
        try:
            version_key(candidate)
            installed, identity = candidate, previous.get('identity', 'cli_version_probe')
        except ValueError:
            pass
    if installed is None:
        candidate = (probe or probe_version)(binary)
        try:
            version_key(candidate)
            installed, identity = candidate.lstrip('v'), 'cli_version_probe'
        except ValueError:
            pass
    after = binary.stat()
    if [after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_ino] != stamp:
        installed, identity = None, 'binary_changed_during_probe'
    # A --version result identifies the hashed local executable, not its package origin.
    return {'schema': 2, 'sha256': digest, 'stamp': stamp, 'installed_version': installed,
            'last_verified_install_version': recorded, 'identity': identity}


def check_for_updates(root=None, *, now=None, fetch=None, probe=None, deliver_notice=True):
    """One public query per Shanghai date. Observers never consume a reminder."""
    root = root or ROOT
    if not _THREAD_LOCK.acquire(blocking=False):
        return None
    try:
        runtime = root / '.runtime' / 'cli-updates'
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (runtime / 'check.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return None
            moment = now or dt.datetime.now(dt.timezone.utc)
            if moment.tzinfo is None:
                raise ValueError('Update check requires timezone-aware time')
            today = moment.astimezone(TZ).date().isoformat()
            path = runtime / 'cache.json'
            cache = read_cache(path)
            daily = cache.get('checked_on') != today
            previous = cache.get('local', {})
            local = local_identity(root, cache, daily, probe)
            binary_changed = bool(previous and previous.get('sha256') != local['sha256'])
            changed = local != previous or cache.get('schema') != 2
            fresh = (daily or binary_changed or cache.get('schema') != 2
                     or local['installed_version'] != previous.get('installed_version'))
            if daily:
                cache['checked_on'] = today  # Failed queries are cached too; no retry storm.
                try:
                    release = (fetch or fetch_release)()
                    version_key(release['version'])
                    cache.update(latest_version=release['version'].lstrip('v'), latest_checked_on=today,
                                 source_url=release.get('source_url') or manifest_url(), check_error=None)
                except Exception:
                    cache['check_error'] = 'release_check_failed'
            cache.update(local=local, schema=2)
            latest, installed = cache.get('latest_version'), local['installed_version']
            if latest:
                try:
                    version_key(latest)
                except ValueError:
                    latest = None
            status, notice = 'up_to_date', None
            if installed is None:
                status = 'local_version_unverified'
                notice = 'Antigravity CLI 当前版本暂未能确认。'
                if latest:
                    notice += '官方最近查询版本为 ' + latest + '（' + str(cache.get('latest_checked_on')) + '）。'
            elif cache.get('check_error'):
                status = 'check_failed'
            elif latest and version_key(latest) > version_key(installed):
                status = 'update_available'
                notice = 'Antigravity CLI 有更新：当前 ' + installed + '，可用 ' + latest + '。'
            elif binary_changed:
                status = 'local_binary_changed'
                notice = 'Antigravity CLI 本地文件已变化，当前版本 ' + installed + '。'
            if notice:
                notice += '如需升级，请告诉 Codex；本次未自动升级。'
            key = [today, local['sha256'], latest, status]
            pending = bool(notice and cache.get('last_notified') != key)
            notify = bool(pending and deliver_notice)
            if notify:
                cache['last_notified'] = key
            if fresh or notify or changed:
                atomic_json(path, cache)
            if not fresh and not pending:
                return None
            return {'checked_on': today, 'timezone': 'Asia/Shanghai',
                    'latest_version': latest, 'latest_checked_on': cache.get('latest_checked_on'),
                    'installed_version': installed, 'local_identity': local['identity'],
                    'last_verified_install_version': local['last_verified_install_version'],
                    'status': status, 'notify': notify, 'notice': notice if notify else None,
                    'notification_pending': bool(pending and not notify),
                    'source_url': cache.get('source_url'), 'changelog_url': CHANGELOG,
                    'check_error': cache.get('check_error')}
    except Exception:
        return {'status': 'check_failed', 'notify': False, 'check_error': 'local_check_failed'}
    finally:
        _THREAD_LOCK.release()


def attach_notice(value, *, deliver_notice=True):
    if isinstance(value, dict):
        update = check_for_updates(deliver_notice=deliver_notice)
        if update is not None:
            value = dict(value, cli_update=update)
    return value
