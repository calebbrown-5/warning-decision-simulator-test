#!/usr/bin/env python3
"""Native NEXRAD Level II -> browser WDS polar sweep format.

Requires ARM Py-ART (arm_pyart package), NumPy and netCDF4 installed on the
facilitator environment. Does NOT synthesize reflectivity or velocity.
"""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
import gzip, json, math, os, re

MAX_SWEEP_CELLS = 12_000_000
MOMENT_FIELDS = {
    'DBZH':('reflectivity','corrected_reflectivity','total_power'),
    'VRADH':('velocity','corrected_velocity','mean_doppler_velocity'),
    'WRADH':('spectrum_width',),
    'ZDR':('differential_reflectivity','corrected_differential_reflectivity'),
    'RHOHV':('cross_correlation_ratio','corrected_cross_correlation_ratio'),
    'PHIDP':('differential_phase','corrected_differential_phase'),
    'KDP':('specific_differential_phase','corrected_specific_differential_phase'),
}

def _utc(t):
    if hasattr(t,'isoformat'):
        if getattr(t,'tzinfo',None) is None:t=t.replace(tzinfo=timezone.utc)
        return t.isoformat(timespec='milliseconds').replace('+00:00','Z')
    raise ValueError('Missing radar ray time')

def _ray_times(radar):
    from netCDF4 import num2date
    import numpy as np
    values=np.asarray(radar.time['data'], dtype='float64')
    if not values.size: raise ValueError('Radar has no ray timestamps')
    times=num2date(values, radar.time['units'], only_use_cftime_datetimes=False, only_use_python_datetimes=True)
    return [_utc(x) for x in times]

def _range_info(ranges):
    import numpy as np
    centers=np.asarray(ranges,dtype='float64')
    if centers.size < 2 or np.any(~np.isfinite(centers)) or np.any(np.diff(centers)<=0):
        raise ValueError('Unsupported radar gate range array')
    diffs=np.diff(centers)
    if np.allclose(diffs,diffs[0],rtol=0,atol=1e-3):
        return {'kind':'uniform','first_gate_center_m':round(float(centers[0]),3),
                'gate_spacing_m':round(float(diffs[0]),3),'gate_count':int(centers.size)}
    return {'kind':'nonuniform','range_centers_m':centers.round(3).tolist(),'gate_count':int(centers.size)}

def _field_key(fields,aliases):
    for key in aliases:
        if key in fields:return key
    return None

def _moment_from_array(values, path, metabase):
    import numpy as np
    arr = np.ma.asarray(values)
    vals = np.asarray(arr.filled(np.nan),dtype='<f4')
    finite = np.isfinite(vals) & ~np.ma.getmaskarray(arr)
    q = np.zeros(vals.shape,dtype='uint8')
    q[finite]=2
    vals[~finite]=np.nan
    valid=int(np.count_nonzero(finite));total=vals.size
    with gzip.open(path,'wb',compresslevel=4) as f:f.write(vals.tobytes(order='C'))
    qpath = path.with_name(path.name.replace('.f32.gz','.q8.gz'))
    with gzip.open(qpath,'wb',compresslevel=4) as f:f.write(q.tobytes(order='C'))
    return {'path':str(path.relative_to(metabase)),'quality_path':str(qpath.relative_to(metabase)),
        'shape':list(vals.shape),'finite_gate_count':valid,'missing_gate_count':total-valid,
        'range_folded_gate_count':0,'source':'native'}

