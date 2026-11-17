#!/usr/bin/env python3
"""Local-only, training-only NEXRAD scenario discovery and raw download service.

This service deliberately does NOT expose any operational warning endpoint. Only
NOAA/Unidata anonymous public Level II objects are fetched by fixed-host URL.
The original locked-down static server remains available for forecaster work.
"""
from __future__ import annotations

import argparse
import errno
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import ThreadingHTTPServer
from urllib.parse import urlsplit
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, parse_qs
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
import secrets
import shutil
import tempfile
import traceback

from simulator_safe_server import SafeHandler, ROOT

BUCKET_HOST = 'unidata-nexrad-level2.s3.amazonaws.com'
CACHE_DIR = ROOT / 'scenario_cache'
PUBLIC_CATALOG = ROOT / 'conus_radar_sites.json'
MAX_WINDOW_HOURS = 6
MAX_FILES = 220
MAX_RAW_BYTES = 3 * 1024**3
MAX_ONE_FILE = 300 * 1024**2
MAX_LISTED = 5000
STATE_LOCK = threading.RLock()
JOB = {'state': 'idle', 'message': 'No scenario downloading', 'completed': 0, 'total': 0}

# Reliable bootstrap coordinates for a subset of CONUS radars. These are not a
# complete site database; use the catalog builder to enable other sites.
SEED_SITES = [
 ('KLNX',41.9579,-100.5763,'North Platte NE'),('KGLD',39.3665,-101.7004,'Goodland KS'),
 ('KOAX',41.3203,-96.3667,'Omaha NE'),('KUEX',40.3209,-98.4419,'Hastings NE'),
 ('KTLX',35.3331,-97.2775,'Oklahoma City OK'),('KFDR',34.3620,-98.9761,'Frederick OK'),
 ('KOUN',35.2361,-97.4622,'Norman OK'),('KICT',37.6547,-97.4431,'Wichita KS'),
 ('KDDC',37.7608,-99.9688,'Dodge City KS'),('KAMA',35.2333,-101.7092,'Amarillo TX'),
 ('KFTG',39.7866,-104.5459,'Denver CO'),('KPUX',38.4594,-104.1814,'Pueblo CO'),
 ('KABR',45.4558,-98.4130,'Aberdeen SD'),('KUNR',44.1250,-102.8298,'Rapid City SD'),
 ('KDMX',41.7312,-93.7229,'Des Moines IA'),('KLOT',41.6044,-88.0848,'Chicago IL'),
 ('KIND',39.7075,-86.2803,'Indianapolis IN'),('KILN',39.4203,-83.8217,'Wilmington OH'),
 ('KHGX',29.4719,-95.0792,'Houston TX'),('KFWS',32.5730,-97.3031,'Dallas/Fort Worth TX'),
 ('KLSX',38.6989,-90.6829,'St Louis MO'),('KMHX',34.7759,-76.8763,'Morehead City NC'),
 ('KFFC',33.3636,-84.5659,'Atlanta GA'),('KJAX',30.4846,-81.7019,'Jacksonville FL'),
 ('KTBW',27.7055,-82.4018,'Tampa FL'),('KBMX',33.1723,-86.7696,'Birmingham AL'),
 ('KLIX',30.3367,-89.8254,'New Orleans LA'),('KMPX',44.8489,-93.5655,'Minneapolis MN'),
 ('KSEW',47.4608,-122.3016,'Seattle WA'),('KRTX',45.7150,-122.9650,'Portland OR'),
 ('KDAX',38.5011,-121.6778,'Sacramento CA'),('KNKX',32.9189,-117.0419,'San Diego CA'),
 ('KSOX',33.8178,-117.6359,'Santa Ana CA'),('KATX',48.1946,-122.4957,'Seattle North WA'),
 ('KRIW',43.0661,-108.4773,'Riverton WY'),('KCYS',41.1519,-104.8061,'Cheyenne WY'),
]
SEED_WFOS=['LBF','GLD','OAX','GID','OUN','ICT','DDC','AMA','BOU','PUB','UNR','ABR','DMX','LOT','IND','ILN','HGX','FWD','LSX','MHX','FFC','JAX','TBW','BMX','LIX','MPX','SEW','PQR','STO','SGX','REV','RIW','CYS']


def catalog():
    radars = {s[0]: {'id': s[0], 'lat':s[1], 'lon':s[2], 'name':s[3]} for s in SEED_SITES}
    wfos = set(SEED_WFOS)
    if PUBLIC_CATALOG.is_file():
        try:
            data=json.loads(PUBLIC_CATALOG.read_text(encoding='utf-8'))
            for r in data.get('radars', []):
                code=str(r['id']).upper(); lat=float(r['lat']); lon=float(r['lon'])
                if re.fullmatch('K[A-Z]{3}',code) and 24<=lat<=50.5 and -126<=lon<=-66:
                    radars[code]={'id':code,'lat':lat,'lon':lon,'name':str(r.get('name',code))[:120]}
            wfos.update(x for x in data.get('wfos',[]) if isinstance(x,str) and re.fullmatch('[A-Z]{3}',x))
        except (OSError,ValueError,KeyError,TypeError):
            pass
    return {'radars':sorted(radars.values(),key=lambda s:s['id']), 'wfos':sorted(wfos),
            'catalogStatus':'extended' if PUBLIC_CATALOG.is_file() else 'bootstrap',
            'note':'Bootstrap radar list is incomplete; load the CONUS radar/WFO catalog to enable all sites.'}


