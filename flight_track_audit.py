"""Bounded, read-only-export audit of flight tracking; no external requests."""
import json
import logging
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flight_tracker import TrackStore, RETENTION

LOG = logging.getLogger('flight_tracks')
LIMIT_BYTES = 100_000_000
PART_BYTES = 5_000_000
PARTS = LIMIT_BYTES // PART_BYTES


class Audit:
    def __init__(self, directory, part_bytes=PART_BYTES, parts=PARTS):
        self.directory = Path(directory)
        self.part_bytes = part_bytes
        self.parts = parts
        self.lock = threading.RLock()
        self.failed = False

    def path(self, i):
        return self.directory / ('track-audit.jsonl' + (f'.{i}' if i else ''))

    def event(self, event, **fields):
        """Audit failure must not interrupt the flight tracker."""
        try:
            line = (json.dumps(dict(utc=datetime.now(timezone.utc).isoformat(), event=event, **fields),
                               ensure_ascii=False, separators=(',', ':'), default=str) + '\n').encode('utf-8')
            if len(line) > self.part_bytes:
                line = (json.dumps({'utc': datetime.now(timezone.utc).isoformat(), 'event': 'oversize_event',
                                    'original_event': event, 'bytes': len(line)}) + '\n').encode('utf-8')
            if len(line) > self.part_bytes:
                return
            with self.lock:
                self.directory.mkdir(parents=True, exist_ok=True)
                active = self.path(0)
                if active.exists() and active.stat().st_size + len(line) > self.part_bytes:
                    self.path(self.parts - 1).unlink(missing_ok=True)
                    for i in range(self.parts - 2, -1, -1):
                        if self.path(i).exists():
                            self.path(i).replace(self.path(i + 1))
                with active.open('ab') as f:
                    f.write(line)
            if self.failed:
                print('[Flight audit] Storage recovered', flush=True)
            self.failed = False
        except Exception as exc:
            if not self.failed:
                LOG.warning('Flight audit unavailable (%s)', type(exc).__name__)
            self.failed = True

    def files(self):
        return [self.path(i) for i in range(self.parts - 1, -1, -1) if self.path(i).is_file()]

    def export(self, output_dir, state):
        """Export chronological plain JSONL, splitting only at segment boundaries."""
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        limit = 7_500_000
        files = []
        handles = []
        with self.lock:
            try:
                for path in self.files():
                    f = path.open('rb')
                    handles.append((f, path.stat().st_size))
            except BaseException:
                for f, _ in handles:
                    f.close()
                raise
        try:
            state_line = (json.dumps({'event': 'export_state', 'state': state},
                                     ensure_ascii=False, separators=(',', ':'), default=str) + '\n').encode('utf-8')
            # Capture all complete records at the sizes observed under the lock.
            # Rotation can rename files, but the open Linux handles remain valid.
            batches = [[f, size] for f, size in handles]
            if len(state_line) <= limit:
                batches.append([None, len(state_line)])
            used = 0
            for f, size in batches:
                if size > limit:
                    raise ValueError('Audit segment exceeds attachment limit')
                if not files or used + size > limit:
                    files.append(out / f'track-audit-{len(files)+1:02d}.jsonl')
                    used = 0
                with files[-1].open('ab') as target:
                    if f is None:
                        target.write(state_line)
                    else:
                        remaining = size
                        while remaining:
                            data = f.read(min(remaining, 65536))
                            if not data:
                                raise OSError('Audit segment ended unexpectedly')
                            target.write(data)
                            remaining -= len(data)
                used += size
            if len(state_line) > limit:
                # Avoid a giant attachment while preserving the state report.
                state_fields = dict(state)
                flights = state_fields.pop('flights', [])
                records = [{'event': 'export_state', 'state': state_fields,
                            'flight_count': len(flights)}]
                records.extend({'event': 'export_state_flight', 'flight': row} for row in flights)
                for record in records:
                    line = (json.dumps(record, ensure_ascii=False, separators=(',', ':'), default=str) + '\n').encode('utf-8')
                    if len(line) > limit:
                        raise ValueError('Audit state record exceeds attachment limit')
                    if not files or used + len(line) > limit:
                        files.append(out / f'track-audit-{len(files)+1:02d}.jsonl')
                        used = 0
                    with files[-1].open('ab') as target:
                        target.write(line)
                    used += len(line)
            if len(files) == 1:
                target = out / 'track-audit.jsonl'
                files[0].replace(target)
                return [target]
            return files
        finally:
            for f, _ in handles:
                f.close()


