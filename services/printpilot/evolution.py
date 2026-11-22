"""Persistent print lifecycle and verified editable 3MF exports."""
import os, re
import calendar, hashlib, io, json, time, uuid, zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
import tuning
import workshop

def migrate(db):
    cols={r[1] for r in db.execute('PRAGMA table_info(analyses)')}
    for key in ('model_key','feedback_context','settings_context'):
        if key not in cols: db.execute(f'ALTER TABLE analyses ADD COLUMN {key} TEXT')
    if 'plate_index' not in cols: db.execute('ALTER TABLE analyses ADD COLUMN plate_index INTEGER')
    for key in ('insights','shape_signature'):
        if key not in cols: db.execute(f'ALTER TABLE analyses ADD COLUMN {key} TEXT')
    for row in db.execute("SELECT id,file_path FROM analyses WHERE model_key IS NULL AND file_path IS NOT NULL").fetchall():
        try:
            path=Path(row['file_path'])
            if path.stat().st_size > 64*1024*1024: continue
            key=model_key(path.read_bytes())
            db.execute("UPDATE analyses SET model_key=? WHERE id=?", (key,row['id']))
        except (OSError,ValueError,ET.ParseError,zipfile.BadZipFile):
            continue
    db.executescript('''CREATE TABLE IF NOT EXISTS print_runs (
    id TEXT PRIMARY KEY,analysis_id TEXT,model_key TEXT,printer_id TEXT NOT NULL,
    job_identity TEXT,job_name TEXT,started_at TEXT NOT NULL,ended_at TEXT,status TEXT NOT NULL,
    duration_seconds INTEGER,material TEXT,settings TEXT,linkage_method TEXT DEFAULT 'unlinked',
    linkage_confidence TEXT DEFAULT 'unknown',review TEXT);
    CREATE INDEX IF NOT EXISTS print_model ON print_runs(model_key,started_at);''')
    if 'plate_index' not in {r[1] for r in db.execute('PRAGMA table_info(print_runs)')}: db.execute('ALTER TABLE print_runs ADD COLUMN plate_index INTEGER')

_MESH_TAGS={'mesh','vertices','vertex','triangles','triangle'}
def _mesh_geom(el):
    attrs=sorted((k,v) for k,v in el.attrib.items() if k in {'x','y','z','v1','v2','v3'})
    return [el.tag.rsplit('}',1)[-1],attrs,[_mesh_geom(c) for c in el if c.tag.rsplit('}',1)[-1] in _MESH_TAGS]]

def _plate_key(z,plate):
    """Geometry of the instances on one plate; ids and the plate's position on the bed do not count."""
    lower={n.lower():n for n in z.namelist()}
    files={}
    def objects(path):
        key=path.lstrip('/').lower()
        if key not in files:
            if key not in lower: raise ValueError('Missing model part '+path)
            files[key]={el.attrib.get('id'):el for el in ET.fromstring(z.read(lower[key])).iter() if el.tag.rsplit('}',1)[-1]=='object'}
        return files[key]
    def tree(path,objid,depth=0):
        if depth>8: raise ValueError('Component nesting too deep')
        obj=objects(path).get(objid)
        if obj is None: raise ValueError('Missing object '+str(objid))
        mesh=[_mesh_geom(c) for c in obj if c.tag.rsplit('}',1)[-1]=='mesh']
        comps=[]
        for c in obj.iter():
            if c.tag.rsplit('}',1)[-1]!='component': continue
            cpath=next((v for k,v in c.attrib.items() if k.rsplit('}',1)[-1]=='path'),None) or path
            comps.append([c.attrib.get('transform',''),tree(cpath,c.attrib.get('objectid'),depth+1)])
        return [mesh,comps]
    main='3D/3dmodel.model'
    root=ET.fromstring(z.read(lower[main.lower()]))
    wanted=set(plate['instances']); seen={}; picked=[]
    for item in root.iter():
        if item.tag.rsplit('}',1)[-1]!='item': continue
        oid=item.attrib.get('objectid'); n=seen.get(oid,0); seen[oid]=n+1
        if (oid,n) not in wanted: continue
        m=[float(x) for x in item.attrib.get('transform','1 0 0 0 1 0 0 0 1 0 0 0').split()]
        if len(m)!=12: raise ValueError('Invalid build transform')
        picked.append((m,tree('/'+main,oid)))
    if len(picked)!=len(wanted): raise ValueError('Plate references missing build items')
    if not picked: return None
    ox=min(m[9] for m,_ in picked); oy=min(m[10] for m,_ in picked)
    entries=sorted(json.dumps([[f'{v:.4f}' for v in m[:9]+[m[9]-ox,m[10]-oy,m[11]]],t],separators=(',',':')) for m,t in picked)
    return 'geom-plate-v1:'+hashlib.sha256('\n'.join(entries).encode()).hexdigest()

