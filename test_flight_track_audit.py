import asyncio
import json
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from flight_track_audit import Audit, AuditedStore, LIMIT_BYTES, PART_BYTES, PARTS
from test_flight_tracker import newsky, vatsim
from test_bot_flight_tracks import ns, vat
from bot_flight_tracks import Service


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.a=Audit(self.root/'audit')
        self.s=AuditedStore(self.root/'db.sqlite3',self.a)
        self.addCleanup(self.s.close)
    def events(self):
        return [json.loads(line) for f in self.a.files() for line in f.read_text(encoding='utf-8').splitlines()]
    def test_100MB_limit(self):
        self.assertEqual(PART_BYTES*PARTS,100_000_000)
        self.assertEqual(LIMIT_BYTES,100_000_000)
    def test_actual_expiry_and_cascade(self):
        self.s.ingest('newsky',newsky(),1000)
        self.s.ingest('newsky',newsky(30.1),1010)
        self.s.ingest('newsky',[],1020)
        self.s.ingest('newsky',[],2819)
        self.assertFalse(any(e['event']=='flight_deleted' for e in self.events()))
        self.s.ingest('newsky',[],2820)
        event=next(e for e in self.events() if e['event']=='flight_deleted')
        self.assertEqual(event['seconds_missing'],1800)
        self.assertEqual(event['deleted_points'],2)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM points').fetchone()[0],0)
    def test_reconnect(self):
        self.s.ingest('vatsim',vatsim(),1000)
        self.s.ingest('vatsim',[],1030)
        self.s.ingest('vatsim',vatsim(30.05,'s2'),1060)
        e=self.events(); self.assertTrue(any(x['event']=='session_changed' for x in e))
        self.assertTrue(any(x['event']=='flight_returned' for x in e))
        self.assertEqual([x['point']['break_before'] for x in e if x['event']=='point_saved'],[0,1])
    def test_duplicate_missing_position_and_snapshot(self):
        self.s.ingest('newsky',newsky(),1000)
        self.s.ingest('newsky',newsky(),1010)
        rows=newsky();rows[0]['pos']=None
        self.s.ingest('newsky',rows,1020)
        self.s.ingest('newsky',[],1030,stamp=1020)
        snapshots=[e for e in self.events() if e['event']=='snapshot']
        self.assertEqual(snapshots[1]['rows'][0]['outcome'],'unchanged_or_stale')
        self.assertEqual(snapshots[2]['rows'][0]['outcome'],'missing_position')
        self.assertFalse(snapshots[-1]['accepted'])
        self.assertEqual(len([e for e in self.events() if e['event']=='point_saved']),1)
        self.assertFalse(any(e['event']=='flight_missing' for e in self.events()))
    def test_rollback(self):
        rows=newsky(); rows.append(dict(rows[0],key='bad',pos=(1,2),alt=object()))
        with self.assertRaises(Exception): self.s.ingest('newsky',rows,1000)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM flights').fetchone()[0],0)
        self.assertTrue(any(e['event']=='snapshot_rollback' for e in self.events()))
        self.assertFalse(any(e['event']=='point_saved' for e in self.events()))
    def test_rotation(self):
        a=Audit(self.root/'tiny',part_bytes=512,parts=3)
        for n in range(100): a.event('test',n=n,text='x'*90)
        self.assertLessEqual(sum(f.stat().st_size for f in a.files()),1536)
        records=[json.loads(l) for f in a.files() for l in f.read_text().splitlines()]
        self.assertEqual(records[-1]['n'],99)
        self.assertGreater(records[0]['n'],0)
        self.assertEqual([r['n'] for r in records],sorted(r['n'] for r in records))
    def test_logger_io_failure_isolated(self):
        self.a.directory=self.root/'not-a-directory';self.a.directory.write_text('x')
        self.assertTrue(self.s.ingest('newsky',newsky(),1000))
        self.assertTrue(self.a.failed)
        self.assertEqual(len(self.s.history('newsky','f1')['points']),1)
    def test_export_contains_audit_and_state(self):
        self.s.ingest('newsky',newsky(),1000)
        paths=self.a.export(self.root/'export',self.s.report())
        self.assertEqual(len(paths),1)
        with zipfile.ZipFile(paths[0]) as z:
            state=json.loads(z.read('state.json'))
            self.assertEqual(state['flights'][0]['callsign'],'UKL123')
            self.assertEqual(state['flights'][0]['points'],1)
            self.assertEqual(state['audit_limit_bytes'],100_000_000)
            self.assertIn('freelist_count',state['sqlite'])
            self.assertTrue(any(b'point_saved' in z.read(n) for n in z.namelist() if 'jsonl' in n))
    def test_restart_log_retained(self):
        self.s.ingest('newsky',newsky(),1000)
        other=AuditedStore(self.root/'db.sqlite3',self.a)
        try:
            self.assertEqual(other.summary()[0]['points'],1)
            self.assertEqual(len([e for e in self.events() if e['event']=='database_open']),2)
        finally: other.close()

class ServiceAuditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.s=Service(self.root/'db.sqlite3',self.root)
    async def asyncTearDown(self):
        await self.s.close();self.tmp.cleanup()
    async def test_service_expiry_path(self):
        self.s.newsky(ns());self.s.newsky({'results':[],'totalResults':0})
        missing=self.s.store().history('newsky','flight-1')['missing_since']
        self.s.expire(missing+1799);self.s.expire(missing+1800)
        events=[json.loads(l) for f in self.s.audit.files() for l in f.read_text().splitlines()]
        deleted=[e for e in events if e['event']=='flight_deleted']
        self.assertEqual(len(deleted),1);self.assertEqual(deleted[0]['operation'],'expiry')
        self.assertEqual(deleted[0]['deleted_points'],1)
    async def test_failed_snapshot(self):
        self.s.newsky(ns());self.s.newsky({'results':[],'totalResults':1})
        events=[json.loads(l) for f in self.s.audit.files() for l in f.read_text().splitlines()]
        self.assertTrue(any(e['event']=='snapshot_rejected' and e['declared_count']==1 and e['actual_count']==0 for e in events))
        self.assertIsNone(self.s.store().history('newsky','flight-1')['missing_since'])
    async def test_export_command(self):
        self.s.newsky(ns()); sent=[]
        class File:
            def __init__(self,path,filename): self.path=Path(path);self.closed=False
            def close(self): self.closed=True
        class Channel:
            async def send(self,text=None,file=None):
                if file:
                    self_path=file.path; assert self_path.is_file()
                    with zipfile.ZipFile(self_path) as z: assert 'state.json' in z.namelist()
                    sent.append(file)
        await self.s.send_audit(Channel(),File)
        self.assertEqual(len(sent),1)
        self.assertTrue(sent[0].closed)
        self.assertFalse(sent[0].path.exists())

if __name__=='__main__': unittest.main()