def convert_cached(data, staging, progress):
    try:
        import numpy as np
        import pyart
    except ImportError as exc:
        raise RuntimeError('Level II decoder unavailable: install requirements with python -m pip install -r WDS_PREP_REQUIREMENTS.txt') from exc
    base=Path(data['_base']);stage=Path(staging);raw=base/'raw'
    spec=data.get('spec',{});site_id=spec.get('destination');origin=spec.get('source')
    if not re.fullmatch(r'K[A-Z]{3}',str(origin)) or not re.fullmatch(r'K[A-Z]{3}',str(site_id)):
        raise ValueError('Missing source/destination radar identity')
    from scenario_backend import catalog
    site=next((r for r in catalog()['radars'] if r['id']==site_id),None)
    if site is None:raise ValueError('Destination radar missing from verified site catalog')
    stage.mkdir(parents=True, exist_ok=True)
    outdir=stage/'moments';outdir.mkdir(exist_ok=True)
    volumes=[];velocity_count=0;reflection_count=0
    rawvol=data.get('volumes',[])
    if not rawvol:raise ValueError('Cached archive has no volumes')
    for vi,src in enumerate(rawvol):
        name=src.get('file','')
        if not re.fullmatch(r'K[A-Z]{3}\d{8}_\d{6}(?:_V\d\d)?(?:\.gz)?',name):raise ValueError('Invalid radar archive filename')
        p=raw/name
        if not p.is_file() or p.stat().st_size!=src.get('bytes'):
            raise ValueError('Missing/incomplete raw radar volume: '+name)
        try:
            radar=pyart.io.read_nexrad_archive(str(p))
        except Exception as exc:
            raise ValueError('Unable to decode '+name+': '+str(exc)[:180]) from exc
        ray_times=_ray_times(radar)
        elevations=np.asarray(radar.elevation['data'],dtype='float64')
        azimuths=np.asarray(radar.azimuth['data'],dtype='float64')
        ranges=np.asarray(radar.range['data'],dtype='float64')
        if ranges.size>7000:raise ValueError('Unsupported gates per sweep; large native volume')
        s_list=[]
        for si in range(radar.nsweeps):
            first=int(radar.sweep_start_ray_index['data'][si]);last=int(radar.sweep_end_ray_index['data'][si])+1
            count=last-first
            if count<=0 or count*ranges.size>MAX_SWEEP_CELLS:raise ValueError('Unsafe sweep size in '+name)
            fixed=float(radar.fixed_angle['data'][si])
            if not (-3<=fixed<=35):continue
            sw={'id':f'{vi}_{si}','first_ray_utc':ray_times[first],
                'last_ray_utc':ray_times[last-1], 'fixed_elevation_deg':round(fixed,3),
                'ray_azimuth_deg':np.round(azimuths[first:last],3).tolist(),
                'ray_elevation_deg':np.round(elevations[first:last],3).tolist(),
                'range':_range_info(ranges),'moments':{}}
            for label,aliases in MOMENT_FIELDS.items():
                field=_field_key(radar.fields,aliases)
                if not field:continue
                rawarr=radar.fields[field]['data'][first:last,:]
                if np.ma.count(rawarr)==0:continue
                output=outdir/f'v{vi:04d}_s{si:02d}_{label}.f32.gz'
                meta=_moment_from_array(rawarr,output,stage)
                if meta['finite_gate_count']:
                    sw['moments'][label]=meta
            if sw['moments'].get('DBZH'):reflection_count+=1
            if sw['moments'].get('VRADH'):velocity_count+=1
            if sw['moments']:s_list.append(sw)
        if not s_list:raise ValueError('No supported polar data in '+name)
        loc={'id':site['id'],'latitude_deg':float(site['lat']),
             'longitude_deg':float(site['lon']), 'altitude_m':float(np.asarray(radar.altitude['data']).flat[0])}
        volumes.append({'id':f'volume_{vi:04d}',
                        'volume_start_utc':min(s['first_ray_utc'] for s in s_list),
                        'volume_end_utc':max(s['last_ray_utc'] for s in s_list),
                        'site':loc,'sweeps':s_list})
        progress(vi+1,len(rawvol),f'Decoded {vi+1}/{len(rawvol)} volumes · base velocity in {velocity_count} cuts')
    if not velocity_count or not reflection_count:
        raise ValueError('Playable case requires REAL decoded reflectivity and base velocity; missing one or both')
    return {'schema':'warning-simulator-radar-manifest-v1','radar':site_id,
            'training_wfo':spec.get('wfo'),
            'relocated':origin!=site_id,'volumes':volumes,
            'provenance':{'source_radar':origin if origin==site_id else 'relocated',
                         'archive':'unidata-nexrad-level2',
                         'note':'Historical polar moments replayed at training radar; NOT real observations at destination.'}}