def model_key(raw,plate=None):
    """Geometry identity of the project, or of one plate (a plate dict from server.plate_layout)."""
    if plate is not None:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            if sum(i.file_size for i in z.infolist()) > 64*1024*1024: raise ValueError('Project expansion exceeds limit')
            return _plate_key(z,plate)
    models=[]
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        if sum(i.file_size for i in z.infolist()) > 64*1024*1024: raise ValueError('Project expansion exceeds limit')
        for name in sorted(z.namelist()):
            if not name.lower().endswith('.model'): continue
            def geom(el):
                attrs={k:v for k,v in el.attrib.items() if k.rsplit('}',1)[-1] in {'id','objectid','path','transform','x','y','z','v1','v2','v3','unit'}}
                return [el.tag.rsplit('}',1)[-1],sorted(attrs.items()),[geom(c) for c in el if c.tag.rsplit('}',1)[-1] in {'resources','object','mesh','vertices','vertex','triangles','triangle','components','component','build','item'}]]
            models.append(geom(ET.fromstring(z.read(name))))
    return 'geom-v1:'+hashlib.sha256(json.dumps(models,separators=(',',':')).encode()).hexdigest() if models else None

def prints(db,key=None):
    rows=db.execute('SELECT * FROM print_runs'+(' WHERE model_key=?' if key else '')+' ORDER BY started_at DESC LIMIT 200',(key,) if key else ()).fetchall()
    out=[]
    for row in rows:
        item=dict(row)
        item['timing_source']='observed_heartbeat'
        item['settings_source']='observed_telemetry' if item.get('settings') and item['settings']!='{}' else 'unknown'
        for field,fallback in [('settings',{}),('review',None),('ai_review',None),('used_settings',None),('slice_stats',None),('trays',[]),('spool_usage',None)]:
            item[field]=json.loads(item[field]) if item.get(field) else fallback
        if item.get('used_settings'): item['settings_source']='studio_send'
        if item.get('slice_stats'): item['slice_stats']={k:v for k,v in item['slice_stats'].items() if k!='layer_times_s'}
        out.append(item)
    return out