class AuditedStore(TrackStore):
    def __init__(self, filename, audit):
        self.filename = Path(filename)
        self.audit = audit
        super().__init__(filename)
        self.audit.event('database_open', retention_seconds=RETENTION, state=self.safe_state())

    def state(self):
        # Includes the exact internal flight id and last real point; queries only.
        rows = {}
        for r in self.db.execute('''SELECT f.*, (SELECT COUNT(*) FROM points WHERE flight_id=f.id) AS points
                                    FROM flights f ORDER BY f.id'''):
            item = dict(r)
            last = self.db.execute('SELECT * FROM points WHERE flight_id=? ORDER BY id DESC LIMIT 1', (r['id'],)).fetchone()
            item['last_point'] = dict(last) if last else None
            rows[r['id']] = item
        return rows

    def safe_state(self):
        try:
            return self.state()
        except Exception as exc:
            self.audit.event('audit_read_error', error=type(exc).__name__)
            return None

    def diff(self, before, after, operation, now):
        if before is None or after is None:
            self.audit.event('audit_diff_unavailable', operation=operation)
            return
        for fid in sorted(before.keys() - after.keys()):
            old = before[fid]
            self.audit.event('flight_deleted', operation=operation, at=now, flight=old,
                             seconds_missing=now-old['missing_since'] if old['missing_since'] is not None else None,
                             deleted_points=old['points'])
        for fid, row in after.items():
            old = before.get(fid)
            if old is None:
                self.audit.event('flight_created', operation=operation, at=now, flight=row)
            else:
                if old['missing_since'] is None and row['missing_since'] is not None:
                    self.audit.event('flight_missing', operation=operation, at=now, flight=row,
                                     expires_at=row['missing_since']+RETENTION)
                if old['missing_since'] is not None and row['missing_since'] is None:
                    self.audit.event('flight_returned', operation=operation, at=now, flight=row,
                                     seconds_missing=now-old['missing_since'])
                if old['session'] != row['session']:
                    self.audit.event('session_changed', operation=operation, at=now, flight=row, previous_session=old['session'])
            if row['points'] > (old['points'] if old else 0):
                self.audit.event('point_saved', operation=operation, at=now, flight_id=fid,
                                 source=row['source'], callsign=row['callsign'], point=row['last_point'], points=row['points'])

    def ingest(self, source, rows, now=None, stamp=None):
        now = time.time() if now is None else now
        stamp = now if stamp is None else stamp
        before = self.safe_state()
        try:
            result = super().ingest(source, rows, now, stamp)
        except Exception as exc:
            self.audit.event('snapshot_rollback', source=source, at=now, stamp=stamp, error=type(exc).__name__)
            raise
        after = self.safe_state()
        try:
            self.diff(before, after, 'ingest:'+source, now)
            added = []
            if before is not None and after is not None:
                added = [r['flight_key'] for fid,r in after.items() if r['source']==source and r['points']>(before.get(fid) or {}).get('points',0)]
            self.audit.event('snapshot', source=source, at=now, stamp=stamp, accepted=result,
                             received=len(rows), saved_points=len(added),
                             # Every filtered row, including unchanged / missing positions.
                             rows=[{'key':r['key'],'session':r['session'],'callsign':r['callsign'],
                                    'dep':r['dep'],'arr':r['arr'],'pos':r['pos'],'alt':r['alt'],
                                    'heading':r['heading'],'observed':r['observed'],'speed':r['speed'],
                                    'outcome':('duplicate_snapshot' if not result else 'point_saved' if r['key'] in added else
                                               'missing_position' if r['pos'] is None else 'unchanged_or_stale')} for r in rows])
        except Exception as exc:
            self.audit.event('audit_diff_error', error=type(exc).__name__)
        return result

    def report(self):
        files = {}
        for suffix in ('', '-wal', '-shm'):
            path = Path(str(self.filename)+suffix)
            if path.exists():
                st = path.stat()
                files[path.name] = {'bytes': st.st_size, 'allocated_bytes': getattr(st,'st_blocks',0)*512 if hasattr(st,'st_blocks') else None}
        disk = shutil.disk_usage(self.filename.parent)
        return {'utc':datetime.now(timezone.utc).isoformat(), 'retention_seconds':RETENTION,
                'audit_limit_bytes':self.audit.parts*self.audit.part_bytes,
                'flights':list(self.state().values()),
                'snapshots':[dict(r) for r in self.db.execute('SELECT * FROM snapshots')],
                'database_files':files, 'sqlite':{k:self.db.execute('PRAGMA '+k).fetchone()[0] for k in ('page_size','page_count','freelist_count','journal_mode')},
                'volume':dict(total=disk.total,used=disk.used,free=disk.free),
                'audit_bytes':sum(p.stat().st_size for p in self.audit.files())}
