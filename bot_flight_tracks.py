"""Optional flight history service for the existing Discord bot; no NewSky HTTP calls."""
import asyncio
import logging
import os
import time
import tempfile
from collections import Counter
from pathlib import Path
from aiohttp import ClientSession, ClientTimeout, web
from flight_tracker import TrackStore, rows_from_vatsim, rows_from_newsky, codes, timestamp, VATSIM_URL, RETENTION

from flight_track_audit import Audit, AuditedStore

LOG = logging.getLogger('flight_tracks')
HERE = Path(__file__).resolve().parent
ORIGINS = frozenset({'https://kazuar.in.ua', 'https://www.kazuar.in.ua', 'https://kazuar-avia.github.io', 'http://localhost:8000', 'http://127.0.0.1:8000'})
INTERVAL = 15

class TrackAccessFilter(logging.Filter):
    """Hide only routine GET responses for the flight history endpoint."""
    def filter(self, record):
        line = getattr(record, 'first_request_line', '')
        parts = line.split() if isinstance(line, str) else []
        return not (record.levelno == logging.INFO and len(parts) == 3 and
                    parts[0] == 'GET' and parts[1].partition('?')[0] == '/api/flight-tracks' and
                    getattr(record, 'response_status', None) in (200, 404))

TRACK_ACCESS_FILTER = TrackAccessFilter()

class Service:
    def __init__(self, db, rules, interval=INTERVAL):
        self.db = Path(db)
        self.rules = Path(rules)
        self.interval = interval
        self._store = None
        self.task = None
        self._reported_sources = set()
        self.audit = Audit(self.db.parent / 'track-audit')
        self._audit_health_at = 0.0
        self._audit_reads = Counter()
        self._audit_export_lock = asyncio.Lock()

    def audit_health(self, force=False):
        now = time.monotonic()
        if not force and now - self._audit_health_at < 60:
            return
        self._audit_health_at = now
        try:
            state = self.store().report()
            self.audit.event('health', state=state, http_reads=dict(self._audit_reads))
            self._audit_reads.clear()
        except Exception as exc:
            self.audit.event('health_failed', error=type(exc).__name__)

    async def send_audit(self, channel, file_factory):
        if self._audit_export_lock.locked():
            await channel.send('Експорт журналу вже виконується.')
            return
        async with self._audit_export_lock:
            self.audit_health(force=True)
            state = self.store().report()
            with tempfile.TemporaryDirectory(prefix='flight-audit-') as tmp:
                parts = await asyncio.to_thread(self.audit.export, tmp, state)
                await channel.send(f'Журнал аудиту: {len(parts)} JSONL-файл(ів). Містить події та поточний стан бази. Частини подано від найстарішої до найновішої.')
                for path in parts:
                    attachment = file_factory(str(path), filename=path.name)
                    try:
                        await channel.send(file=attachment)
                    finally:
                        attachment.close()

    def report_first_snapshot(self, source, count):
        if source not in self._reported_sources:
            self._reported_sources.add(source)
            print(f"[Flight tracks] {source}: collector ready ({count} flights)", flush=True)

    def store(self):
        if self._store is None:
            self.db.parent.mkdir(parents=True, exist_ok=True)
            self._store = AuditedStore(self.db, self.audit)
        return self._store

    def expire(self, now=None):
        store = self.store()
        now = time.time() if now is None else now
        before = store.safe_state()
        try:
            with store.db:
                cursor = store.db.execute('DELETE FROM flights WHERE missing_since IS NOT NULL AND missing_since<=?', (now-RETENTION,))
        except Exception as exc:
            self.audit.event('expiry_rollback', at=now, error=type(exc).__name__)
            raise
        try:
            store.diff(before, store.safe_state(), 'expiry', now)
            self.audit.event('expiry_check', at=now, cutoff=now-RETENTION, deleted_flights=cursor.rowcount)
        except Exception as exc:
            self.audit.event('audit_diff_error', operation='expiry', error=type(exc).__name__)

    def newsky(self, data):
        # Only known-complete snapshots can mark flights missing. Never fetch NewSky here.
        try:
            if not isinstance(data, dict) or type(data.get('totalResults')) is not int:
                raise ValueError('Missing NewSky snapshot count')
            rows = rows_from_newsky(data)
            saved = self.store().ingest('newsky', rows)
            if saved:
                self.report_first_snapshot('NewSky', len(rows))
            return saved
        except Exception as exc:
            self.audit.event('snapshot_rejected', source='newsky', error=type(exc).__name__,
                             declared_count=data.get('totalResults') if isinstance(data, dict) and type(data.get('totalResults')) is int else None,
                             actual_count=len(data['results']) if isinstance(data, dict) and isinstance(data.get('results'), list) else None)
            LOG.warning('NewSky history skipped (%s)', type(exc).__name__)
            return False

    def vatsim(self, data, now=None):
        now = time.time() if now is None else now
        stamp = timestamp((data.get('general') or {}).get('update_timestamp'))
        if stamp is None or abs(now-stamp)>180:
            raise ValueError('Stale VATSIM snapshot')
        airlines = codes(self.rules/'listofUKRaircompanies.txt')
        banned = {x.strip().upper() for x in (self.rules/'ruBanned.txt').read_text(encoding='utf-8-sig').splitlines() if x.strip()}
        rows = rows_from_vatsim(data, airlines, banned)
        saved = self.store().ingest('vatsim', rows, now=now, stamp=stamp)
        if saved:
            self.report_first_snapshot('VATSIM', len(rows))
        return saved

    async def run(self):
        async with ClientSession(timeout=ClientTimeout(total=12)) as session:
            while True:
                started = time.monotonic()
                try:
                    async with session.get(VATSIM_URL) as response:
                        response.raise_for_status()
                        self.vatsim(await response.json())
                except Exception as exc:
                    self.audit.event('snapshot_rejected', source='vatsim', error=type(exc).__name__, http_status=getattr(exc, 'status', None))
                    LOG.warning('VATSIM history skipped (%s)', type(exc).__name__)
                try:
                    self.expire()
                except Exception as exc:
                    LOG.warning('History expiry skipped (%s)', type(exc).__name__)
                self.audit_health()
                await asyncio.sleep(max(0.1, self.interval-(time.monotonic()-started)))

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run(), name='flight-history')
        return self.task

    def headers(self, request):
        headers = {'Cache-Control':'no-store', 'Vary':'Origin'}
        origin = request.headers.get('Origin')
        if origin in ORIGINS:
            headers['Access-Control-Allow-Origin'] = origin
        return headers

    async def history(self, request):
        headers = self.headers(request)
        source, key = request.query.get('source',''), request.query.get('key','')
        session = request.query.get('session')
        if source not in ('vatsim','newsky') or not key or len(key)>512 or (source=='vatsim' and (not session or len(session)>100)):
            self._audit_reads['invalid:400'] += 1
            return web.json_response({'error':'Invalid track request'}, status=400, headers=headers)
        try:
            result = self.store().history(source,key,session if source=='vatsim' else key)
            if result is not None and result['missing_since'] is not None and result['missing_since']<=time.time()-RETENTION:
                result = None
        except Exception as exc:
            self._audit_reads[source+':503'] += 1
            self.audit.event('track_read_failed', source=source, error=type(exc).__name__)
            LOG.warning('Track read failed (%s)', type(exc).__name__)
            return web.json_response({'error':'History unavailable'},status=503,headers=headers)
        self._audit_reads[source+(':200' if result is not None else ':404')] += 1
        return web.json_response(result if result is not None else {'error':'Track not found'}, status=200 if result is not None else 404,headers=headers)

    def mount(self, app):
        app.router.add_get('/api/flight-tracks', self.history)
        logging.getLogger('aiohttp.access').addFilter(TRACK_ACCESS_FILTER)

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if self._store is not None:
            self.audit.event('database_close')
            self._store.close()
            self._store = None