def ingest(db,printer,status,t,now):
    active=db.execute('SELECT * FROM print_runs WHERE printer_id=? AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1',(printer,)).fetchone()
    identity=json.dumps([t.get(k) for k in ('project_id','task_id','subtask_id')]) if any(t.get(k) not in (None,'','0',0) for k in ('task_id','subtask_id')) else None
    state=str(t.get('gcode_state','')).upper()
    terminal={'FINISH':'completed','FAILED':'failed','CANCELED':'cancelled','CANCELLED':'cancelled'}
    previous=json.loads(active['job_identity']) if active and active['job_identity'] else [None,None,None]
    current=json.loads(identity) if identity else [None,None,None]
    changed=active and any(a not in (None,'','0',0) and b not in (None,'','0',0) and a!=b for a,b in zip(previous[1:],current[1:]))
    if active and (changed or state in terminal or status=='idle'):
        duration=max(0,int(calendar.timegm(time.strptime(now,'%Y-%m-%dT%H:%M:%SZ'))-calendar.timegm(time.strptime(active['started_at'],'%Y-%m-%dT%H:%M:%SZ'))))
        db.execute('UPDATE print_runs SET ended_at=?,status=?,duration_seconds=? WHERE id=?',(now,'interrupted' if changed else terminal.get(state,'ended_unknown'),duration,active['id']))
        workshop.consume(db,active['id'])
        active=None
    ams=t.get('ams',{})
    material=next((slot.get('material') for unit in ams.get('units',[]) if unit.get('id')==ams.get('active_ams_id') for slot in unit.get('slots',[]) if slot.get('id')==ams.get('active_slot_id') and slot.get('present') is True),None)
    if status in ('printing','paused') and state not in terminal:
        plate=t.get('plate_index') if type(t.get('plate_index')) is int and 1<=t.get('plate_index')<=64 else None
        if not active:
            run_id=str(uuid.uuid4())
            db.execute('INSERT INTO print_runs (id,printer_id,job_identity,job_name,started_at,status,settings,material,plate_index) VALUES (?,?,?,?,?,?,?,?,?)',(run_id,printer,identity,t.get('job_name',''),now,status,json.dumps(t.get('print_settings',{})),material,plate))
            workshop.link_new_run(db,run_id,printer,plate,t.get('job_name',''),now)
            active=db.execute('SELECT * FROM print_runs WHERE id=?',(run_id,)).fetchone()
        else: db.execute('UPDATE print_runs SET status=?,job_identity=COALESCE(job_identity,?),plate_index=COALESCE(plate_index,?) WHERE id=?',(status,identity,plate,active['id']))
        workshop.note_tray(db,dict(active),t)

def optimized(raw,changes,validate,extract):
    extract(raw)
    values={c['key']:c['value'] for c in changes if c['key'] not in tuning.SPECS}
    extended=[c for c in changes if c['key'] in tuning.SPECS]
    if not values and not extended: raise ValueError('Analysis has no validated changes')
    if values and set(validate(values))!=set(values): raise ValueError('Unsupported change')
    for c in extended: tuning.normalize(c['key'],c['value'])
    touched=set(values)|{c['key'] for c in extended}
    output=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as src:
        targets=[n for n in src.namelist() if n.lower() in ('metadata/project_settings.config','metadata/project_settings.json')]
        if len(targets)!=1: raise ValueError('A single Bambu project settings JSON is required')
        target=targets[0]; config=json.loads(src.read(target))
        if not isinstance(config,dict): raise ValueError('Invalid global settings')
        for name in src.namelist():
            if name.lower().startswith('metadata/plate_') or 'slice_info' in name.lower(): continue
            if name.lower().endswith(('.config','.json')) and name!=target:
                data=src.read(name)
                if data.lstrip().startswith(b'<'):
                    if any(e.attrib.get('key') in touched for e in ET.fromstring(data).iter()): raise ValueError('Object/plate overrides require Studio editing')
                elif any(k.encode() in data for k in touched): raise ValueError('Conflicting settings require Studio editing')
        for key,value in values.items():
            if key not in config: raise ValueError('Changed setting absent from global project: '+key)
            config[key]=str(value)+('%' if key=='sparse_infill_density' else '')
        for c in extended:
            if c['key'] not in config: raise ValueError('Changed setting absent from global project: '+c['key'])
            config[c['key']]=tuning.project_value(c['key'],c['value'],config[c['key']],c.get('filament_index'))
        with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as dst:
            for item in src.infolist():
                lower=item.filename.lower()
                if lower.endswith(('.gcode','.gcode.md5','.gcode.sha256')) or lower.startswith('metadata/plate_') or 'slice_info' in lower: continue
                content=json.dumps(config).encode() if item.filename==target else src.read(item)
                if lower.endswith('.rels') or lower=='[content_types].xml':
                    root=ET.fromstring(content)
                    for entry in list(root):
                        ref=entry.attrib.get('Target',entry.attrib.get('PartName','')).lower()
                        if '.gcode' in ref or 'metadata/plate_' in ref or 'slice_info' in ref: root.remove(entry)
                    content=ET.tostring(root,encoding='utf-8',xml_declaration=True)
                dst.writestr(item,content)
    result=output.getvalue(); got=extract(result)['config']
    for key,value in values.items():
        if float(str(got.get(key,'')).rstrip('%'))!=float(value): raise ValueError('Export verification failed: '+key)
    for c in extended:
        stored=got.get(c['key'])
        if tuning.SPECS[c['key']]['scope']=='filament':
            stored=stored[c['filament_index']-1] if isinstance(stored,list) and len(stored)>=c['filament_index'] else None
        if str(stored)!=str(c['value']): raise ValueError('Export verification failed: '+c['key'])
    return result

