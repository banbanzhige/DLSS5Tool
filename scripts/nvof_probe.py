"""Standalone NVOFA synthetic translation probe; shares the production ABI."""
import json
import time
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dlss5tool.nvofa import OpticalFlow


def main():
    import argparse
    from pathlib import Path
    import cv2
    import numpy as np
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    # Known synthetic translation checks ordering, scaling, fixed-point decode.
    rng=np.random.default_rng(19)
    first=rng.integers(0,256,(384,512,3),dtype=np.uint8)
    first=cv2.GaussianBlur(first,(5,5),0)
    second=cv2.warpAffine(first,np.float32([[1,0,8],[0,1,4]]),(512,384),borderMode=cv2.BORDER_REFLECT)
    engine=OpticalFlow(512,384)
    durations=[]
    for i in range(24):
        tick=time.perf_counter();flow=engine.calculate(first,second)
        if i>=4:durations.append((time.perf_counter()-tick)*1000)
    roi=flow[32:-32,32:-32]
    error=np.linalg.norm(roi-np.float32([8,4]),axis=-1)
    reverse=engine.calculate(second,first)[32:-32,32:-32]
    record={'api':'2.0','grid':4,'quality':'slow','temporal_hints':False,
        'mean_ms':float(np.mean(durations)),'p95_ms':float(np.percentile(durations,95)),
        'median_flow':np.median(roi,axis=(0,1)).tolist(),'epe_mean':float(error.mean()),
        'reverse_median_flow':np.median(reverse,axis=(0,1)).tolist(),
        'finite':bool(np.isfinite(flow).all())}
    print(json.dumps(record),flush=True)
    if args.output:
        with args.output.open('x',encoding='utf-8') as handle:
            json.dump(record,handle,indent=2)
    assert record['finite'] and record['epe_mean'] < 1.0,record
    assert np.max(np.abs(np.array(record['reverse_median_flow'])+np.array([8,4]))) < 1,record
    engine.close()


if __name__=='__main__':main()
