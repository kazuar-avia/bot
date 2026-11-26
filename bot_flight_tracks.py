"""Optional flight history service for the existing Discord bot; no NewSky HTTP calls."""
import asyncio
import logging
import os
import time
from pathlib import Path
from aiohttp import ClientSession, ClientTimeout, web
from flight_tracker import TrackStore, rows_from_vatsim, rows_from_newsky, codes, timestamp, VATSIM_URL, RETENTION

LOG = logging.getLogger('flight_tracks')
HERE = Path(__file__).resolve().parent
ORIGINS = frozenset({'https://kazuar.in.ua', 'https://www.kazuar.in.ua', 'http://localhost:8000', 'http://127.0.0.1:8000'})
INTERVAL = 15

class Service:
    def __init__(self, db, rules, interval=INTERVAL):
        self.db = Path(db)
        self.rules = Path(rules)
        self.interval = interval
        self._store = None
        self.task = None

    def store(self):
        if self._store is None:
            self.db.parent.mkdir(parents=True, exist_ok=True)
            self._store = TrackStore(self.db)
        return self._store

    def expire(self, now=None):
        store = self.store()
        with store.db:
            store.db.execute('DELETE FROM flights WHERE missing_since IS NOT NULL AND missing_since<=?', ((time.time() if now is None else now)-RETENTION,))

    def newsky(self, data):
        # Only known-complete snapshots can mark flights missing. Never fetch NewSky here.
        try:
            if not isinstance(data, dict) or type(data.get('totalResults')) is not int:
                raise ValueError('Missing NewSky snapshot count')
            rows = rows_from_newsky(data)
            saved = self.store().ingest('newsky', rows)
            if saved:
                LOG.info('NewSky: %s flights', len(rows))
            return saved
        except Exception as exc:
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
            LOG.info('VATSIM: %s flights', len(rows))
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
                    LOG.warning('VATSIM history skipped (%s)', type(exc).__name__)
                try:
                    self.expire()
                except Exception as exc:
                    LOG.warning('History expiry skipped (%s)', type(exc).__name__)
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
            return web.json_response({'error':'Invalid track request'}, status=400, headers=headers)
        try:
            result = self.store().history(source,key,session if source=='vatsim' else key)
            if result is not None and result['missing_since'] is not None and result['missing_since']<=time.time()-RETENTION:
                result = None
        except Exception as exc:
            LOG.warning('Track read failed (%s)', type(exc).__name__)
            return web.json_response({'error':'History unavailable'},status=503,headers=headers)
        return web.json_response(result if result is not None else {'error':'Track not found'}, status=200 if result is not None else 404,headers=headers)

    def mount(self, app):
        app.router.add_get('/api/flight-tracks', self.history)

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if self._store is not None:
            self._store.close()
            self._store = None

_service = None

def enabled():
    return os.getenv('FLIGHT_TRACKS_ENABLED','0') == '1'

def get_service():
    global _service
    if _service is None:
        site = HERE.parent/'kazuar-avia.github.io'
        rules = Path(os.getenv('FLIGHT_TRACK_RULES_DIR',str(site if site.is_dir() else HERE/'flight_track_rules')))
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
