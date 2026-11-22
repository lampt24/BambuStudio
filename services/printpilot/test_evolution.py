import io,json,os,sys,tempfile,unittest,zipfile
sys.path.insert(0,os.path.dirname(__file__))
import evolution
from server import open_db,validate_settings,extract_3mf
from test_server import ServerTestCase,_jreq,_req,_make_minimal_3mf,DEVICE_TOKEN

def project(layer='0.2',override=False):
    out=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(_make_minimal_3mf())) as src,zipfile.ZipFile(out,'w') as dst:
        for n in src.namelist():
            if 'config' not in n.lower(): dst.writestr(n,src.read(n))
        dst.writestr('Metadata/project_settings.config',json.dumps({'layer_height':layer,'sparse_infill_density':'15%'}))
        dst.writestr('Metadata/plate_1.gcode','G28; stale')
        dst.writestr('Metadata/plate_1.json',json.dumps({'layer_height':'stale slice metadata'}))
        dst.writestr('Metadata/slice_info.config','<config><metadata key="layer_height" value="0.2"/></config>')
        if override: dst.writestr('Metadata/model_settings.config','<config><object><metadata key="layer_height" value="0.1"/></object></config>')
    return out.getvalue()

class ExportTests(unittest.TestCase):
    def test_geometry_ignores_settings_and_archive_names(self):
        self.assertEqual(evolution.model_key(project('0.2')),evolution.model_key(project('0.1')))
    def test_export_reextract_and_stale_removed(self):
        raw=project(); result=evolution.optimized(raw,[{'key':'layer_height','value':0.16},{'key':'sparse_infill_density','value':23}],validate_settings,extract_3mf)
        self.assertEqual(extract_3mf(result)['config']['layer_height'],'0.16')
        self.assertEqual(extract_3mf(result)['config']['sparse_infill_density'],'23%')
        with zipfile.ZipFile(io.BytesIO(result)) as z: self.assertFalse(any(n.endswith('.gcode') for n in z.namelist()))
        self.assertEqual(evolution.model_key(raw),evolution.model_key(result))
    def test_override_rejected(self):
        with self.assertRaisesRegex(ValueError,'override'): evolution.optimized(project(override=True),[{'key':'layer_height','value':0.16}],validate_settings,extract_3mf)
    def test_real_profiles_and_rfid_fail_closed(self):
        profiles=evolution.profiles(os.path.abspath(os.path.join(os.path.dirname(__file__),'../..')),'A1')
        self.assertTrue(profiles)
        t={'ams':{'units':[{'id':3,'slots':[{'id':2,'present':True,'rfid':False}]}]}}
        params=dict(ams_id=3,slot_id=2,profile_id=profiles[0]['id'],color='#123abc')
        self.assertEqual(evolution.ams_params(params,t,'idle',profiles)['tray_color'],'123ABCFF')
        del t['ams']['units'][0]['slots'][0]['rfid']
        with self.assertRaises(ValueError): evolution.ams_params(params,t,'idle',profiles)

class ContextTests(unittest.TestCase):
    def test_material_context_and_authoritative_ams(self):
        db=open_db(':memory:')
        review=json.dumps(dict(rating=2,issues=['warping'],comment='bad'))
        db.execute("INSERT INTO print_runs(id,model_key,printer_id,started_at,status,material,review) VALUES('a','key','p','now','completed','ABS',?)",(review,))
        self.assertEqual(evolution.feedback(db,'key',{'filament_type':['PLA']}),[])
        self.assertEqual(len(evolution.feedback(db,'key',{'filament_type':['ABS']})),1)
        merged=evolution.merge_telemetry({'ams':{'units':[{'id':0},{'id':1}]}},{'ams':{'units':[{'id':0}]}})
        self.assertEqual(merged['ams']['units'],[{'id':0}])
        normalized=evolution.clean_ams({'units':[{'id':0,'humidity':42,'humidity_unit':'%','humidity_raw':3,'slots':[{'id':1,'rfid_known':True}]}]})
        self.assertEqual(normalized['units'][0]['humidity_unit'],'%')
        self.assertTrue(normalized['units'][0]['slots'][0]['rfid_known'])
        db.close()

