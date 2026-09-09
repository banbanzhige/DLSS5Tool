"""Summarize existing independent route runs, never run GPU work."""
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]


def main():
    import cv2
    output=ROOT/'output'
    summary={}
    names=['realtime-routes-portrait-controlled','realtime-routes-portrait-throughput',
        'realtime-routes-square-throughput','realtime-routes-portrait-256',
        'realtime-cache-portrait-full','realtime-cache-portrait-optimized','realtime-cache-portrait-indexed',
        'realtime-cache-indexed-repeat-windows']
    for name in names:
        report=output/name/'report.json'
        if not report.exists():continue
        entries=json.loads(report.read_text(encoding='utf-8'))
        summary[name]=[]
        for entry in entries:
            if 'error' in entry:
                summary[name].append(entry);continue
            detail=json.loads((output/name/entry['variant']/'result.json').read_text(encoding='utf-8'))
            passes=detail['passes']
            first=passes[0]['samples']
            rows=[]
            for p in passes:
                row={k:v for k,v in p.items() if k!='samples'}
                # Early probe versions wrote zeros when no baseline was present.
                # Do not present those placeholders as measured equality/errors.
                if not (output/name/'reference.npy').exists():
                    row['rgb_mae']=row['rgb_max']=None
                if not (output/name/'guidance_sampled8.npy').exists():
                    row['flow_sampled8_epe_vs_baseline']=row['depth_sampled8_mae_vs_baseline']=None
                row['mean_guidance_ms']=sum(x['guidance_ms'] for x in p['samples'])/len(p['samples'])
                if p['pass'].startswith('warm') or p['pass']=='uncached_changed':
                    row['flow_matches_cold']=all(a['flow_sha256']==b['flow_sha256'] for a,b in zip(first,p['samples']))
                    row['depth_matches_cold']=all(a['depth_sha256']==b['depth_sha256'] for a,b in zip(first,p['samples']))
                    row['outputs_changed_from_cold']=sum(a['output_sha256']!=b['output_sha256'] for a,b in zip(first,p['samples']))
                rows.append(row)
            summary[name].append({'variant':detail['variant'],'dimensions':detail['dimensions'],
                'full':detail['full'],'speed_only':detail.get('speed_only',False),'passes':rows})
    videos=[]
    for folder in ['realtime-cache-portrait-full','realtime-cache-portrait-optimized','realtime-routes-portrait-hardware-video']:
        for path in (output/folder).glob('*/*.mp4'):
            cap=cv2.VideoCapture(str(path));count=0;shape=None
            while True:
                ok,frame=cap.read()
                if not ok:break
                count+=1;shape=list(frame.shape)
            cap.release()
            videos.append({'path':str(path),'decoded_frames':count,'shape':shape})
    summary['videos']=videos
    destination=output/'realtime-routes-summary.json'
    destination.write_text(json.dumps(summary,indent=2),encoding='utf-8')
    for name,entries in summary.items():
        print(name)
        if name=='videos': print(json.dumps(entries));continue
        for e in entries:
            for p in e.get('passes',[]):
                print(e['variant'],p['pass'],round(p['core_fps'],2),'decode',p.get('decode_process_fps'),
                    'p95',round(p['p95_ms'],2),'guidance',round(p['mean_guidance_ms'],2),
                    'equal',p.get('all_replay_exact'),'raw',p.get('flow_matches_cold'),p.get('depth_matches_cold'))


if __name__=='__main__':main()