_FILAMENT_NAMES={}
def filament_names(root):
    """filament_id (tray_info_idx) -> display name, e.g. GFA01 -> 'Bambu PLA Matte'."""
    folder=Path(os.environ.get('PRINTPILOT_PROFILE_DIR', str(Path(root)/'resources/profiles/BBL/filament')))
    key=str(folder)
    if key in _FILAMENT_NAMES: return _FILAMENT_NAMES[key]
    base,other={},{}
    for path in folder.rglob('*.json') if folder.is_dir() else []:
        try: data=json.loads(path.read_text())
        except (ValueError,OSError): continue
        fid,name=data.get('filament_id'),str(data.get('name',path.stem))
        sid=data.get('setting_id')
        if isinstance(sid,str) and sid and data.get('instantiation')=='true':
            base.setdefault('setting:'+sid,re.sub(r' @.*$','',name))  # exact preset (e.g. TINMORRY sharing Generic's filament_id)
        if not isinstance(fid,str) or not fid: continue
        target=base if name.endswith(' @base') else other
        target.setdefault(fid,re.sub(r' @.*$','',name))
    names={**other,**base}
    _FILAMENT_NAMES[key]=names
    return names

def name_ams(ams,names):
    """Attach catalogue names to normalized AMS slots without mutating stored telemetry."""
    if not isinstance(ams,dict): return ams
    def named(slot):
        if not isinstance(slot,dict): return slot
        return {**slot,'filament_name':names.get('setting:'+(slot.get('setting_id') or '')) or names.get(slot.get('filament_id') or '')}
    out=dict(ams); out['units']=[{**u,'slots':[named(s) for s in u.get('slots',[])]} for u in ams.get('units',[]) if isinstance(u,dict)]
    if ams.get('external'): out['external']=named(ams['external'])
    return out

def profiles(root,printer_model):
    folder=Path(os.environ.get('PRINTPILOT_PROFILE_DIR', str(Path(root)/'resources/profiles/BBL/filament')))
    if not folder.is_dir() or not printer_model: return []
    presets={}
    for path in folder.rglob('*.json'):
        try:
            data=json.loads(path.read_text()); presets[data.get('name',path.stem)]=data
        except (ValueError,OSError): continue
    def resolve(name,seen=()):
        if name in seen or len(seen)>10: return {}
        data=presets.get(name,{})
        return {**resolve(data.get('inherits'),(*seen,name)),**data} if data else {}
    out=[]
    for name in presets:
        p=resolve(name); model=printer_model.lower().replace('bambu lab ','')
        if not any(re.sub(r' \d+(?:\.\d+)? nozzle$', '', c.lower().replace('bambu lab ','')) == model for c in p.get('compatible_printers',[])): continue
        def first(key):
            v=p.get(key); return v[0] if isinstance(v,list) and v else v
        try:
            lo=int(first('nozzle_temperature_range_low')); hi=int(first('nozzle_temperature_range_high')); kind=first('filament_type')
            if not p.get('filament_id') or not p.get('setting_id') or not kind or not 100<=lo<=hi<=350: continue
            out.append(dict(id=name,name=name,type=kind,filament_id=p['filament_id'],setting_id=p['setting_id'],nozzle_temp_min=lo,nozzle_temp_max=hi))
        except (ValueError,TypeError): continue
    return out