class HistoryAPI(ServerTestCase):
    def test_lifecycle_link_review_restart(self):
        def beat(status,t):
            code,_=_jreq(self._port,'POST','/api/heartbeat',dict(device_id='d',printer_id='p',status=status,telemetry=t),DEVICE_TOKEN)
            self.assertEqual(code,200)
        beat('printing',dict(task_id='1',subtask_id='10',job_name='same',gcode_state='RUNNING'))
        response=_req(self._port,'GET','/api/prints'); active=json.loads(response.read())['prints'][0]
        self.assertEqual(_jreq(self._port,'POST',f"/api/prints/{active['id']}/review",dict(rating=4))[0],409)
        beat('paused',{'progress':30})
        beat('idle',{'gcode_state':'FINISH'})
        response=_req(self._port,'GET','/api/prints'); runs=json.loads(response.read())['prints']
        self.assertEqual(len(runs),1); self.assertEqual(runs[0]['status'],'completed'); self.assertIsNone(runs[0]['analysis_id'])
        run_id=runs[0]['id']; db=open_db(os.path.join(self._tmpdir,'printpilot.db'))
        key=evolution.model_key(project()); db.execute("INSERT INTO analyses(id,created_at,filename,status,model_key) VALUES('abc','now','same','complete',?)",(key,));db.commit();db.close()
        self.assertEqual(_jreq(self._port,'POST',f'/api/prints/{run_id}/link',dict(analysis_id='abc'))[0],200)
        self.assertEqual(_jreq(self._port,'POST',f'/api/prints/{run_id}/review',dict(rating=4,issues=['stringing'],comment='good'))[0],200)
        self.assertEqual(_jreq(self._port,'POST',f'/api/prints/{run_id}/review',dict(rating=True))[0],400)
        response=_req(self._port,'GET','/api/analyses/abc/prints'); run=json.loads(response.read())['prints'][0]
        self.assertEqual(run['review']['rating'],4);self.assertEqual(run['settings'],{});self.assertEqual(run['linkage_method'],'manager')
        beat('printing',dict(task_id='2',subtask_id='11',gcode_state='RUNNING'))
        response=_req(self._port,'GET','/api/prints'); self.assertEqual(len(json.loads(response.read())['prints']),2)
    def test_history_sent_server_side_and_downloads(self):
        import base64
        from unittest.mock import patch
        raw=project()
        payload=dict(filename='renamed.3mf',project_base64=base64.b64encode(raw).decode())
        result=dict(summary='ok',changes=[dict(key='layer_height',old=0.2,value=0.16,reason='quality')],warnings=[],model={})
        with patch('server.ai_analyze',return_value=result) as ai:
            code,first=_jreq(self._port,'POST','/api/analyze',payload)
            self.assertEqual(code,200);self.assertEqual(first['status'],'complete')
            db=open_db(os.path.join(self._tmpdir,'printpilot.db'))
            review=dict(rating=2,issues=['stringing'],comment='observed',updated_at='now')
            db.execute("INSERT INTO print_runs(id,analysis_id,model_key,printer_id,started_at,status,review) VALUES('run',?,?, 'p','now','completed',?)",(first['id'],first['model_key'],json.dumps(review)))
            db.commit();db.close()
            payload['feedback_context']=[dict(comment='client injection')]
            code,second=_jreq(self._port,'POST','/api/analyze',payload)
            self.assertEqual(second['feedback_context'][0]['review']['comment'],'observed')
            self.assertEqual(ai.call_args.args[1]['feedback_context'][0]['id'],'run')
        response=_req(self._port,'GET',f"/api/analyses/{first['id']}/file");self.assertEqual(response.read(),raw)
        response=_req(self._port,'GET',f"/api/analyses/{first['id']}/optimized");self.assertEqual(response.status,200)
        self.assertEqual(extract_3mf(response.read())['config']['layer_height'],'0.16')

    def test_lost_result_expires_and_late_success_rejected(self):
        db=open_db(os.path.join(self._tmpdir,'printpilot.db'))
        db.execute("INSERT INTO printer_commands(id,created_at,updated_at,printer_id,command,params,status) VALUES('abc','2000-01-01T00:00:00Z','2000-01-01T00:00:00Z','p','light','{}','sent')")
        db.commit();db.close()
        response=_req(self._port,'GET','/api/commands/abc')
        self.assertEqual(json.loads(response.read())['status'],'expired')
        self.assertEqual(_jreq(self._port,'POST','/api/device/commands/result',dict(id='abc',ok=True,reason='late'),DEVICE_TOKEN)[0],404)

    def test_auth(self):
        for path in ('/api/prints','/api/filament-profiles'):
            r=_req(self._port,'GET',path,token=None);r.read();self.assertEqual(r.status,401)

if __name__=='__main__': unittest.main()


class FilamentNames(unittest.TestCase):
    def test_catalogue_names_attach_without_mutating(self):
        import tempfile, os
        from pathlib import Path
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / 'resources/profiles/BBL/filament'; folder.mkdir(parents=True)
            (folder / 'a.json').write_text(json.dumps({'name': 'Bambu PLA Matte @base', 'filament_id': 'GFA01'}))
            (folder / 'b.json').write_text(json.dumps({'name': 'Bambu PLA Matte @BBL A1', 'filament_id': 'GFA01'}))
            (folder / 'c.json').write_text(json.dumps({'name': 'Odd Brand @BBL A1', 'filament_id': 'GFX9'}))
            names = evolution.filament_names(root)
            self.assertEqual(names['GFA01'], 'Bambu PLA Matte')
            self.assertEqual(names['GFX9'], 'Odd Brand')
            ams = {'units': [{'id': 0, 'slots': [{'id': 0, 'filament_id': 'GFA01'}, {'id': 1, 'filament_id': 'NOPE'}]}], 'external': {'id': 0, 'filament_id': 'GFX9'}}
            named = evolution.name_ams(ams, names)
            self.assertEqual([s['filament_name'] for s in named['units'][0]['slots']], ['Bambu PLA Matte', None])
            self.assertEqual(named['external']['filament_name'], 'Odd Brand')
            self.assertNotIn('filament_name', ams['units'][0]['slots'][0])
            cleaned = evolution.clean_ams({'units': [{'id': 0, 'slots': [{'id': 0, 'flow_k': 0.02, 'cali_idx': 3, 'evil': 'x'}]}]})
            self.assertEqual(cleaned['units'][0]['slots'][0], {'id': 0, 'flow_k': 0.02, 'cali_idx': 3})
