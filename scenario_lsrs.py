#!/usr/bin/env python3
"""Historical IEM LSRs for training. Never transport LSR choices/warnings to Cloudflare.

IEM event valid times are not authoritative message issuance times. We explicitly
mark this and let the facilitator apply simulated reveal delays. For relocated
cases, historical remarks and original coordinates are NOT shipped to trainees.
"""
from __future__ import annotations
from datetime import datetime,timezone
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.parse import urlencode
import csv,hashlib,io,json,math,os,re,secrets

IEM='https://mesonet.agron.iastate.edu/cgi-bin/request/gis/lsr.py'

def _utc(value):
    s=str(value or '').strip().replace(' ','T')
    if not s:return None
    if s.endswith('Z'):s=s[:-1]+'+00:00'
    try:
        t=datetime.fromisoformat(s)
        if t.tzinfo is None:t=t.replace(tzinfo=timezone.utc)
        return t.astimezone(timezone.utc)
    except ValueError:return None

def _km(a,b,c,d):
    lat1,lat2=math.radians(a),math.radians(c)
    dlat=lat2-lat1;dlon=math.radians(d-b)
    h=math.sin(dlat/2)**2+math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    return 6371*2*math.asin(min(1,math.sqrt(h)))

def _category(text):
    t=str(text or '').upper()
    if 'HAIL' in t:return 'hail'
    if 'TORNADO' in t:return 'tornado'
    if 'WIND' in t or 'TSTM WND' in t or 'DOWNBURST' in t:return 'wind'
    return None

def _label(category,magnitude,text):
    if category=='hail':return (f'{magnitude:g}\" hail' if magnitude is not None else 'Hail')
    if category=='wind':return (f'{magnitude:g} mph wind' if magnitude is not None and 'GST' in str(text).upper() else 'Wind damage')
    return 'Tornado'

def _safe_source(raw):
    """Remove free-text place names from relocated trainee-readable JSON."""
    t=re.sub(r'[_-]+',' ',str(raw or '').upper()).strip()
    mapping=[(r'TRAINED.*SPOTTER|SKYWARN', 'TRAINED SPOTTER'),
             (r'STORM CHASER', 'STORM CHASER'),
             (r'LAW ENFORCEMENT|POLICE|SHERIFF','LAW ENFORCEMENT'),
             (r'EMERGENCY MANAG','EMERGENCY MANAGEMENT'),
             (r'FIRE DEPARTMENT','FIRE DEPARTMENT'),
             (r'BROADCAST|MEDIA|TV STATION|RADIO STATION','BROADCAST MEDIA'),
             (r'PUBLIC|CITIZEN','PUBLIC'),
             (r'ASOS','ASOS'),(r'AWOS','AWOS'),
             (r'MESONET','MESONET'),(r'COCORAHS','COCORAHS'),
             (r'NWS EMPLOYEE','NWS EMPLOYEE'),(r'NWS STORM SURVEY','NWS STORM SURVEY')]
    for pattern,label in mapping:
        if re.search(pattern,t):return label
    return 'OTHER REPORT SOURCE' if t else ''

