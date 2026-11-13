"""Create an isolated, reproducible synthetic RFI annotation playground."""
import argparse
import json
from pathlib import Path

import numpy as np
from astropy.io import fits


def create_demo(root: Path):
    root.mkdir(parents=True, exist_ok=False)  # Never overwrite an existing demo.
    data=root/'data';data.mkdir()
    one=root/'one_channel_rfi_v2_products';one.mkdir()
    sat=root/'satellite_rfi_1380_products';sat.mkdir()
    (root/'masks_demo').mkdir()
    rng=np.random.default_rng(20261101)
    freq=np.linspace(1320.,1420.,512)
    r=np.arange(128)[:,None,None];c=np.arange(512)[None,:,None]
    baseline=20+np.sin(c/65)*.2+np.cos(r/40)*.03+np.array([0.,.35])
    for serial in (1,2):
        mjd=60000+serial+np.arange(128)/86400
        for beam in range(1,20):
            flux=(baseline+rng.normal(0,.025,(128,512,2))+beam*.004).astype('float32')
            flux[:,243,:]+=.65
            flux[:,268,0]+=.85
            flux[:,279,1]+=.75
            flux[58:61,:,:]+=.32
            flux[68:75,253:259,:]+=.8
            flux[80:84,292:296,:]=np.nan
            flux[44+serial:47+serial,233:236,beam%2]+=.5
            path=data/f'SYNTHETIC_GRID_DEMO_arcdrift-M{beam:02}_W_{serial:04}_T.fits'
            h=fits.ImageHDU(freq,name='FREQ');h.header['BUNIT']='MHz'
            fits.HDUList([fits.PrimaryHDU(),h,fits.ImageHDU(mjd,name='MJD'),fits.ImageHDU(flux,name='FLUX')]).writeto(path)
            common=dict(freq=freq,mjd=mjd,axis_order=np.asarray('time, frequency, polarization'),processing_mode=np.asarray('mask_only'))
            mask=np.zeros(flux.shape,dtype='uint8');mask[:,243,:]=1
            np.savez_compressed(one/f'{path.stem}_1channel_rfi_v2_mask.npz',mask=mask,**common)
            mask[:]=0;mask[94:98,305:313,:]=1
            np.savez_compressed(sat/f'{path.stem}_satellite_rfi_1380_mask.npz',mask=mask,
                science_data_modified=np.asarray(False),science_mask_eligible=np.asarray(True),input_file=np.asarray(path.name),**common)
    (root/'DEMO.json').write_text(json.dumps(dict(synthetic=True,seed=20261101,nfiles=2,beams=19,
        shape=[128,512,2],description='Synthetic interaction fixture; no real observations or human masks.'),indent=2)+'\n')
    print(root.resolve())


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    create_demo(parser.parse_args().directory)