def clean_ams(raw):
    def slot(s):
        if not isinstance(s,dict) or type(s.get('id')) is not int: return None
        fields={'id','label','present','material','color','remaining','rfid','rfid_known','active','filament_id','setting_id','flow_k','flow_n','cali_idx'}
        return {k:(v[:128] if isinstance(v,str) else v) for k,v in s.items() if k in fields and (v is None or isinstance(v,(str,int,float,bool)))}
    out={k:raw[k] for k in ('active_ams_id','active_slot_id','supports_edit_while_printing') if k in raw and (raw[k] is None or isinstance(raw[k],(int,bool)))}
    out['units']=[]
    for u in (raw.get('units') if isinstance(raw.get('units'),list) else [])[:16]:
        if not isinstance(u,dict) or type(u.get('id')) is not int: continue
        out['units'].append(dict(id=u['id'],name=str(u.get('name',''))[:80],humidity=u.get('humidity') if isinstance(u.get('humidity'),(int,float)) else None,humidity_raw=u.get('humidity_raw') if isinstance(u.get('humidity_raw'),(int,float)) else None,humidity_unit=str(u.get('humidity_unit',''))[:16],slots=[v for s in (u.get('slots') if isinstance(u.get('slots'),list) else [])[:16] if (v:=slot(s)) is not None]))
    out['external']=slot(raw.get('external'))
    return out

def ams_params(params,telemetry,status,available):
    import re
    if not isinstance(params,dict): raise ValueError('Invalid AMS parameters')
    ams_id,slot_id=params.get('ams_id'),params.get('slot_id')
    if type(ams_id) is not int or type(slot_id) is not int or not 0<=ams_id<=255 or not 0<=slot_id<=3: raise ValueError('AMS and slot IDs must be integers')
    ams=telemetry.get('ams',{})
    if status not in ('idle','printing','paused'): raise ValueError('Printer is not ready for filament editing')
    if status!='idle' and ams.get('supports_edit_while_printing') is not True: raise ValueError('Printer does not support edits during printing')
    slot=next((s for u in ams.get('units',[]) if u.get('id')==ams_id for s in u.get('slots',[]) if s.get('id')==slot_id),None)
    if not slot or slot.get('present') is not True or slot.get('rfid') is not False: raise ValueError('Slot must contain a non-RFID spool with known RFID status')
    profile=next((p for p in available if p['id']==params.get('profile_id')),None)
    if not profile: raise ValueError('Unsupported filament profile for this printer')
    color=params.get('color')
    if not isinstance(color,str) or not re.fullmatch(r'#[0-9a-fA-F]{6}',color): raise ValueError('Color must be #RRGGBB')
    return dict(ams_id=ams_id,slot_id=slot_id,filament_id=profile['filament_id'],setting_id=profile['setting_id'],tray_type=profile['type'],tray_color=color[1:].upper()+'FF',nozzle_temp_min=profile['nozzle_temp_min'],nozzle_temp_max=profile['nozzle_temp_max'])


def merge_telemetry(old,new):
    # Bridge has already merged MQTT deltas; normalized AMS is authoritative.
    return {**old,**new}


def feedback(db,key,settings):
    if not key: return []
    def scalar(value): return value[0] if isinstance(value,list) and value else value
    material=scalar(settings.get('filament_type'))
    printer_model=scalar(settings.get('printer_model'))
    out=[]
    for run in prints(db,key):
        if not run['review']: continue
        if material and run['material'] and str(material).casefold()!=str(run['material']).casefold(): continue
        printer=db.execute('SELECT model FROM printers WHERE printer_id=?',(run['printer_id'],)).fetchone()
        if printer_model and printer and printer['model'] and str(printer_model).casefold()!=printer['model'].casefold(): continue
        out.append(run)
    return out[:12]
