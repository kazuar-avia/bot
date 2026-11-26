"""Local-first flight history. No Discord connection, GitHub writes or NewSky HTTP calls."""
import argparse
import asyncio
import json
import math
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

VATSIM_URL = 'https://data.vatsim.net/v3/vatsim-data.json'
INTERVAL = 15
RETENTION = 1800


def norm(value):
    return str(value or '').strip().upper()


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (ValueError, TypeError):
        return None


def position(lat, lon):
    lat, lon = number(lat), number(lon)
    if lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180:
        return None
    return round(lat, 6), round(lon, 6)


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt.timestamp() if dt.tzinfo else None
    except ValueError:
        return None


def distance_nm(a, b):
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp = p2 - p1
    dl = math.radians(b[1] - a[1])
    h = math.sin(dp / 2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl / 2)**2
    return 3440.065 * 2 * math.asin(min(1, math.sqrt(max(0, h))))


def rows_from_vatsim(data, airlines, banned):
    if not isinstance(data, dict) or not isinstance(data.get('pilots'), list):
        raise ValueError('Invalid VATSIM snapshot')
    rows = []
    for p in data['pilots']:
        if not isinstance(p, dict) or not p.get('cid') or not p.get('callsign'):
            raise ValueError('Incomplete VATSIM snapshot')
        fp = p.get('flight_plan') or {}
        cs, dep, arr = norm(p['callsign']), norm(fp.get('departure')), norm(fp.get('arrival'))
        if not fp or not (dep.startswith('UK') or arr.startswith('UK') or cs[:3] in airlines):
            continue
        if cs[:3] in airlines and (dep in banned or arr in banned):
            continue
        if not p.get('logon_time'):
            raise ValueError('Missing VATSIM session identifier')
        rows.append(dict(key=json.dumps([str(p['cid']), cs, dep, arr], separators=(',', ':')),
                         session=str(p['logon_time']), callsign=cs, dep=dep, arr=arr,
                         pos=position(p.get('latitude'), p.get('longitude')),
                         alt=number(p.get('altitude')), heading=number(p.get('heading')),
                         speed=number(p.get('groundspeed')), observed=timestamp(p.get('last_updated'))))
    return rows


def rows_from_newsky(data):
    if not isinstance(data, dict) or not isinstance(data.get('results'), list):
        raise ValueError('Invalid NewSky snapshot')
    results = data['results']
    if data.get('totalResults') is not None and data['totalResults'] != len(results):
        raise ValueError('Incomplete NewSky snapshot (pagination or count mismatch)')
    rows = []
    for f in results:
        if not isinstance(f, dict) or not (f.get('_id') or f.get('id')):
            raise ValueError('Missing NewSky flight ID')
        ident = str(f.get('_id') or f['id'])
        airline = norm((f.get('airline') or {}).get('icao') or 'UKL')
        loc = (f.get('lastState') or {}).get('location') or {}
        rows.append(dict(key=ident, session=ident, callsign=airline + norm(f.get('flightNumber')),
                         dep=norm((f.get('dep') or {}).get('icao')), arr=norm((f.get('arr') or {}).get('icao')),
                         pos=position(loc.get('lat'), loc.get('lon')), alt=None,
                         heading=number(loc.get('hdg')), speed=None, observed=None))
    return rows