_service = None

def enabled():
    return os.getenv('FLIGHT_TRACKS_ENABLED','0') == '1'

def get_service():
    global _service
    if _service is None:
        site = HERE.parent/'kazuar-avia.github.io'
        site_data = site/'data'
        rule_names = ('listofUKRaircompanies.txt', 'ruBanned.txt')
        default_rules = next((p for p in (site_data, site, HERE/'flight_track_rules') if all((p/name).is_file() for name in rule_names)), HERE/'flight_track_rules')
        rules = Path(os.getenv('FLIGHT_TRACK_RULES_DIR', str(default_rules)))
        if not all((rules/name).is_file() for name in rule_names) and all((rules/'data'/name).is_file() for name in rule_names):
            rules = rules/'data'
        db = Path(os.getenv('FLIGHT_TRACK_DB',str(HERE/'flight-tracks.local.sqlite3')))
        _service = Service(db,rules)
    return _service

def start():
    if enabled():
        try:
            get_service().start()
        except Exception as exc:
            LOG.warning('History startup skipped (%s)',type(exc).__name__)

def ingest_newsky(data):
    if enabled():
        try:
            get_service().newsky(data)
        except Exception as exc:
            LOG.warning('History hook skipped (%s)',type(exc).__name__)

def mount(app):
    if enabled():
        try:
            get_service().mount(app)
        except Exception as exc:
            LOG.warning('History route skipped (%s)',type(exc).__name__)
