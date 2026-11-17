#!/usr/bin/env python3
"""Fetch and validate training county/CWA/city/road vectors from official GIS.

All reference requests are pinned to NOAA's NWS reference map service and
Census TIGERweb. Absent CWA/county/city geometry is a hard preparation failure.
No geographic outlines or place names are fabricated.
"""
from __future__ import annotations
import json, math, os, re, secrets
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode

NWS = 'https://mapservices.weather.noaa.gov/static/rest/services/nws_reference_maps/nws_reference_map/FeatureServer'
CENSUS_PLACE = 'https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Places_CouSub_ConCity_SubMCD/MapServer'
CENSUS_ROAD = 'https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Transportation_LargeScale/MapServer'
MAX_GIS_BYTES = 18 * 1024 * 1024

def _fetch(layer, where='1=1', bounds=None, max_count=1500):
    q={'where':where,'outFields':'*','returnGeometry':'true','outSR':'4326',
       'f':'geojson','maxRecordCount':str(max_count),'resultRecordCount':str(max_count),
       'geometryPrecision':'6'}
    if bounds:
        q.update({'geometry':','.join(str(round(v,5)) for v in bounds),
                  'geometryType':'esriGeometryEnvelope','inSR':'4326',
                  'spatialRel':'esriSpatialRelIntersects'})
    url=layer+'/query?'+urlencode(q)
    with urlopen(Request(url,headers={'User-Agent':'WDS-Training-CaseBuilder/3.35.0.2'}),timeout=45) as r:
        blob=r.read(MAX_GIS_BYTES+1)
    if len(blob)>MAX_GIS_BYTES:raise ValueError('GIS layer exceeds safe response size')
    doc=json.loads(blob)
    if not isinstance(doc,dict) or doc.get('type')!='FeatureCollection' or not isinstance(doc.get('features'),list):
        raise ValueError('GIS service did not return a GeoJSON FeatureCollection')
    if doc.get('properties',{}).get('exceededTransferLimit') or doc.get('exceededTransferLimit'):
        raise ValueError('GIS layer truncated; reduce geographic extent')
    return doc['features']

def _polylines(geom):
    t=geom.get('type');c=geom.get('coordinates',[])
    if t=='Polygon':return c
    if t=='MultiPolygon':return [ring for poly in c for ring in poly]
    if t=='LineString':return [c]
    if t=='MultiLineString':return c
    return []

def _clean_ring(points, closed=False):
    result=[]
    for p in points:
        if not isinstance(p,(list,tuple)) or len(p)<2:continue
        a,b=float(p[0]),float(p[1])
        if not (math.isfinite(a) and math.isfinite(b) and -126<=a<=-65 and 24<=b<=51):continue
        loc=[round(a,5),round(b,5)]
        if not result or loc!=result[-1]:result.append(loc)
    if closed and len(result)>=3 and result[0]!=result[-1]:result.append(result[0])
    return result if len(result)>=(4 if closed else 2) else []

def _shape(feature, polygon=False):
    geom=feature.get('geometry') or {}
    parts=[_clean_ring(part, polygon) for part in _polylines(geom)]
    return [p for p in parts if p]

def _bounds(parts):
    pts=[p for ring in parts for p in ring]
    if not pts:return None
    return [min(p[0] for p in pts),min(p[1] for p in pts),max(p[0] for p in pts),max(p[1] for p in pts)]

def _point(feature, parts):
    props=feature.get('properties') or {}
    for lonkey,latkey in [('lon','lat'),('LON','LAT'),('INTPTLON','INTPTLAT')]:
        try:
            lon=float(props[lonkey]);lat=float(props[latkey])
            if -126<=lon<=-65 and 24<=lat<=51:return [round(lon,5),round(lat,5)]
        except (KeyError,ValueError,TypeError):pass
    try:
        from shapely.geometry import shape
        geom=shape(feature['geometry'])
        pt=geom.representative_point()
        return [round(pt.x,5),round(pt.y,5)]
    except Exception:
        box=_bounds(parts)
        return [(box[0]+box[2])/2,(box[1]+box[3])/2] if box else None

def _poly_feature(feat, name_key, country_state=False):
    props=feat.get('properties') or {}
    parts=_shape(feat,True)
    if not parts:return None
    n=str(props.get(name_key) or '').strip()
    if not n:return None
    result={'n':n,'b':_bounds(parts),'r':parts,'p':parts,'at':_point(feat,parts)}
    if country_state:
        result.update({'state':str(props.get('state') or ''),'id':str(props.get('fips') or props.get('objectid') or ''),
                       'cwaCodes':str(props.get('cwa') or '')})
    return result

def _lines(features):
    result=[]
    for feat in features:
        parts=_shape(feat,False)
        if not parts:continue
        props=feat.get('properties') or {}
        n=str(props.get('BASENAME') or props.get('FULLNAME') or props.get('NAME') or '')[:72]
        result.append({'n':n,'b':_bounds(parts),'p':parts})
    return result

def _city_features(features):
    result=[];seen=set()
    for feat in features:
        props=feat.get('properties') or {};name=str(props.get('BASENAME') or props.get('NAME') or '').strip()
        if not name:continue
        parts=_shape(feat,True);pos=_point(feat,parts)
        if not pos:continue
        ident=(name,tuple(pos))
        if ident in seen:continue
        seen.add(ident)
        b=_bounds(parts) or [pos[0],pos[1],pos[0],pos[1]]
        result.append({'n':name,'state':str(props.get('STUSAB') or props.get('STATE') or ''),
                       'at':pos,'b':b,'p':parts})
    return result