def _reports(rows,spec,source_site):
    start=_utc(spec['start']);end=_utc(spec['end'])
    origlat=float(source_site['lat']);origlon=float(source_site['lon'])
    from scenario_backend import catalog
    target=next((s for s in catalog()['radars'] if s['id']==spec['destination']),None)
    if target is None:raise ValueError('Destination radar missing')
    tlat=float(target['lat']);tlon=float(target['lon']);relocated=spec['source']!=spec['destination']
    out=[];ids=set()
    for row in rows:
        r={str(k or '').upper():str(v or '').strip() for k,v in row.items()}
        ts=_utc(r.get('VALID'));cat=_category(r.get('TYPETEXT') or r.get('TYPE'))
        if ts is None or not(start<=ts<=end) or not cat:continue
        try:lat=float(r.get('LAT',''));lon=float(r.get('LON',''))
        except ValueError:continue
        if not (24<=lat<=50.5 and -126<=lon<=-66) or _km(origlat,origlon,lat,lon)>270:continue
        # Uniform translation in a local tangent plane (training geography).
        moved_lat=tlat+(lat-origlat)
        moved_lon=tlon+(lon-origlon)*math.cos(math.radians(origlat))/max(.3,math.cos(math.radians(tlat)))
        if not (24<=moved_lat<=50.5 and -126<=moved_lon<=-66):continue
        try:mag=float(r['MAG']) if r.get('MAG') else None
        except ValueError:mag=None
        if mag is not None and not math.isfinite(mag):mag=None
        uid=hashlib.sha256(json.dumps([row,len(out)],sort_keys=True,default=str).encode()).hexdigest()[:16]
        while uid in ids: uid=hashlib.sha256((uid+'dup').encode()).hexdigest()[:16]
        ids.add(uid)
        report={'id':uid,'type':cat,'eventType':r.get('TYPETEXT','')[:80],
            'eventUtc':ts.isoformat(timespec='seconds').replace('+00:00','Z'),
            'issuanceUtc':ts.isoformat(timespec='seconds').replace('+00:00','Z'),
            'availabilityBasis':'event_time_proxy', 'mag':mag,
            'label':_label(cat,mag,r.get('TYPETEXT')),
            'source':(_safe_source(r.get('SOURCE','')) if relocated else r.get('SOURCE','')[:160]),
            'relocated':{'lat':round(moved_lat,5),'lon':round(moved_lon,5)},
            'trainingCwa':spec['wfo']}
        if not relocated:
            report['original']={'lat':round(lat,5),'lon':round(lon,5),
                'city':r.get('CITY','')[:120], 'county':r.get('COUNTY','')[:100],
                'state':r.get('STATE','')[:2], 'remarks':r.get('REMARK','')[:1200],
                'source':r.get('SOURCE','')[:160]}
        out.append(report)
        if len(out)>=3000:break
    out.sort(key=lambda r:(r['eventUtc'],r['id']))
    return out

def build_lsrs(spec, source_site, raw_base, staging, progress):
    progress('Retrieving IEM historical LSR source/remarks, if available')
    start=_utc(spec['start']);end=_utc(spec['end'])
    qs=urlencode({'wfo':'ALL','sts':start.strftime('%Y-%m-%dT%H:%MZ'),
                  'ets':end.strftime('%Y-%m-%dT%H:%MZ'),'fmt':'csv'})
    cache=Path(raw_base)/'original_lsrs.csv'
    status='available';rows=[]
    try:
        if not cache.is_file():
            with urlopen(Request(IEM+'?'+qs,headers={'User-Agent':'WDS-Training-CaseBuilder/3.35.0.2'}),timeout=45) as r:
                b=r.read(8_000_001)
            if len(b)>8_000_000:raise ValueError('IEM report response exceeded 8 MB')
            if not b.strip().upper().startswith(b'VALID,') and b'VALID' not in b[:200].upper():
                raise ValueError('IEM response is not a valid LSR CSV')
            tmp=cache.with_name('.original_lsrs.'+secrets.token_hex(4)+'.tmp')
            try:tmp.write_bytes(b);os.replace(tmp,cache)
            finally:tmp.unlink(missing_ok=True)
        with cache.open(encoding='utf-8-sig',newline='') as f:
            rows=list(csv.DictReader(f))
        if not rows or 'VALID' not in {str(k).upper() for k in rows[0]}:raise ValueError('Missing IEM VALID column')
        reports=_reports(rows,spec,source_site)
    except Exception as e:
        print('WARNING: Optional IEM report archive unavailable:',e,flush=True)
        status='unavailable';reports=[]
    suggested=[r['id'] for r in reports]
    result={'schema':'warning-simulator-lsrs-v1','allReports':reports,
            'suggestedIds':suggested,
            'meta':{'availabilityNote':'IEM event-valid times used; actual product issuance times not known',
                    'relocated':spec['source']!=spec['destination']}}
    (Path(staging)/'storm_reports.json').write_text(json.dumps(result,separators=(',',':')),encoding='utf-8')
    return {'count':len(reports),'suggested':len(suggested),'status':status,
            'availability_basis':'event_time_proxy'}