def parse_spec(spec):
    if not isinstance(spec,dict): raise ValueError('Scenario must be a JSON object.')
    start_text=str(spec.get('start',''))
    end_text=str(spec.get('end',''))
    def parse(t):
        # UTC must be explicit, to prevent ambiguous time zone interpretation.
        if not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d(?:Z|\+00:00)',t):
            raise ValueError('Start/end must be UTC ISO timestamps with Z suffix.')
        return datetime.fromisoformat(t.replace('Z','+00:00'))
    start=parse(start_text); end=parse(end_text)
    if end<=start or end-start>timedelta(hours=MAX_WINDOW_HOURS):
        raise ValueError('Choose a positive time window no longer than six hours.')
    if start>datetime.now(timezone.utc) or start.year<1991: raise ValueError('Archive date is outside the supported range.')
    sites={s['id']:s for s in catalog()['radars']}
    source=str(spec.get('source','')).strip().upper(); target=str(spec.get('destination','')).strip().upper()
    wfo=str(spec.get('wfo','')).strip().upper()
    if source not in sites or target not in sites: raise ValueError('Source and destination must be CONUS radars in the loaded site catalog.')
    if wfo not in catalog()['wfos']: raise ValueError('Training WFO must be in the loaded CONUS catalog.')
    return {'start':start,'end':end,'source':source,'destination':target,'wfo':wfo,
            'relocated':source!=target,'destination_site':sites[target]}