def _office_time_zone(counties,dest):
    from collections import Counter
    zones=Counter((r.get('properties') or {}).get('time_zone') for r in counties
                  if (r.get('properties') or {}).get('time_zone'))
    codes={'E':'eastern','C':'central','M':'mountain','P':'pacific'}
    for key,_ in zones.most_common():
        if str(key).upper()[:1] in codes:return codes[str(key).upper()[:1]]
    # Fallback is only a display time-zone preference; never used for UTC archives.
    lon=float(dest['lon'])
    return 'pacific' if lon<-114 else 'mountain' if lon<-102 else 'central' if lon<-86 else 'eastern'

def build_geography(wfo, dest, cache_dir, staging, progress):
    if not re.fullmatch(r'[A-Z]{3}',str(wfo)):raise ValueError('Invalid training WFO')
    if not (-125<float(dest['lon'])<-66 and 24<float(dest['lat'])<50.5):raise ValueError('Invalid destination coordinates')
    cache_dir=Path(cache_dir);cache_dir.mkdir(parents=True,exist_ok=True)
    Path(staging).mkdir(parents=True,exist_ok=True)
    cache=cache_dir/f'wds_gis_{wfo}_{dest["id"]}_v33502.json'
    if cache.is_file():
        doc=json.loads(cache.read_text(encoding='utf-8'))
        if doc.get('schema')=='wds-trusted-gis-cache-v33502':
            (Path(staging)/'basemap_static.json').write_text(json.dumps(doc['basemap'],separators=(',',':')),encoding='utf-8')
            (Path(staging)/'warning_geography.json').write_text(json.dumps(doc['warning'],separators=(',',':')),encoding='utf-8')
            return doc['summary']
    lat=float(dest['lat']);lon=float(dest['lon'])
    # Approx. 270 km radius, enough for primary Level II training coverage.
    delta_lon=min(5.5,3.0/max(.45,math.cos(math.radians(lat))))
    bounds=[lon-delta_lon,lat-2.5,lon+delta_lon,lat+2.5]
    progress('Fetching official NWS CWA and county polygons')
    cwa_raw=_fetch(NWS+'/1',f"wfo='{wfo}'")
    cwa=[p for f in cwa_raw if (p:=_poly_feature(f,'cwa'))]
    if not cwa:raise ValueError(f'No validated official NWS CWA polygon for {wfo}')
    county_raw=_fetch(NWS+'/2', bounds=bounds)
    county=[p for f in county_raw if (p:=_poly_feature(f,'countyname',True))]
    if not county:raise ValueError('No validated NWS county polygons near training radar')
    if not any(wfo in (x.get('cwaCodes') or '') for x in county):
        # CWA polygon may extend away from destination radar, but that would
        # make warning clipping unsafe; fail rather than assign invented counties.
        raise ValueError('Selected radar is not near counties belonging to training WFO '+wfo)
    office=(cwa_raw[0].get('properties') or {})
    progress('Fetching Census incorporated places and CDPs')
    cities=[]
    for layer in [4,5]:
        cities.extend(_city_features(_fetch(CENSUS_PLACE+f'/{layer}', bounds=bounds)))
    if not cities:raise ValueError('No verified Census cities/places returned for destination')
    progress('Fetching Census primary/secondary roads')
    primary=[];secondary=[]
    # Roads are useful context but not required for issuance. Preserve errors
    # in server logs and return a real road count; never manufacture segments.
    try:
        primary=_lines(_fetch(CENSUS_ROAD+'/0',bounds=bounds,max_count=2000))
        secondary=_lines(_fetch(CENSUS_ROAD+'/1',bounds=bounds,max_count=2000))
    except Exception as e:
        print('WARNING: Optional road layer download failed:',e,flush=True)
    states=[]
    try:
        states=[p for f in _fetch(NWS+'/3',bounds=bounds) if (p:=_poly_feature(f,'state'))]
    except Exception as e:
        print('WARNING: Optional state layer download failed:',e,flush=True)
    warning={'schema':'warning-simulator-warning-geo-v1',
             'layers':{'cwa':cwa,'county':county}}
    basemap={'schema':'warning-simulator-static-basemap-v2',
             'layers':{'cwa':cwa,'county':county,'state':states,
                       'road':primary,'secondary_road':secondary,'city':cities}}
    summary={'wfo':wfo,'office':{'citystate':str(office.get('citystate') or wfo),
                                   'city':str(office.get('city') or wfo)},
             'time_zone':_office_time_zone(county_raw,dest),
             'cwa_counties':sum(wfo in (x.get('cwaCodes') or '') for x in county),
             'mapped_places':len(cities),'primary_road_segments':len(primary),
             'secondary_road_segments':len(secondary)}
    (Path(staging)/'warning_geography.json').write_text(json.dumps(warning,separators=(',',':')),encoding='utf-8')
    (Path(staging)/'basemap_static.json').write_text(json.dumps(basemap,separators=(',',':')),encoding='utf-8')
    tmp=cache.with_name('.'+cache.name+'.'+secrets.token_hex(4)+'.tmp')
    try:tmp.write_text(json.dumps({'schema':'wds-trusted-gis-cache-v33502','summary':summary,
                                   'warning':warning,'basemap':basemap},separators=(',',':')),encoding='utf-8');os.replace(tmp,cache)
    finally:tmp.unlink(missing_ok=True)
    return summary
