import asyncio
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase
import bot_flight_tracks as service


def ns(lat=49.0):
    return {'totalResults':1,'results':[{'_id':'flight-1','flightNumber':'2410','airline':{'icao':'UKL'},'dep':{'icao':'UKKK'},'arr':{'icao':'UKLL'},'lastState':{'location':{'lat':lat,'lon':30,'hdg':270,'alt':9000}}}]}

def vat(now, lat=49):
    stamp=datetime.fromtimestamp(now,timezone.utc).isoformat()
    return {'general':{'update_timestamp':stamp},'pilots':[{'cid':123,'callsign':'PDA1','logon_time':'session1','last_updated':stamp,'latitude':lat,'longitude':30,'altitude':10000,'heading':270,'groundspeed':300,'flight_plan':{'departure':'UKKK','arrival':'UKLL'}}]}

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        p=Path(self.tmp.name); (p/'listofUKRaircompanies.txt').write_text('PDA Podillia\n'); (p/'ruBanned.txt').write_text('UUEE\n')
        self.s=service.Service(p/'db.sqlite3',p)
        self.addCleanup(lambda: asyncio.run(self.s.close()))
    def test_newsky_history(self):
        self.assertTrue(self.s.newsky(ns())); self.assertTrue(self.s.newsky(ns(49.01)))
        points=self.s.store().history('newsky','flight-1','flight-1')['points']; self.assertEqual(len(points),2); self.assertIsNone(points[-1]['alt'])
    def test_duplicate_position(self):
        self.s.newsky(ns()); self.s.newsky(ns()); self.assertEqual(len(self.s.store().history('newsky','flight-1')['points']),1)
    def test_incomplete_and_failed(self):
        self.s.newsky(ns()); self.assertFalse(self.s.newsky(None)); self.assertFalse(self.s.newsky({'results':[],'totalResults':2})); self.assertFalse(self.s.newsky({'results':[]})); self.assertIsNone(self.s.store().history('newsky','flight-1')['missing_since'])
    def test_complete_empty_and_expiry(self):
        self.s.newsky(ns()); self.s.newsky({'results':[],'totalResults':0}); missing=self.s.store().history('newsky','flight-1')['missing_since']; self.s.expire(missing+1799); self.assertIsNotNone(self.s.store().history('newsky','flight-1')); self.s.expire(missing+1800); self.assertIsNone(self.s.store().history('newsky','flight-1'))
    def test_reconnect(self):
        self.s.newsky(ns()); self.s.newsky({'results':[],'totalResults':0}); self.s.newsky(ns(49.01)); h=self.s.store().history('newsky','flight-1'); self.assertIsNone(h['missing_since']); self.assertEqual(len(h['points']),2)
    def test_vatsim(self):
        t=time.time(); self.assertTrue(self.s.vatsim(vat(t),t)); self.assertFalse(self.s.vatsim(vat(t),t)); self.assertTrue(self.s.vatsim(vat(t+15,49.01),t+15)); self.assertEqual(self.s.store().summary()[0]['points'],2)
    def test_stale(self):
        t=time.time(); self.s.vatsim(vat(t),t); self.assertRaises(ValueError,self.s.vatsim,vat(t),t+181); self.assertIsNone(self.s.store().summary()[0]['missing_since'])
    def test_isolation(self):
        t=time.time(); self.s.vatsim(vat(t),t); self.s.newsky(ns()); self.s.newsky({'results':[],'totalResults':0}); self.assertIsNone(next(x for x in self.s.store().summary() if x['source']=='vatsim')['missing_since'])
    def test_restart(self):
        self.s.newsky(ns()); asyncio.run(self.s.close()); self.assertEqual(self.s.store().summary()[0]['points'],1)
    def test_disabled(self):
        with patch.dict(os.environ,{'FLIGHT_TRACKS_ENABLED':'0'}), patch.object(service,'get_service',side_effect=AssertionError('disabled')):
            service.start(); service.ingest_newsky(ns()); service.mount(web.Application())
    def test_hook_failure_isolated(self):
        with patch.dict(os.environ,{'FLIGHT_TRACKS_ENABLED':'1'}), patch.object(service,'get_service',side_effect=OSError('unavailable')):
            service.start(); service.ingest_newsky(ns()); service.mount(web.Application())

class HTTPTests(AioHTTPTestCase):
    async def get_application(self):
        self.tmp=tempfile.TemporaryDirectory(); self.s=service.Service(Path(self.tmp.name)/'db',self.tmp.name); self.s.newsky(ns()); app=web.Application(); app.router.add_post('/webhook',self.webhook); self.s.mount(app); return app
    async def webhook(self,request): return web.Response(text='original')
    async def asyncTearDown(self):
        await super().asyncTearDown(); await self.s.close(); self.tmp.cleanup()
    async def test_history(self):
        r=await self.client.get('/api/flight-tracks?source=newsky&key=flight-1',headers={'Origin':'http://127.0.0.1:8000'}); self.assertEqual(r.status,200); self.assertEqual(r.headers['Access-Control-Allow-Origin'],'http://127.0.0.1:8000'); self.assertEqual((await r.json())['points'][0]['lat'],49)
    async def test_no_write(self):
        r=await self.client.post('/api/flight-tracks?source=newsky&key=flight-1'); self.assertEqual(r.status,405)
        r=await self.client.get('/api/flight-tracks/summary'); self.assertEqual(r.status,404)
    async def test_no_other_origin(self):
        r=await self.client.get('/api/flight-tracks?source=newsky&key=flight-1',headers={'Origin':'https://other.example'}); self.assertNotIn('Access-Control-Allow-Origin',r.headers)
    async def test_webhook_preserved(self):
        r=await self.client.post('/webhook'); self.assertEqual(await r.text(),'original')
    async def test_missing_session(self):
        r=await self.client.get('/api/flight-tracks?source=vatsim&key=x'); self.assertEqual(r.status,400)
    async def test_single_task(self):
        with patch.object(self.s,'run',side_effect=lambda: asyncio.sleep(60)):
            self.assertIs(self.s.start(),self.s.start())
        await self.s.close()

if __name__=='__main__': unittest.main()
