#!/usr/bin/env python3
"""Offline security and file-format tests, no historical network requests."""
import contextlib, gzip, importlib, importlib.util, json, os, pathlib, socket, sys, tempfile, threading, types, unittest
from datetime import datetime,timezone,timedelta
from unittest.mock import patch

DIR=pathlib.Path(__file__).resolve().parent
sys.path.insert(0,str(DIR))
import scenario_backend as b
import scenario_converter as conv
import scenario_geography as geo
import scenario_lsrs as lsrs
import simulator_safe_server as safe

class CoreTests(unittest.TestCase):
    def spec(self):
        return dict(source='KTLX',destination='KLNX',wfo='LBF',start='2024-04-14T00:00Z',end='2024-04-14T01:00Z')
    def test_spec_and_host(self):
        s=b.parse_spec(self.spec());self.assertTrue(s['relocated'])
        for invalid in [{'source':'../../'}, {'end':'2024-04-14T09:00Z'}, {'start':'2024-04-14T00:00'}, {'destination':'KAAA'}]:
            d=self.spec()|invalid
            with self.assertRaises(ValueError):b.parse_spec(d)
        class Dummy:
            _expected_origin=b.ScenarioHandler._expected_origin
            server=types.SimpleNamespace(server_port=8765)
            headers={'Host':'127.0.0.1:8765','Origin':'http://127.0.0.1:8765','Sec-Fetch-Site':'same-origin'}
        self.assertTrue(b.ScenarioHandler._api_host_ok(Dummy()))
        Dummy.headers={'Host':'example.org','Origin':'http://example.org'}
        self.assertFalse(b.ScenarioHandler._api_host_ok(Dummy()))
        with patch.dict(os.environ, {'CODESPACES':'true','CODESPACE_NAME':'wds-test-567'},clear=False):
            Dummy.headers={'Host':'wds-test-567-8765.app.github.dev','Origin':'https://wds-test-567-8765.app.github.dev','Sec-Fetch-Site':'same-origin'}
            self.assertTrue(b.ScenarioHandler._api_host_ok(Dummy()))
            Dummy.headers['Origin']='https://evil.example'
            self.assertFalse(b.ScenarioHandler._api_host_ok(Dummy()))

    def test_static_file_guard(self):
        class Dummy:
            path='/scenario_cache/secret.txt'
        # Must use the strict path grammar even if a private file exists.
        self.assertIsNone(safe.SafeHandler._allowed(Dummy()))
        Dummy.path='/web_data/scenarios/0000000000000000/../secrets.txt'
        self.assertIsNone(safe.SafeHandler._allowed(Dummy()))
        Dummy.path='/web_data/scenarios/0000000000000000/moments/../x.gz'
        self.assertIsNone(safe.SafeHandler._allowed(Dummy()))
        Dummy.path='/scenario_builder.html'
        self.assertTrue(safe.SafeHandler._allowed(Dummy()))

    def test_lsrs_keep_original_out_of_relocated(self):
        rows=[{'VALID':'2024-04-14 00:15:00','TYPETEXT':'HAIL','MAG':'2.0','SOURCE':'TRAINED SPOTTER',
                'REMARK':'Two miles northeast of Oklahoma City','LAT':'35.4','LON':'-97.3','CITY':'OKLAHOMA CITY',
                'COUNTY':'OKLAHOMA','STATE':'OK'}]
        x=lsrs._reports(rows,self.spec(),{'lat':35.3331,'lon':-97.2775})
        self.assertEqual(len(x),1)
        self.assertEqual(x[0]['source'],'TRAINED SPOTTER')
        self.assertNotIn('original',x[0]);self.assertNotIn('REMARK',json.dumps(x))
        self.assertNotIn('OKLAHOMA CITY',json.dumps(x))
        self.assertNotEqual(x[0]['relocated']['lat'],35.4)
        y=lsrs._reports(rows,self.spec()|{'destination':'KTLX'}, {'lat':35.3331,'lon':-97.2775})
        self.assertEqual(y[0]['original']['remarks'],rows[0]['REMARK'])

    def test_geography_json_schema(self):
        coords=[[[-101,41],[-100,41],[-100,42],[-101,42],[-101,41]]]
        def feat(name,**kw):
            return {'type':'Feature','properties':{'cwa':'LBF','wfo':'LBF','city':'North Platte',
                    'citystate':'North Platte NE','time_zone':'C','countyname':'Lincoln','state':'NE','fips':'31111',
                    'BASENAME':'North Platte','lon':-100.5,'lat':41.5,**kw},
                    'geometry':{'type':'Polygon','coordinates':coords}}
        def query(url,where='1=1',bounds=None,max_count=1500):
            if url.endswith('/1'):return [feat('cwa')]
            if url.endswith('/2'):return [feat('countyname')]
            if url.endswith('/3'):return [feat('state')]
            if 'Places' in url:return [feat('BASENAME')]
            if url.endswith('/0') or url.endswith('/1'):return []
            return []
        with tempfile.TemporaryDirectory() as tmp,patch.object(geo,'_fetch',side_effect=query):
            t=pathlib.Path(tmp);dest={'id':'KLNX','lat':41.9579,'lon':-100.5763}
            result=geo.build_geography('LBF',dest,t/'ref',t/'stage',lambda msg:None)
            self.assertEqual(result['wfo'],'LBF');self.assertEqual(result['time_zone'],'central')
            static=json.loads((t/'stage'/'basemap_static.json').read_text())
            warn=json.loads((t/'stage'/'warning_geography.json').read_text())
            self.assertEqual(static['schema'],'warning-simulator-static-basemap-v2')
            self.assertEqual(warn['schema'],'warning-simulator-warning-geo-v1')
            self.assertTrue(static['layers']['city']);self.assertTrue(warn['layers']['county'])
            self.assertEqual(geo.build_geography('LBF',dest,t/'ref',t/'stage2',lambda msg:None)['wfo'],'LBF')

    @unittest.skipUnless(importlib.util.find_spec('numpy'), 'NumPy not installed; run SETUP_PREP_CODESPACES.sh')
    def test_converter_writes_native_binary(self):
        import numpy as np
        class Radar:
            nsweeps=1
            time={'data':[0,1,2],'units':'seconds since 2024-04-14T00:00:00Z'}
            elevation={'data':np.array([.5,.5,.5])}
            azimuth={'data':np.array([0,120,240])}
            range={'data':np.array([500,750,1000])}
            fixed_angle={'data':np.array([.5])}
            sweep_start_ray_index={'data':np.array([0])}
            sweep_end_ray_index={'data':np.array([2])}
            altitude={'data':np.array([800])}
            fields={'reflectivity':{'data':np.ma.array([[30,40,50],[20,25,35],[5,10,15]])},
                    'velocity':{'data':np.ma.array([[-10,-5,0],[0,5,10],[4,6,8]])}}
        fake_pyart=types.ModuleType('pyart');fake_pyart.io=types.SimpleNamespace(read_nexrad_archive=lambda p:Radar())
        fake_netcdf=types.ModuleType('netCDF4')
        fake_netcdf.num2date=lambda values,units,**kwargs:[datetime(2024,4,14,tzinfo=timezone.utc)+timedelta(seconds=float(v)) for v in values]
        with tempfile.TemporaryDirectory() as tmp,patch.dict(sys.modules,{'pyart':fake_pyart,'netCDF4':fake_netcdf}):
            t=pathlib.Path(tmp);r=t/'raw';r.mkdir();name='KTLX20240414_000000_V06'
            (r/name).write_bytes(b'fake level2')
            meta={'_base':str(t),'spec':{'source':'KTLX','destination':'KLNX','wfo':'LBF'},
                'volumes':[{'file':name,'bytes':len(b'fake level2')}]}
            result=conv.convert_cached(meta,t/'stage',lambda *args:None)
            s=result['volumes'][0]['sweeps'][0]
            self.assertEqual(set(s['moments']),{'DBZH','VRADH'})
            f=t/'stage'/s['moments']['VRADH']['path']
            values=np.frombuffer(gzip.decompress(f.read_bytes()),dtype='<f4').reshape(3,3)
            self.assertEqual(values[0,0],-10)
            q=t/'stage'/s['moments']['VRADH']['quality_path']
            self.assertTrue(all(x==2 for x in gzip.decompress(q.read_bytes())))
            self.assertEqual(result['radar'],'KLNX')

    @unittest.skipUnless(importlib.util.find_spec('numpy'), 'NumPy not installed; run SETUP_PREP_CODESPACES.sh')
    def test_complete_preparation_synthetic_keeps_existing_case(self):
        """Exercise the existing server's complete publish sequence without network."""
        import numpy as np
        class Radar:
            nsweeps=1
            time={'data':[0,1,2],'units':'seconds since 2024-04-14T00:00:00Z'}
            elevation={'data':np.array([.5,.5,.5])}
            azimuth={'data':np.array([0,120,240])}
            range={'data':np.array([500,750,1000])}
            fixed_angle={'data':np.array([.5])}
            sweep_start_ray_index={'data':np.array([0])}
            sweep_end_ray_index={'data':np.array([2])}
            altitude={'data':np.array([800])}
            fields={'reflectivity':{'data':np.ma.array([[30,40,50],[20,25,35],[5,10,15]])},
                    'velocity':{'data':np.ma.array([[-10,-5,0],[0,5,10],[4,6,8]])}}
        fake_pyart=types.ModuleType('pyart');fake_pyart.io=types.SimpleNamespace(read_nexrad_archive=lambda p:Radar())
        fake_netcdf=types.ModuleType('netCDF4')
        fake_netcdf.num2date=lambda values,units,**kwargs:[datetime(2024,4,14,tzinfo=timezone.utc)+timedelta(seconds=float(v)) for v in values]
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules,{'pyart':fake_pyart,'netCDF4':fake_netcdf}):
            root=pathlib.Path(tmp);cache=root/'scenario_cache';cache.mkdir()
            existing=root/'web_data'/'scenarios'/'1fdcdaeeeca6f7d3'
            existing.mkdir(parents=True)
            (existing/'sentinel.txt').write_text('DO NOT CHANGE')
            spec=b.parse_spec(self.spec());ident=b.identifier(spec)
            raw=cache/ident/'raw';raw.mkdir(parents=True)
            name='KTLX20240414_000000_V06'
            (raw/name).write_bytes(b'fake radar')
            manifest={'status':'RAW_DOWNLOADED_NOT_PLAYABLE','scenarioId':ident,
                      'spec':b.serialize_spec(spec),
                      'volumes':[{'file':name,'bytes':len(b'fake radar')}]}
            (raw.parent/'raw_manifest.json').write_text(json.dumps(manifest))
            def geography(wfo,dest,ref,stage,progress):
                geo={'schema':'warning-simulator-warning-geo-v1','layers':{'county':[], 'cwa':[]}}
                bm={'schema':'warning-simulator-static-basemap-v2','layers':{}}
                (pathlib.Path(stage)/'warning_geography.json').write_text(json.dumps(geo))
                (pathlib.Path(stage)/'basemap_static.json').write_text(json.dumps(bm))
                return {'wfo':wfo,'cwa_counties':1,'mapped_places':1,'primary_road_segments':0,
                        'secondary_road_segments':0,'time_zone':'central','office':{'citystate':'North Platte NE','city':'North Platte'}}
            def lsrs_dummy(spec,source,base,stage,progress):
                (pathlib.Path(stage)/'storm_reports.json').write_text(json.dumps({'schema':'warning-simulator-lsrs-v1','allReports':[],'suggestedIds':[]}))
                return {'count':0,'suggested':0,'status':'unavailable'}
            with (patch.object(b,'ROOT',root),patch.object(b,'CACHE_DIR',cache),
                  patch('scenario_geography.build_geography',side_effect=geography),
                  patch('scenario_lsrs.build_lsrs',side_effect=lsrs_dummy)):
                b.JOB={'state':'preparing','message':'','completed':0,'total':0}
                b._prepare_cached(spec)
                self.assertEqual(b.JOB['state'],'prepared',b.JOB.get('message'))
                created=root/'web_data'/'scenarios'/b.JOB['scenarioId']
                self.assertEqual(json.loads((created/'manifest.json').read_text())['scenario_id'],created.name)
                self.assertEqual((existing/'sentinel.txt').read_text(),'DO NOT CHANGE')
                self.assertTrue((created/'moments').is_dir())


if __name__=='__main__':unittest.main(verbosity=2)