def list_archive(spec):
    """List Level II volumes, with diagnostic distinctions for empty/filter-missed searches.

    A successful empty S3 listing is not proof a radar had no data: it can also
    be the result of a wrong source/date or an archive coverage gap. Report it
    separately from filename filtering and UTC-time-window mismatches.
    """
    records = []
    listed = 0
    recognized = 0
    in_window = 0
    too_large = 0
    sample_names = []
    first_volume = None
    last_volume = None
    date = spec['start'].date()
    source = spec['source']
    # Some archived names include a lower-case .gz extension. Keep the source
    # radar pinned to the selected site, rather than matching any four letters.
    filename_pattern = re.compile(r'^' + re.escape(source) +
                                  r'(\d{8})_(\d{6})(?:_V\d{2})?(?:\.gz)?$')
    while date <= spec['end'].date():
        prefix = f'{date:%Y/%m/%d}/{source}/'
        token = None
        while True:
            params = {'list-type': '2', 'prefix': prefix, 'max-keys': '1000'}
            if token: params['continuation-token'] = token
            url = f'https://{BUCKET_HOST}/?' + urlencode(params)
            with urlopen(Request(url, headers={'User-Agent': 'LocalWarningTrainingScenarioBuilder/3.23.1'}), timeout=30) as res:
                body = res.read(2 * 1024 * 1024)
            doc = ET.fromstring(body)
            def txt(e, k): return e.findtext('{*}' + k) if e is not None else None
            for elem in doc.findall('{*}Contents'):
                key = txt(elem, 'Key') or ''
                if not key.startswith(prefix):
                    continue
                listed += 1
                if listed > MAX_LISTED:
                    raise ValueError('Archive listing exceeds the safe object count.')
                name = key.rsplit('/', 1)[-1]
                if len(sample_names) < 3:
                    sample_names.append(name[:80])
                # Accept expected archive .gz suffixes; basename and source
                # identity are validated again before any file is downloaded.
                if not re.fullmatch(r'[A-Za-z0-9_./-]+', key):
                    continue
                m = filename_pattern.fullmatch(name)
                if not m:
                    continue  # Omit MDM status or archive noise
                try:
                    ts = datetime.strptime(m[1] + m[2], '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
                    size = int(txt(elem, 'Size') or '0')
                except (ValueError, TypeError):
                    continue
                recognized += 1
                if first_volume is None or ts < first_volume:
                    first_volume = ts
                if last_volume is None or ts > last_volume:
                    last_volume = ts
                if not (spec['start'] <= ts <= spec['end']):
                    continue
                in_window += 1
                if not (0 < size <= MAX_ONE_FILE):
                    too_large += 1
                    continue
                records.append({'key': key, 'bytes': size, 'timestamp': ts.isoformat()})
            token = txt(doc, 'NextContinuationToken')
            if txt(doc, 'IsTruncated') != 'true':
                break
            if not token:
                raise ValueError('Archive listing pagination failed.')
        date += timedelta(days=1)
    records.sort(key=lambda r: r['timestamp'])
    if not records:
        context = (f'{source} {spec["start"]:%Y-%m-%d %H:%M} to '
                   f'{spec["end"]:%Y-%m-%d %H:%M} UTC')
        if listed == 0:
            raise ValueError(f'No S3 archive objects returned for {context}. Verify the source RDA and UTC date; try another known archived day.')
        if recognized == 0:
            examples = ', '.join(sample_names)
            raise ValueError(f'Archive returned {listed} objects for {context}, but none matched a Level II filename. Examples: {examples}')
        if in_window == 0:
            available = f'{first_volume:%Y-%m-%d %H:%M} to {last_volume:%Y-%m-%d %H:%M} UTC'
            raise ValueError(f'Archive has {recognized} Level II volumes for the selected date(s), but none within {context}. Available time span: {available}.')
        if too_large:
            raise ValueError(f'Archive has {in_window} volumes for {context}, but all failed the 300 MB per-file size limit or had zero size.')
        raise ValueError(f'No usable Level II volumes found for {context}.')
    if len(records) > MAX_FILES:
        raise ValueError(f'Too many scans ({len(records)}). Narrow your time range to at most {MAX_FILES}.')
    total = sum(r['bytes'] for r in records)
    if total > MAX_RAW_BYTES:
        raise ValueError('Estimated archive size exceeds the 3 GB per-scenario download limit.')
    return records


def identifier(spec):
    fields={k:(v.isoformat() if isinstance(v,datetime) else v) for k,v in spec.items() if k!='destination_site'}
    return hashlib.sha256(json.dumps(fields,sort_keys=True).encode()).hexdigest()[:16]


def serialize_spec(spec):
    return {k:(v.isoformat() if isinstance(v,datetime) else v) for k,v in spec.items()}



# v3.26 — Instructor-only local Scenario Library. No operational interfaces.
LIBRARY_FILE = CACHE_DIR / 'scenario_library.json'
LIBRARY_LOCK = threading.RLock()
CASE_ID_RE = re.compile(r'^[0-9a-f]{16}$')
MAX_LIBRARY_CASES = 1000


def _library_read():
    if not LIBRARY_FILE.is_file():
        return {'schema': 'training-scenario-library-v1', 'cases': {}}
    raw = LIBRARY_FILE.read_bytes()
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError('Scenario library metadata is unexpectedly large')
    data = json.loads(raw)
    if data.get('schema') != 'training-scenario-library-v1' or not isinstance(data.get('cases'), dict):
        raise ValueError('Scenario library metadata is invalid')
    return data


def _library_write(data):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_DIR / ('scenario_library.' + secrets.token_hex(5) + '.tmp')
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(LIBRARY_FILE)
    finally:
        tmp.unlink(missing_ok=True)


def _prepared_dir(case_id):
    if not isinstance(case_id, str) or not CASE_ID_RE.fullmatch(case_id):
        raise ValueError('Invalid prepared scenario ID')
    root = (ROOT / 'web_data' / 'scenarios').resolve()
    dest = (root / case_id).resolve()
    if not dest.is_relative_to(root):
        raise ValueError('Invalid scenario location')
    return dest


def _prepared_metadata(case_id):
    dest = _prepared_dir(case_id)
    m = dest / 'manifest.json'
    if not m.is_file() or not (dest / 'basemap_static.json').is_file() or not (dest / 'warning_geography.json').is_file():
        raise ValueError('Prepared scenario files are incomplete')
    data = json.loads(m.read_text(encoding='utf-8'))
    if data.get('scenario_id') != case_id or not data.get('volumes') or not isinstance(data.get('geography'), dict):
        raise ValueError('Prepared scenario identity is invalid')
    raw_id = data.get('raw_scenario_id')
    if not isinstance(raw_id, str) or not CASE_ID_RE.fullmatch(raw_id):
        raise ValueError('Prepared scenario is missing a valid raw archive identity')
    return data


def _raw_info(raw_id):
    if not CASE_ID_RE.fullmatch(raw_id):
        return None
    path = CACHE_DIR / raw_id / 'raw_manifest.json'
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('scenarioId') != raw_id or data.get('status') != 'RAW_DOWNLOADED_NOT_PLAYABLE':
            return None
        return data
    except (OSError, ValueError, TypeError):
        return None


def scenario_library_list():
    with LIBRARY_LOCK:
        labels = _library_read()['cases']
        result = []
        prepared_raw_ids = set()
        folder = ROOT / 'web_data' / 'scenarios'
        if folder.is_dir():
            for child in sorted(folder.iterdir()):
                if not child.is_dir() or not CASE_ID_RE.fullmatch(child.name):
                    continue
                try:
                    man = _prepared_metadata(child.name)
                    raw_id = man['raw_scenario_id']
                    spec = (_raw_info(raw_id) or {}).get('spec', {})
                    if not isinstance(spec, dict): spec = {}
                    start = spec.get('start') or man['volumes'][0].get('volume_start_utc')
                    end = spec.get('end') or man['volumes'][-1].get('volume_end_utc')
                    # Never expose cached filesystem paths or original report details in list metadata.
                    report_meta = man.get('storm_reports', {})
                    name = labels.get(child.name, {}).get('name') or f"{spec.get('source', man.get('provenance',{}).get('source_radar','Radar'))} → {man.get('radar','Radar')} · {str(start)[:10]}"
                    result.append({'id':child.name,'kind':'prepared','state':'PLAYABLE',
                       'name':name,'source':spec.get('source',man.get('provenance',{}).get('source_radar','')),
                       'destination':spec.get('destination',man['volumes'][0].get('site',{}).get('id','')),
                       'wfo':spec.get('wfo',man.get('training_wfo','')),'start':start,'end':end,
                       'volumeCount':len(man['volumes']), 'lsrCount':report_meta.get('count',0) if isinstance(report_meta,dict) else 0,
                       'hasSavedReportPlan':bool(labels.get(child.name,{}).get('plan')),
                       'launchPath':'/geo_viewer.html?case='+child.name})
                    prepared_raw_ids.add(raw_id)
                except (ValueError,KeyError,TypeError,OSError):
                    continue
        if CACHE_DIR.is_dir():
            for child in CACHE_DIR.iterdir():
                if not child.is_dir() or not CASE_ID_RE.fullmatch(child.name) or child.name in prepared_raw_ids:
                    continue
                meta = _raw_info(child.name)
                if not meta:continue
                spec=meta.get('spec',{})
                if not isinstance(spec,dict):continue
                result.append({'id':child.name,'kind':'raw','state':'DOWNLOADED',
                    'name':labels.get(child.name,{}).get('name') or f"{spec.get('source','Radar')} → {spec.get('destination','Radar')} · {str(spec.get('start',''))[:10]}",
                    'source':spec.get('source',''),'destination':spec.get('destination',''),
                    'wfo':spec.get('wfo',''),'start':spec.get('start'), 'end':spec.get('end'),
                    'volumeCount':len(meta.get('volumes',[])), 'lsrCount':0,
                    'hasSavedReportPlan':False, 'launchPath':None,
                    'spec':{k:spec.get(k) for k in ('source','destination','wfo','start','end')}})
        result.sort(key=lambda c:(str(c.get('start') or ''),c['state']=='PLAYABLE'), reverse=True)
        return {'schema':'training-scenario-library-v1','items':result[:MAX_LIBRARY_CASES], 'total':len(result)}


def scenario_library_rename(case_id, name):
    _prepared_dir(case_id)  # validate identity
    name = str(name).strip()
    if not 1 <= len(name) <= 72 or any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise ValueError('Scenario name must be 1–72 printable characters')
    if not (_prepared_dir(case_id) / 'manifest.json').is_file() and not (CACHE_DIR / case_id / 'raw_manifest.json').is_file():
        raise ValueError('Scenario not found')
    with LIBRARY_LOCK:
        data = _library_read(); entries = data['cases']
        if len(entries) >= MAX_LIBRARY_CASES and case_id not in entries: raise ValueError('Library metadata limit reached')
        entries.setdefault(case_id,{})['name']=name
        _library_write(data)
    return {'id':case_id,'name':name}


def scenario_library_save_plan(case_id, plan):
    _prepared_metadata(case_id)
    if not isinstance(plan, dict) or plan.get('schema')!='training-lsr-plan-v1' or plan.get('caseId') != case_id:
        raise ValueError('Invalid report configuration')
    report_file=_prepared_dir(case_id)/'storm_reports.json'
    if not report_file.is_file():raise ValueError('Scenario has no report archive')
    report_data=json.loads(report_file.read_text(encoding='utf-8'))
    ids={r.get('id') for r in report_data.get('allReports',[]) if isinstance(r,dict)}
    selected=plan.get('selected');overrides=plan.get('overrides')
    if not isinstance(selected,list) or len(selected)>3000 or not all(isinstance(i,str) and i in ids for i in selected) or len(set(selected))!=len(selected):
        raise ValueError('Invalid selected report IDs')
    if not isinstance(overrides,dict) or len(overrides)>3000 or not all(i in ids and isinstance(n,int) and n in (0,3,5,10,15,30) for i,n in overrides.items()):
        raise ValueError('Invalid per-report delays')
    if type(plan.get('defaultDelay')) is not int or plan['defaultDelay'] not in (0,3,5,10,15,30):
        raise ValueError('Invalid default delivery offset')
    if type(plan.get('lifetimeMinutes')) is not int or plan['lifetimeMinutes'] not in (30,60,90,120):
        raise ValueError('Invalid marker lifetime')
    clean={k:plan[k] for k in ('schema','caseId','selected','overrides','defaultDelay','lifetimeMinutes')}
    clean['confirmed']=True
    with LIBRARY_LOCK:
        data=_library_read();data['cases'].setdefault(case_id,{})['plan']=clean
        _library_write(data)
    return {'saved':True,'caseId':case_id,'selectedCount':len(selected)}


def scenario_library_plan(case_id):
    _prepared_metadata(case_id)
    with LIBRARY_LOCK:
        plan=_library_read()['cases'].get(case_id,{}).get('plan')
    return {'caseId':case_id,'plan':plan}


def scenario_library_delete_prepared(case_id, active_id):
    if case_id == active_id:
        raise ValueError('Cannot delete the currently loaded scenario. Switch away from it first.')
    dest=_prepared_dir(case_id)
    _prepared_metadata(case_id)
    # Only the exact prepared case is deleted; never erase source scans or other versions.
    try:
        shutil.rmtree(dest)
    except PermissionError:
        raise ValueError('Windows has this scenario open. Close any browser tabs using it and retry.')
    with LIBRARY_LOCK:
        data=_library_read();data['cases'].pop(case_id,None);_library_write(data)
    return {'deleted':case_id,'rawCachePreserved':True}


def download_records(spec, records):
    global JOB
    ident=identifier(spec)
    base=(CACHE_DIR/ident).resolve()
    if not base.is_relative_to(CACHE_DIR.resolve()): raise ValueError('Bad cache location.')
    raw=base/'raw'; raw.mkdir(parents=True,exist_ok=True)
    done=[]
    try:
        for idx, record in enumerate(records):
            key=record['key']; expected=record['bytes']
            name=Path(key).name
            if not re.fullmatch(r'K[A-Z]{3}\d{8}_\d{6}(?:_V\d\d)?(?:\.gz)?',name):
                raise ValueError('Archive object filename failed validation.')
            output=raw/name
            if output.is_file() and output.stat().st_size==expected:
                pass
            else:
                # Only the hard-coded approved archive hostname is contacted.
                url=f'https://{BUCKET_HOST}/'+quote(key,safe='/')
                tmp=raw/(name+'.part')
                try:
                    with urlopen(Request(url,headers={'User-Agent':'LocalWarningTrainingScenarioBuilder/3.23'}),timeout=120) as source,open(tmp,'wb') as dest:
                        transferred=0
                        while True:
                            chunk=source.read(1024*1024)
                            if not chunk: break
                            transferred+=len(chunk)
                            if transferred>expected or transferred>MAX_ONE_FILE:raise ValueError('Radar file exceeded announced size.')
                            dest.write(chunk)
                    if transferred!=expected: raise ValueError('Downloaded radar size mismatch.')
                    tmp.replace(output)
                finally: tmp.unlink(missing_ok=True)
            done.append({'file':name, 'bytes':expected, 'timestamp':record['timestamp']})
            with STATE_LOCK: JOB.update(completed=idx+1,total=len(records),
                phase='download',percent=round(100*(idx+1)/len(records)),
                message=f'Downloaded {idx+1} / {len(records)} radar volumes')
        meta={'schema':'warning-simulator-raw-archive-v1', 'status':'RAW_DOWNLOADED_NOT_PLAYABLE',
              'scenarioId':ident,'spec':serialize_spec(spec),'volumes':done,
              'provenance':{'bucket':'unidata-nexrad-level2','source_radar':spec['source'],
                            'simulated_destination':spec['destination'],'radar_centered_only':True}}
        tmp=base/'raw_manifest.json.part'; tmp.write_text(json.dumps(meta,indent=2),encoding='utf-8');tmp.replace(base/'raw_manifest.json')
        with STATE_LOCK: JOB.update(state='downloaded',phase='downloaded',percent=100,
            message=f'{len(done)} Level II scans cached. Conversion/geography still required before this case can run.',scenarioId=ident)
    except Exception as exc:
        with STATE_LOCK: JOB.update(state='error', message=str(exc)[:300])


# v3.35.0.3: Check the EXACT final publish folder before any expensive
# conversion begins.  Use real nested write and rename operations rather than
# os.access(), which may miss ACL restrictions. Never touch existing cases.
def check_prepared_output_writable():
    destination = ROOT / 'web_data' / 'scenarios'
    try:
        if destination.is_symlink():
            raise ValueError('Prepared-case folder must not be a symbolic link.')
        destination.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.wds-write-check-', dir=destination) as trial:
            trial = Path(trial)
            incoming = trial / 'incoming'
            incoming.mkdir()
            (incoming / 'probe').write_bytes(b'training-only')
            incoming.rename(trial / 'published')
            if (trial / 'published' / 'probe').read_bytes() != b'training-only':
                raise OSError('Prepared-case publish probe failed validation')
    except OSError as exc:
        raise ValueError(
            'Prepared-case folder is not writable: web_data/scenarios. '
            'In the frontend Codespace, verify owner permissions with '
            '`ls -ld web_data/scenarios` and correct them before preparing. '
            'Your existing cases and downloaded radar scans were not changed. '
            f'Details: {exc}'
        ) from exc

# v3.24.1: build in a private per-user staging directory, not inside the
# extracted simulator folder.  Windows may prohibit nested writes in Downloads
# (or other managed folders) even if creation of the cache directory worked.
# The cache containing archived scans is never changed by conversion.
def _private_stage_dir(ident):
    # LOCALAPPDATA is user-scoped on Windows.  The operating system temp folder
    # is the portable fallback when an account redirects or restricts AppData.
    locations = []
    if os.environ.get('LOCALAPPDATA'):
        locations.append(Path(os.environ['LOCALAPPDATA']) / 'WarningDecisionSimulator' / 'staging')
    locations.append(Path(tempfile.gettempdir()) / 'WarningDecisionSimulator' / 'staging')
    reasons = []
    for parent in dict.fromkeys(locations):
        try:
            parent.mkdir(parents=True, exist_ok=True)
            work = Path(tempfile.mkdtemp(prefix=f'{ident}-', dir=parent))
            # Fail early (before conversion begins) if folder creation or file
            # writes are blocked by permissions or security software.
            test_dir = work / 'moments'
            test_dir.mkdir()
            (test_dir / '.write-test').write_bytes(b'local-training-only')
            (test_dir / '.write-test').unlink()
            test_dir.rmdir()
            return work
        except OSError as exc:
            reasons.append(f'{parent}: {exc}')
            if 'work' in locals() and work.is_dir():
                try: shutil.rmtree(work)
                except OSError: pass
            work = None
    raise PermissionError('Cannot create writable temporary conversion workspace. '
        'Check temporary-folder permissions and available disk space. Details: ' + ' | '.join(reasons))


def _next_prepared_id():
    """Give each fully prepared case its own immutable 16-hex public identity.

    Windows can refuse to delete a radar-case directory while a browser,
    antivirus, or filesystem indexer has a file open. Never overwrite an
    already-published scenario: existing training sessions may be using it.
    """
    root = ROOT / 'web_data' / 'scenarios'
    root.mkdir(parents=True, exist_ok=True)
    for _ in range(12):
        case_id = secrets.token_hex(8)
        if not (root / case_id).exists():
            return case_id
    raise RuntimeError('Could not allocate a unique prepared scenario ID')


def _publish_prepared_case(staging, dest):
    """Publish a complete validated case once, without touching active cases."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise FileExistsError('Prepared scenario target already exists; refusing to overwrite it')
    try:
        # Fast, same-volume atomic publish when the source and destination are
        # on the same filesystem. No currently served data is removed.
        staging.replace(dest)
    except OSError as exc:
        if exc.errno != errno.EXDEV and getattr(exc, 'winerror', None) != 17:
            raise
        # Cross-volume: copy to a nonpublic dot-prefixed staging directory.
        # The locked-down local server will not serve dot-prefixed paths.
        incoming = dest.parent / f'.{dest.name}.incoming-{secrets.token_hex(6)}'
        try:
            shutil.copytree(staging, incoming)
            if dest.exists():
                raise FileExistsError('Prepared scenario target appeared during publishing')
            incoming.replace(dest)
        finally:
            if incoming.exists():
                shutil.rmtree(incoming, ignore_errors=True)


def _prepare_cached(spec):
    global JOB
    ident=identifier(spec)
    base=(CACHE_DIR/ident).resolve()
    if not base.is_relative_to(CACHE_DIR.resolve()):raise ValueError('Invalid scenario path')
    raw_manifest_file=base/'raw_manifest.json'
    if not raw_manifest_file.is_file():raise ValueError('No cached raw scans for these exact selections. Download the case first, or restore its original UTC window.')
    data=json.loads(raw_manifest_file.read_text(encoding='utf-8'))
    if data.get('status')!='RAW_DOWNLOADED_NOT_PLAYABLE' or data.get('scenarioId')!=ident:
        raise ValueError('Selected cache is incomplete or incompatible')
    if any(str(data.get('spec',{}).get(k)) != str(serialize_spec(spec).get(k)) for k in ['start','end','source','destination','wfo']):
        raise ValueError('Cached scenario selections do not match')
    if not isinstance(data.get('volumes'),list) or not data['volumes']:
        raise ValueError('Cached volume manifest is empty')
    def phase_update(phase,percent,message):
        with STATE_LOCK:
            JOB.update(phase=phase,percent=int(percent),message=str(message))
    def progress(completed,total,message):
        # Converter progress is scan-based. Reserve remaining progress for
        # geography, LSR enrichment and atomic publishing (not yet playable).
        pct=8 + round(70 * min(max(completed,0),max(total,1))/max(total,1))
        with STATE_LOCK:
            JOB.update(completed=completed,total=total,phase='radar',percent=pct,message=message)
    staging=None
    # Do not modify an existing case directory: Chrome/Edge may have files open.
    # A new public identity is assigned after conversion and validation.
    dest=None
    try:
        phase_update('preflight',2,'Checking writable prepared-case directory')
        check_prepared_output_writable()
        phase_update('workspace',5,'Creating temporary radar conversion workspace')
        staging=_private_stage_dir(ident)
        progress(0,len(data['volumes']),'Decoding archived Level II radar sweeps in private workspace')
        from scenario_converter import convert_cached
        data['_base']=str(base)
        radar=convert_cached(data,staging,progress)
        velocity_volumes=sum(any((sw['moments'].get('VRADH') or {}).get('finite_gate_count',0)>0
                                 for sw in v['sweeps']) for v in radar['volumes'])
        if velocity_volumes==0:
            raise ValueError('No usable base velocity (VRADH) was decoded from the archived Level II volumes. '
                             'Reflectivity-only training preparation was stopped; please report the source radar and UTC window.')
        print(f'RADAR MOMENT AUDIT: usable velocity in {velocity_volumes}/{len(radar["volumes"])} volumes; '
              f'SRV is calculated from that velocity and selected storm motion.',flush=True)
        phase_update('geography',80,'Preparing NWS county/CWA and Census city geography')
        from scenario_geography import build_geography
        geo=build_geography(spec['wfo'],spec['destination_site'],ROOT/'reference_cache',staging,
                            lambda msg:phase_update('geography',84,msg))
        assert radar['training_wfo']==geo['wfo']
        radar['geography']=geo
        # v3.25: historical LSR enrichment is optional. It never makes an
        # otherwise valid radar case unusable and never sends warning products.
        phase_update('reports',88,'Retrieving historical storm reports and preserving source metadata')
        from scenario_lsrs import build_lsrs
        sites={r['id']:r for r in catalog()['radars']}
        reports=build_lsrs(spec, sites[spec['source']], base, staging,
                           lambda msg:phase_update('reports',92,msg))
        radar['storm_reports']=reports
        phase_update('publish',96,'Validating playable case and preparing atomic publish')
        prepared_id=_next_prepared_id()
        dest=ROOT/'web_data'/'scenarios'/prepared_id
        radar['scenario_id']=prepared_id
        radar['raw_scenario_id']=ident
        (staging/'manifest.json').write_text(json.dumps(radar,separators=(',',':')),encoding='utf-8')
        if not (staging/'warning_geography.json').is_file() or not (staging/'basemap_static.json').is_file():
            raise ValueError('Geographic package was not generated')
        phase_update('publish',98,'Publishing prepared case without changing existing cases')
        _publish_prepared_case(staging,dest)
        record={'scenarioId':prepared_id,'rawScenarioId':ident,'state':'PREPARED','source':spec['source'],
                'destination':spec['destination'],'wfo':spec['wfo'],
                'volumes':len(radar['volumes']),'geography':geo,'storm_reports':reports,
                'launchPath':'/geo_viewer.html?case='+prepared_id,
                'note':'Local TRAINING ONLY; original polar radar moments are not real observations at relocated RDA.'}
        (base/'prepared_manifest.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
        with STATE_LOCK:JOB.update(state='prepared',phase='complete',percent=100,message=f'Playable radar + {geo["cwa_counties"]} WFO counties + {geo["mapped_places"]} places + {geo["primary_road_segments"]} primary and {geo["secondary_road_segments"]} secondary roads; LSRs: {reports["count"]} available / {reports["suggested"]} suggested ({reports["status"]}).',scenarioId=prepared_id,launchPath=record['launchPath'],geography=geo)
    except Exception as exc:
        # Full traceback stays in instructor-side localhost console so problems
        # can be diagnosed without exposing implementation details to trainees.
        traceback.print_exc()
        reason = str(exc)
        if isinstance(exc, PermissionError):
            reason = ('Cannot write prepared-case files. Check directory permissions and free '
                'disk space in the frontend Codespace; downloaded scans remain cached. Details: ' + reason)
        with STATE_LOCK:JOB.update(state='error',phase='error',message='Preparation failed: '+reason[:350])
    finally:
        if staging is not None and staging.exists():
            try:shutil.rmtree(staging)
            except OSError as exc:print('WARNING: Could not remove private temporary conversion folder:',exc,flush=True)


def prepare_cached(spec):
    """Never leave an asynchronous job stuck in preparing after validation errors."""
    global JOB
    try:return _prepare_cached(spec)
    except Exception as exc:
        with STATE_LOCK:JOB.update(state='error',message='Preparation failed: '+str(exc)[:290])

class ScenarioHandler(SafeHandler):
    def _expected_origin(self):
        """Validate only loopback hosts or this Codespace's private forwarded host.

        The GitHub forwarding proxy can pass the browser's HTTPS Origin while
        rewriting the upstream Host header to a local loopback host.  Both
        represent the SAME origin after forwarding, not a cross-origin API.
        An arbitrary remote Host, another Codespace name, or other Origin is
        never accepted. The listener remains bound to 127.0.0.1 only.
        """
        port = self.server.server_port
        host = self.headers.get('Host', '').strip().lower()
        local_hosts = {f'127.0.0.1:{port}', f'localhost:{port}', '127.0.0.1', 'localhost'}
        codespaces = os.environ.get('CODESPACES', '').lower() == 'true'
        name = os.environ.get('CODESPACE_NAME', '')
        forwarded = None
        if codespaces and re.fullmatch(r'[a-z0-9-]{4,110}', name):
            forwarded = f'https://{name}-{port}.app.github.dev'
            forwarded_host = f'{name}-{port}.app.github.dev'
            if host in (forwarded_host, forwarded_host + ':443'):
                return forwarded
        if host in local_hosts:
            # Forwarded proxies may rewrite Host to loopback while preserving
            # the public HTTPS Origin. Only the EXACT current Codespace URL is
            # accepted (not arbitrary github.dev subdomains).
            if forwarded and self.headers.get('Origin', '') == forwarded:
                return forwarded
            if forwarded and self.headers.get('Referer', '').startswith(forwarded + '/'):
                return forwarded
            if host.startswith('localhost'):
                return f'http://localhost:{port}'
            return f'http://127.0.0.1:{port}'
        return None

    def _api_host_ok(self):
        expected = self._expected_origin()
        site = self.headers.get('Sec-Fetch-Site', '').lower()
        origin = self.headers.get('Origin', '')
        allowed = bool(expected and site not in ('cross-site', 'same-site') and
                       (not origin or origin == expected))
        if not allowed:
            # Show only nonsecret routing headers in the PRIVATE Codespace
            # terminal. Never log cookies, authorization, or request bodies.
            print('WDS API host guard: rejected request '
                  f'Host={self.headers.get("Host", "")[:160]!r} '
                  f'Origin={origin[:160]!r} Sec-Fetch-Site={site[:40]!r} '
                  f'expected={expected!r} codespaces={os.environ.get("CODESPACES")!r}',
                  flush=True)
        return allowed

    def _json(self,status,body):
        data=json.dumps(body,default=str).encode()
        self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(data))); self.end_headers();self.wfile.write(data)

    def do_GET(self):
        if urlsplit(self.path).path=='/api/scenario/catalog':
            if not self._api_host_ok(): return self._json(403,{'error':'Forbidden host'})
            return self._json(200,catalog())
        if urlsplit(self.path).path=='/api/scenario/library':
            if not self._api_host_ok():return self._json(403,{'error':'Forbidden host'})
            return self._json(200,scenario_library_list())
        if urlsplit(self.path).path=='/api/scenario/library/plan':
            if not self._api_host_ok():return self._json(403,{'error':'Forbidden host'})
            case_id=parse_qs(urlsplit(self.path).query).get('case',[''])[0]
            try:return self._json(200,scenario_library_plan(case_id))
            except (ValueError,KeyError,OSError) as exc:return self._json(400,{'error':str(exc)[:200]})
        if urlsplit(self.path).path=='/api/scenario/status':
            if not self._api_host_ok():return self._json(403,{'error':'Forbidden host'})
            with STATE_LOCK: return self._json(200,dict(JOB))
        return super().do_GET()

    def _post_training_request_ok(self):
        """Guard writes through the private Codespaces proxy without trusting it blindly.

        Browsers ordinarily attach an Origin to JSON POSTs. Some private
        forwarding layers can omit Origin or normalize Sec-Fetch-Site. A missing
        Origin is accepted ONLY when both the browser's same-origin fetch marker
        and same-origin Referer are present. Explicit foreign/null Origins are
        always rejected. Host must still be exactly this Codespace or loopback.
        """
        expected=self._expected_origin()
        origin=self.headers.get('Origin','')
        referer=self.headers.get('Referer','')
        site=self.headers.get('Sec-Fetch-Site','').strip().lower()
        ctype=self.headers.get('Content-Type','').split(';',1)[0].strip().lower()
        training=self.headers.get('X-Training-Request','')=='scenario-builder-v1'
        same_site_ref=bool(expected and referer.startswith(expected+'/') and
                           urlsplit(referer).netloc==urlsplit(expected).netloc and
                           urlsplit(referer).scheme==urlsplit(expected).scheme)
        # GitHub Codespaces sometimes forwards a browser POST with the
        # ORIGINAL private forwarded Referer, but rewrites Host to localhost
        # and Origin to *https*://localhost:<port>. This is not the browser's
        # public origin; accept ONLY that observed, tightly constrained form.
        # An arbitrary localhost Origin, unrelated Referer, foreign Host,
        # non-Codespaces process, or cross-site fetch is never exempted.
        port=self.server.server_port
        proxied_local_https_origin=bool(
            os.environ.get('CODESPACES','').lower()=='true'
            and expected and expected.startswith('https://')
            and expected.endswith(f'-{port}.app.github.dev')
            and self.headers.get('Host','').strip().lower()==f'localhost:{port}'
            and origin==f'https://localhost:{port}'
            and site=='same-origin' and same_site_ref
        )
        origin_ok=bool(expected and (
            origin==expected or
            (not origin and site=='same-origin' and same_site_ref) or
            proxied_local_https_origin
        ))
        allowed=bool(origin_ok and site!='cross-site' and ctype=='application/json' and training)
        if not allowed:
            # No authentication tokens, cookies, body, or full query-bearing URLs.
            print('WDS POST guard: rejected request '
                  f'Host={self.headers.get("Host", "")[:140]!r} '
                  f'Origin={origin[:140]!r} '
                  f'Sec-Fetch-Site={site[:40]!r} '
                  f'expected={expected!r} '
                  f'Referer-origin-match={same_site_ref} '
                  f'Codespaces-localhost-match={proxied_local_https_origin} '
                  f'content-type-ok={ctype=="application/json"} '
                  f'training-header-ok={training}',flush=True)
        return allowed

    def do_POST(self):
        global JOB
        path=urlsplit(self.path).path
        if path not in ('/api/scenario/discover','/api/scenario/download','/api/scenario/prepare','/api/scenario/library/rename','/api/scenario/library/plan','/api/scenario/library/delete'):return self._json(403,{'error':'POST endpoint not allowed'})
        if not self._post_training_request_ok():
            return self._json(403,{'error':'Same-origin JSON training request required'})
        try:
            count=int(self.headers.get('Content-Length','0'))
            limit = 256 * 1024 if path=='/api/scenario/library/plan' else 4096
            if not(0<count<=limit):raise ValueError('Invalid request size')
            body=json.loads(self.rfile.read(count))
            if path.startswith('/api/scenario/library/'):
                case_id=body.get('caseId','') if isinstance(body,dict) else ''
                if path.endswith('/rename'): result=scenario_library_rename(case_id,body.get('name',''))
                elif path.endswith('/plan'): result=scenario_library_save_plan(case_id,body.get('plan'))
                else: result=scenario_library_delete_prepared(case_id,body.get('activeCaseId',''))
                return self._json(200,result)
            spec=parse_spec(body)
            with STATE_LOCK:
                if JOB['state'] in ('downloading','preparing'):return self._json(409,{'error':'A download/preparation job is already active'})
            if path.endswith('/prepare'):
                raw_id=identifier(spec)
                if _raw_info(raw_id) is None:
                    raise ValueError('No cached raw radar archive for these exact selections. Download scans before preparing.')
                # Fail immediately (before queuing long conversion) if the
                # final scenario directory cannot accept files.
                check_prepared_output_writable()
                with STATE_LOCK:
                    JOB={'state':'preparing','phase':'preflight','percent':0,
                         'scenarioId':raw_id,'message':'Checking case output, then decoding Level II scans',
                         'completed':0,'total':0}
                threading.Thread(target=prepare_cached,args=(spec,),daemon=True).start()
                return self._json(202,{'state':'preparing','id':identifier(spec)})
            records=list_archive(spec)
            overview={'id':identifier(spec),'scanCount':len(records),'totalBytes':sum(r['bytes'] for r in records),
                      'first':records[0]['timestamp'],'last':records[-1]['timestamp'],
                      'status':'DISCOVERED_NOT_PLAYABLE'}
            if path.endswith('/discover'):return self._json(200,overview)
            available=shutil.disk_usage(CACHE_DIR if CACHE_DIR.is_dir() else ROOT).free
            if available < sum(r['bytes'] for r in records)*1.20 + 1_000_000_000:
                raise ValueError('Insufficient free disk for raw radar cache plus conversion workspace. Choose a shorter time window.')
            with STATE_LOCK:
                JOB={'state':'downloading','phase':'download','percent':0,
                     'scenarioId':identifier(spec),'message':'Downloading archived radar scans',
                     'completed':0,'total':len(records)}
            threading.Thread(target=download_records,args=(spec,records),daemon=True).start()
            return self._json(202,overview|{'state':'downloading'})
        except (ValueError,KeyError,TypeError,json.JSONDecodeError,URLError,HTTPError,ET.ParseError) as exc:
            return self._json(400,{'error':str(exc)[:300]})
        except Exception as exc:
            return self._json(502,{'error':('Archive unavailable: '+str(exc))[:250]})


def main():
    parser=argparse.ArgumentParser(description='Local-only instructor scenario downloader (TRAINING ONLY)')
    parser.add_argument('--port',type=int,default=8765)
    args=parser.parse_args()
    if not 1024<=args.port<=65535:parser.error('Port must be 1024..65535')
    # Codespaces accesses a loopback listener via GitHub's private port proxy.
    # Do not bind directly to a public network interface.
    server=ThreadingHTTPServer(('127.0.0.1',args.port),ScenarioHandler)
    print('TRAINING ONLY — Local scenario archive downloader, no operational warning functions')
    print(f'Open http://127.0.0.1:{args.port}/scenario_builder.html')
    if os.environ.get('CODESPACES')=='true':
        print('CODESPACES: Forward port privately in the Ports panel. NEVER set visibility to Public.')
        print('Private forwarded URL (if available): https://'+os.environ.get('CODESPACE_NAME','<name>')+f'-{args.port}.app.github.dev/scenario_builder.html')
    print('External downloads are backend-only from approved archive and NWS/Census/IEM reference sites.')
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()

if __name__=='__main__':main()
