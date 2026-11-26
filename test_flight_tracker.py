import unittest
import tempfile
from pathlib import Path
from flight_tracker import TrackStore, rows_from_newsky, rows_from_vatsim


def vatsim(lon=30, session='s1', altitude=12000, cs='UKL123', cid=1, speed=350):
    return rows_from_vatsim({'pilots':[{'cid':cid,'callsign':cs,'logon_time':session,'latitude':50,'longitude':lon,'altitude':altitude,'heading':90,'groundspeed':speed,'flight_plan':{'departure':'UKBB','arrival':'EPWA'}}]}, {'UKL'}, set())


def newsky(lon=30):
    return rows_from_newsky({'totalResults':1,'results':[{'_id':'f1','flightNumber':'123','airline':{'icao':'UKL'},'lastState':{'location':{'lat':50,'lon':lon,'alt':777,'hdg':90}}}]})


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'test.sqlite3'
        self.s=TrackStore(self.path)
    def tearDown(self):
        self.s.close(); self.tmp.cleanup()
    def test_sources_separate_and_persist(self):
        v=vatsim(); self.s.ingest('vatsim',v,1000); self.s.ingest('newsky',newsky(),1000)
        self.s.ingest('vatsim',vatsim(30.1,altitude=13000),1030)
        self.s.close(); self.s=TrackStore(self.path)
        self.assertEqual([p['alt'] for p in self.s.history('vatsim',v[0]['key'])['points']],[12000,13000])
        self.assertIsNone(self.s.history('newsky','f1')['points'][0]['alt'])
    def test_offline_retention(self):
        self.s.ingest('newsky',newsky(),1000); self.s.ingest('newsky',[],1030)
        self.s.ingest('vatsim',[],4000)
        self.assertIsNotNone(self.s.history('newsky','f1'))
        self.s.ingest('newsky',[],2829); self.assertIsNotNone(self.s.history('newsky','f1'))
        self.s.ingest('newsky',[],2830); self.assertIsNone(self.s.history('newsky','f1'))
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM points').fetchone()[0],0)
    def test_return_before_expiry(self):
        self.s.ingest('newsky',newsky(),1000); self.s.ingest('newsky',[],1030)
        self.s.ingest('newsky',newsky(30.1),1100)
        r=self.s.history('newsky','f1'); self.assertIsNone(r['missing_since']); self.assertEqual(len(r['points']),2)
        self.assertEqual(r['points'][-1]['break_before'],1)
    def test_newsky_return_after_expiry(self):
        self.s.ingest('newsky',newsky(),1000); self.s.ingest('newsky',[],1030)
        self.s.ingest('newsky',newsky(30.1),2830)
        self.assertEqual(len(self.s.history('newsky','f1')['points']),1)
    def test_reconnect(self):
        v=vatsim(); self.s.ingest('vatsim',v,1000); self.s.ingest('vatsim',[],1030)
        self.s.ingest('vatsim',vatsim(30.05,'s2'),1060)
        self.assertEqual(len(self.s.history('vatsim',v[0]['key'],'s2')['points']),2)
    def test_distinct_flight(self):
        v=vatsim(); self.s.ingest('vatsim',v,1000)
        self.s.ingest('vatsim',vatsim(70,'s2'),1030)
        self.assertEqual(len(self.s.history('vatsim',v[0]['key'],'s2')['points']),1)
        self.assertEqual(len(self.s.history('vatsim',v[0]['key'],'s1')['points']),1)
    def test_duplicate_snapshot(self):
        self.s.ingest('newsky',newsky(),1000,stamp=100)
        self.assertFalse(self.s.ingest('newsky',[],1030,stamp=100))
        self.assertIsNone(self.s.history('newsky','f1')['missing_since'])
    def test_partial_snapshot(self):
        with self.assertRaises(ValueError): rows_from_newsky({'totalResults':2,'results':[]})
    def test_missing_position_not_offline(self):
        self.s.ingest('newsky',newsky(),1000)
        r=newsky(); r[0]['pos']=None; self.s.ingest('newsky',r,1030)
        self.assertIsNone(self.s.history('newsky','f1')['missing_since'])
    def test_filter(self):
        data={'pilots':[{'cid':1,'callsign':'ABC1','logon_time':'s','flight_plan':{'departure':'EGLL','arrival':'EDDF'}}]}
        self.assertEqual(rows_from_vatsim(data,{'UKL'},set()),[])
    def test_duplicate_point(self):
        self.s.ingest('vatsim',vatsim(),1000); self.s.ingest('vatsim',vatsim(),1030)
        self.assertEqual(self.s.summary()[0]['points'],1)

if __name__=='__main__': unittest.main(verbosity=2)