class TrackStore:
    def __init__(self, filename):
        self.db = sqlite3.connect(filename, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA foreign_keys=ON;
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS flights (
                id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
                flight_key TEXT NOT NULL, session TEXT NOT NULL, callsign TEXT NOT NULL,
                dep TEXT NOT NULL, arr TEXT NOT NULL, last_seen REAL NOT NULL,
                missing_since REAL, last_observed REAL, last_speed REAL
            );
            CREATE INDEX IF NOT EXISTS flights_lookup ON flights(source,flight_key,id);
            CREATE TABLE IF NOT EXISTS points (
                id INTEGER PRIMARY KEY, flight_id INTEGER NOT NULL REFERENCES flights(id) ON DELETE CASCADE,
                t REAL NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
                alt REAL, heading REAL, break_before INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS points_lookup ON points(flight_id,id);
            CREATE TABLE IF NOT EXISTS snapshots (source TEXT PRIMARY KEY, stamp REAL NOT NULL);
        ''')

    def close(self):
        self.db.close()

    def ingest(self, source, rows, now=None, stamp=None):
        """Call only after validating a complete successful snapshot; failures never mean offline."""
        now = time.time() if now is None else now
        stamp = now if stamp is None else stamp
        keys = [r['key'] for r in rows]
        if source not in ('vatsim', 'newsky') or len(set(keys)) != len(keys):
            raise ValueError('Invalid or ambiguous snapshot')
        with self.db:
            previous = self.db.execute('SELECT stamp FROM snapshots WHERE source=?', (source,)).fetchone()
            if previous and stamp <= previous['stamp']:
                return False
            self.db.execute('DELETE FROM flights WHERE source=? AND missing_since IS NOT NULL AND missing_since<=?',
                            (source, now-RETENTION))
            active = {f['flight_key']: f for f in self.db.execute('SELECT * FROM flights WHERE source=? ORDER BY id', (source,))}
            seen = set()
            for row in rows:
                f = active.get(row['key'])
                last = self.db.execute('SELECT * FROM points WHERE flight_id=? ORDER BY id DESC LIMIT 1', (f['id'],)).fetchone() if f else None
                reuse = f is not None
                if f and f['session'] != row['session']:
                    # Reconnection is conservative: same CID/callsign/route, recent and plausible position.
                    gap = now - f['last_seen']
                    reuse = bool(last and row['pos'] and 0 <= gap < RETENTION and
                                 distance_nm((last['lat'],last['lon']),row['pos']) <= max(3, gap/3600*650+2))
                    # A new stationary/ground session can be a different flight on the same route.
                    if row['speed'] is not None and row['speed'] < 40:
                        reuse = False
                if not reuse:
                    if f:
                        self.db.execute('UPDATE flights SET missing_since=COALESCE(missing_since,?) WHERE id=?',(now,f['id']))
                    fid = self.db.execute('INSERT INTO flights(source,flight_key,session,callsign,dep,arr,last_seen,last_observed,last_speed) VALUES(?,?,?,?,?,?,?,?,?)',
                        (source,row['key'],row['session'],row['callsign'],row['dep'],row['arr'],now,row['observed'],row['speed'])).lastrowid
                    last = None
                    gap_break = False
                else:
                    fid = f['id']
                    gap_break = f['missing_since'] is not None or now-f['last_seen'] > 90
                    self.db.execute('UPDATE flights SET session=?,callsign=?,dep=?,arr=?,last_seen=?,missing_since=NULL,last_observed=?,last_speed=? WHERE id=?',
                                    (row['session'],row['callsign'],row['dep'],row['arr'],now,row['observed'],row['speed'],fid))
                seen.add(fid)
                if row['pos'] is None:
                    continue
                lat,lon = row['pos']
                if reuse and row['observed'] is not None and f['last_observed'] is not None and row['observed'] <= f['last_observed'] and f['session']==row['session'] and not gap_break:
                    continue
                if last and not gap_break and (last['lat'],last['lon'],last['alt'],last['heading']) == (lat,lon,row['alt'],row['heading']):
                    continue
                self.db.execute('INSERT INTO points(flight_id,t,lat,lon,alt,heading,break_before) VALUES(?,?,?,?,?,?,?)',
                                (fid,now,lat,lon,row['alt'],row['heading'],int(gap_break and last is not None)))
            for f in self.db.execute('SELECT id FROM flights WHERE source=? AND missing_since IS NULL',(source,)).fetchall():
                if f['id'] not in seen:
                    self.db.execute('UPDATE flights SET missing_since=? WHERE id=?',(now,f['id']))
            self.db.execute('INSERT INTO snapshots VALUES(?,?) ON CONFLICT(source) DO UPDATE SET stamp=excluded.stamp',(source,stamp))
        return True

    def history(self, source, key, session=None):
        # Session is optional for local inspection; production requests will bind to the selected marker.
        query = 'SELECT * FROM flights WHERE source=? AND flight_key=?'
        args = [source,key]
        if session is not None:
            query += ' AND session=?'
            args.append(session)
        f = self.db.execute(query+' ORDER BY id DESC LIMIT 1', args).fetchone()
        if not f:
            return None
        return dict(id=f['id'], source=source, callsign=f['callsign'], missing_since=f['missing_since'],
                    points=[dict(p) for p in self.db.execute('SELECT t,lat,lon,alt,heading,break_before FROM points WHERE flight_id=? ORDER BY id',(f['id'],))])

    def summary(self):
        return [dict(r) for r in self.db.execute('''SELECT f.source,f.callsign,f.dep,f.arr,f.missing_since,COUNT(p.id) AS points,
                (SELECT alt FROM points WHERE flight_id=f.id ORDER BY id DESC LIMIT 1) AS altitude_ft
                FROM flights f LEFT JOIN points p ON p.flight_id=f.id GROUP BY f.id ORDER BY f.source,f.callsign''')]


def codes(path):
    return {norm(line.split()[0]) for line in path.read_text(encoding='utf-8-sig').splitlines() if line.strip()}


async def collect_local(store, site, once=False):
    import aiohttp
    rule_names = ('listofUKRaircompanies.txt', 'ruBanned.txt')
    rules = next((p for p in (site/'data', site) if all((p/name).is_file() for name in rule_names)), site/'data')
    airlines, banned = codes(rules/'listofUKRaircompanies.txt'), codes(rules/'ruBanned.txt')
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        while True:
            started = time.monotonic()
            try:
                async with session.get(VATSIM_URL) as response:
                    response.raise_for_status()
                    data = await response.json()
                now = time.time()
                stamp = timestamp((data.get('general') or {}).get('update_timestamp'))
                if stamp is None or abs(now-stamp)>180:
                    raise ValueError('Missing or stale VATSIM update_timestamp')
                rows = rows_from_vatsim(data,airlines,banned)
                saved = store.ingest('vatsim',rows,now=now,stamp=stamp)
                print(f'{datetime.now(timezone.utc).isoformat(timespec="seconds")} VATSIM: {len(rows)} flights; '+('saved' if saved else 'duplicate snapshot'),flush=True)
                for r in store.summary():
                    if r['source']=='vatsim' and r['missing_since'] is None:
                        print(f"  {r['callsign']:10} {r['dep']}-{r['arr']}  points={r['points']}  alt={r['altitude_ft']} ft",flush=True)
            except Exception as exc:
                print(f'VATSIM ERROR (history retained): {exc}',flush=True)
                if once:
                    raise
            if once:
                return
            await asyncio.sleep(max(0,INTERVAL-(time.monotonic()-started)))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--site',type=Path,default=Path(__file__).resolve().parent.parent/'kazuar-avia.github.io')
    p.add_argument('--db',type=Path,default=Path(__file__).resolve().parent/'flight-tracks.local.sqlite3')
    p.add_argument('--once',action='store_true')
    p.add_argument('--summary',action='store_true')
    p.add_argument('--newsky-json',type=Path,help='Import one manually saved snapshot; no API requests')
    a=p.parse_args()
    store=TrackStore(a.db)
    try:
        if a.newsky_json:
            store.ingest('newsky',rows_from_newsky(json.loads(a.newsky_json.read_text(encoding='utf-8-sig'))))
            print('NewSky snapshot imported (altitude is NULL).')
        if a.summary or a.newsky_json:
            print(json.dumps(store.summary(),ensure_ascii=False,indent=2))
        else:
            print(f'Local database: {a.db}. Ctrl+C to stop.',flush=True)
            asyncio.run(collect_local(store,a.site,a.once))
    except KeyboardInterrupt:
        print('Stopped. History saved.')
    finally:
        store.close()


if __name__=='__main__':
    main()
