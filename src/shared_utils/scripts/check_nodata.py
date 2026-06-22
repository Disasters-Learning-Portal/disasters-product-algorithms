import rasterio
from pathlib import Path

files = list(Path('.').glob('*.tif'))
new_files = [f for f in files if any(x in f.name for x in ['202501', '202506', '202507', '202410_Hurricane_Milton_ARIA', '202409_Hurricane_Helene_ARIA'])]

print(f'Checking {len(new_files)} new .tif files:')
print('='*70)

needs_conversion = []
for f in sorted(new_files):
    with rasterio.open(f) as src:
        dtype = src.dtypes[0]
        nodata = src.nodata
        status = 'OK' if nodata == 0 else 'NEEDS CONVERSION'
        if nodata != 0:
            needs_conversion.append(f.name)
        print(f'{status:16} | {f.name[:45]:45} | {dtype:8} | nodata={nodata}')

print('='*70)
if needs_conversion:
    print(f'\n{len(needs_conversion)} file(s) need conversion:')
    for fname in needs_conversion:
        print(f'  - {fname}')
else:
    print('\nAll files already have nodata=0')
