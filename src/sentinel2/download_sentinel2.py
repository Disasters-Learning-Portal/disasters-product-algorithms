"""
download_sentinel2v2.py

Name:           Aaron Serre and Kaylee Sharp
Date:           October 2024, updated January 2026
"""

import requests
import os
import geopandas
import argparse
import subprocess
from datetime import datetime
from datetime import timedelta
import gc
import sys

# Force unbuffered output for real-time display in JupyterHub/subprocess
# Flush stdout/stderr after every write
class Unbuffered(object):
    def __init__(self, stream):
        self.stream = stream
    def write(self, data):
        self.stream.write(data)
        self.stream.flush()
    def writelines(self, datas):
        self.stream.writelines(datas)
        self.stream.flush()
    def __getattr__(self, attr):
        return getattr(self.stream, attr)

sys.stdout = Unbuffered(sys.stdout)
sys.stderr = Unbuffered(sys.stderr)

parser=argparse.ArgumentParser(
        description='''This script downloads Sentinel-2 from the Copernicus OData API by list of tile IDs, polygon shapefile, or a single point.''',
        usage='process_sentinel2v2.py output [-h] [-date [DATE ...]] [-tile [TILE ...]] [-polygon [/PATH/TO/FILE.SHP]] [-point [LON LAT]] [-u USER] [-p PASS] [-y]')

parser.add_argument('output', nargs=1, help= 'Path to directory to download the .zip files.')
parser.add_argument('-date', nargs='*', default=False, help='Date (e.g. 20230116) or start/end dates.')
parser.add_argument('-tile', nargs ='*', default=False, help='List of tile IDs or path to .txt file.')
parser.add_argument('-polygon', nargs=1, default=False, help='Absolute path to shapefile.')
parser.add_argument('-point', nargs=2, default=False, help='Longitude and latitude of single point.')
parser.add_argument('-level', nargs=1, default='2', help='Processing level (1 = L1C, 2 = L2A).')
parser.add_argument('-limit', nargs=1, default='50', help='Limit number of search results.')

# --- NEW ARGUMENTS FOR SUBPROCESS USE ---
parser.add_argument('-u', '--user', nargs=1, help='Copernicus Username (email).')
parser.add_argument('-p', '--password', nargs=1, help='Copernicus Password.')
parser.add_argument('-y', '--yes', action='store_true', help='Skip confirmation prompt.')

args=parser.parse_args()


###########INPUTS###########

# Logic to handle credentials via arguments (subprocess), environment (DPS), or
# manual input. Precedence: CLI args -> env vars (COP_USER/COP_PASS) -> prompt.
# The env path lets a headless caller (e.g. the DPS run.sh, which pulls the values
# from MAAP secrets) pass credentials WITHOUT putting the password on the command
# line, so it never lands in argv / `ps` / a process log.
cop_user = args.user[0] if args.user else os.environ.get('COP_USER')
cop_pass = args.password[0] if args.password else os.environ.get('COP_PASS')
if cop_user and cop_pass:
    print("Credentials received.")
else:
    # Manual fallback for interactive use
    cop_user = cop_user or input('Copernicus Username (email): ')
    cop_pass = cop_pass or input('Copernicus Password: ')

# parse user arguments
out_dir = args.output[0]
print('\nOutput directory:', out_dir)
level = args.level[0]
if level=='2':
    level='L2A'
elif level=='1':
    level='L1C'
else:
    print('ERROR: Invalid level. Input either 1 or 2. Default is 2.')
print('Level:', level)
limit = args.limit[0]

if args.date:
    if len(args.date) == 1:
        # single date
        start_date = datetime.strptime(args.date[0], '%Y%m%d').strftime('%Y-%m-%d')
        end_date = start_date
        print('Date:', start_date)
    elif len(args.date) == 2:
        # date range
        start_date = datetime.strptime(args.date[0], '%Y%m%d').strftime('%Y-%m-%d')
        end_date =datetime.strptime(args.date[1], '%Y%m%d').strftime('%Y-%m-%d')
        print('Dates:', start_date, 'to', end_date)
    else:
        print('ERROR: Too many dates entered. Either enter one date or a start and end date.')
        quit()
else:
    # search past 10 days
    today = datetime.today()
    dt = timedelta(days=1)
    end_date = today - dt
    end_date = end_date.strftime('%Y-%m-%d')
    dt = timedelta(days=11)
    start_date = today - dt
    start_date = start_date.strftime('%Y-%m-%d')
    print('Dates:', start_date, 'to', end_date)

if not (args.tile or args.polygon or args.point):
    print('\nERROR: Please constrain your search geographically using either a list of tile IDs, a polygon shapefile, or a lon/lat point.')
    print('Use -h for guidance on formatting.')
    quit()

elif args.tile:
    if ((len(args.tile) == 1) & ('.txt' in args.tile[0])):
        # get tile IDs from inputted .txt file
        tile_ids = []
        with open(args.tile[0], 'r') as file:
            for line in file:
                tile_ids.append(line.strip())
        print('Tile IDs:', tile_ids)
    else:
        # get tile IDs entered directly into command line
        tile_ids=args.tile
        print('Tile IDs:', tile_ids)

elif (args.polygon):
    # read polygon file
    print('Polygon:', os.path.basename(args.polygon[0]))
    try:
        aoi_poly= geopandas.read_file(args.polygon[0])['geometry'].iloc[0]
    except:
        print('ERROR: Invalid polygon.')
        quit()
elif (args.point):
    # format lat/lon point
    lon = args.point[0]
    lat = args.point[1]
    print('Point:', args.point)

############################

def get_keycloak(username: str, password: str):
    data = {
        "client_id": "cdse-public",
        "username": username,
        "password": password,
        "grant_type": "password",
    }
    try:
        r = requests.post(
            "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token",
            data=data,
        )
        r.raise_for_status()
    except Exception as e:
        raise Exception(f"Keycloak token creation failed: {r.json()}")
    return (r.json()["access_token"], r.json()["refresh_token"])

def get_refresh(refresh):
    data = {
        "client_id": "cdse-public",
        "refresh_token": refresh,
        "grant_type": "refresh_token",
    }
    try:
        r = requests.post(
            "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token",
            data=data,
        )
        r.raise_for_status()
    except Exception as e:
        raise Exception(f"Refresh failed: {r.json()}")
    return (r.json()["access_token"], r.json()["refresh_token"])

def search_catalogue(url):
    """Run one OData product search. Returns (products, error).

    products = list of (Id, Name, ContentLength) tuples (possibly empty).
    error    = None on success, else a short human-readable string.

    Never raises — a failed search is surfaced to the caller and logged,
    never silently swallowed (mirrors the get_keycloak error-wrap style).
    """
    try:
        r = requests.get(url)
        r.raise_for_status()
    except Exception as e:
        return [], f"request failed: {e}"
    try:
        body = r.json()
    except Exception as e:
        return [], f"invalid JSON response: {e}"
    if isinstance(body, dict) and body.get('error'):
        return [], f"OData error: {body['error']}"
    values = body.get('value') or []
    prods = [(v['Id'], v['Name'], v['ContentLength']) for v in values]
    return prods, None

# Search Logic
collection_url = f"https://catalogue.dataspace.copernicus.eu/odata/v1/Products?$filter=Collection/Name eq 'SENTINEL-2'"
prod_filter = f" and Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'productType' and att/OData.CSC.StringAttribute/Value eq '{level}')"
date_filter = f" and ContentDate/Start gt {start_date}T00:00:00.000Z and ContentDate/Start lt {end_date}T23:59:59.999Z"
base_url = collection_url + prod_filter + date_filter
prods_to_download = []

if args.tile:
    print('\nSearching catalogue...')
    missing = []
    for tile in tile_ids:
        label = tile                                        # keep the operator's form for display
        query_tile = tile[1:] if tile[0] == 'T' else tile   # catalogue tileId has no leading T
        tile_search = base_url + f" and Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'tileId' and att/OData.CSC.StringAttribute/Value eq '{query_tile}')&$top={limit}"
        prods, err = search_catalogue(tile_search)
        if err:
            print(f'  tile {label} → search FAILED ({err}) ⚠')
            missing.append(label)
        elif not prods:
            print(f'  tile {label} → 0 products ⚠')
            missing.append(label)
        else:
            print(f'  tile {label} → {len(prods)} product(s)')
        prods_to_download.extend(prods)                      # [] on empty/error, so always safe
    if missing:
        print(f'\n⚠ {len(missing)} of {len(tile_ids)} tiles returned no products (or failed): {", ".join(missing)}')
        print('   Likely causes: (a) no acquisition for that tile on this date — Sentinel-2 revisit is ~5 days,')
        print('   so widen -date to a start/end range; (b) wrong -level (1=L1C vs 2=L2A);')
        print('   (c) a transient API error on a "search FAILED" line — just re-run.')

elif args.polygon:
    aoi_str = str(aoi_poly)
    poly_search = base_url + f" and OData.CSC.Intersects(area=geography'SRID=4326;{aoi_str}')&$top={limit}"
    prods, err = search_catalogue(poly_search)
    print(f'  polygon search FAILED ({err}) ⚠' if err else f'  polygon → {len(prods)} product(s)')
    prods_to_download.extend(prods)

elif args.point:
    point_search = base_url + f" and OData.CSC.Intersects(area=geography'SRID=4326;POINT({lon} {lat})')&$top={limit}"
    prods, err = search_catalogue(point_search)
    print(f'  point search FAILED ({err}) ⚠' if err else f'  point → {len(prods)} product(s)')
    prods_to_download.extend(prods)

num_of_prods = len(prods_to_download)
print('\nNumber of products to download: ', num_of_prods)

# Handle confirmation for Subprocess (using the -y flag)
if args.yes:
    print('Confirmation skipped (-y provided).')
else:
    confirm = input('Confirm download (y/n): ').strip().lower()
    if confirm == 'n':
        print('Goodbye!')
        quit()

# set up requests session and tokens
session = requests.Session()
keycloak_token, refresh_token = get_keycloak(cop_user, cop_pass)
session.headers.update({"Authorization": f"Bearer {keycloak_token}"})

total_files = len(prods_to_download)
print('\n')
# Explicit [i/total] file counter instead of a tqdm bar: through a subprocess
# pipe (non-TTY) tqdm's carriage-return counter doesn't advance visibly in the
# Jupyter cell, so we print one clear line per file.
for i, prod in enumerate(prods_to_download, start=1):
    id, safe_name, length = prod
    outname = os.path.join(out_dir, safe_name+'.zip')
    part = outname + '.part'
    # Copernicus product Names already end in ".SAFE", so the extracted
    # directory is `safe_name` itself. (Joining another ".SAFE" produced a dead
    # ".SAFE.SAFE" path that never matched anything.)
    safe_dir = os.path.join(out_dir, safe_name)

    # Skip if the zip already exists. The download below is atomic (streamed to
    # a .part file, renamed only on success), so `outname` exists only once a
    # previous run finished it -- presence alone is a reliable "done" marker.
    # We deliberately DON'T compare against the catalogue ContentLength: the
    # /$value zip transfer size is not guaranteed to equal it, so the old
    # `getsize(outname) == length` check was False every run and re-downloaded
    # the file forever.
    if os.path.isfile(outname):
        print(f'  [{i}/{total_files}] ✓ {safe_name}.zip already exists!')
        continue

    # Skip if an extracted .SAFE directory is already present.
    if os.path.isdir(safe_dir):
        print(f'  [{i}/{total_files}] ✓ {safe_name} already exists (extracted)!')
        continue

    print(f'  [{i}/{total_files}] → Downloading: {safe_name}')
    then = datetime.now()
    try:
        url = f"https://zipper.dataspace.copernicus.eu/odata/v1/Products({id})/$value"
        response = session.get(url, allow_redirects=True, stream=True)

        # Print one progress line per ~10% downloaded instead of a live tqdm
        # byte-bar. The bar's carriage-return refreshes (one per 8 KB chunk)
        # flood the subprocess -> Jupyter cell output; 10% milestones stay readable.
        downloaded = 0
        next_pct = 10
        total_mb = length / (1024 ** 2)
        with open(part, "wb") as file:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    file.write(chunk)
                    downloaded += len(chunk)
                    if length > 0:
                        pct = downloaded * 100 // length
                        if pct >= next_pct:
                            print(f'     {safe_name[:30]}  {min(pct, 100):3d}%  ({downloaded / (1024 ** 2):.0f}/{total_mb:.0f} MB)')
                            next_pct = (pct // 10 + 1) * 10

        # Atomic promote: the final filename appears only after a complete
        # download, so an interrupted run never leaves a complete-looking zip.
        os.replace(part, outname)

        elapsed = (datetime.now()-then).total_seconds()
        speed = length / (1024**2) / elapsed  # MB/s
        print(f'     ✓ Complete - {elapsed:.1f}s ({speed:.1f} MB/s)')

        # Refresh for next item
        keycloak_token, refresh_token = get_refresh(refresh_token)
        session.headers.update({"Authorization": f"Bearer {keycloak_token}"})
    except Exception as e:
        print(f'    ✗ Error downloading {safe_name}: {e}')
        # Remove any partial download so the next run starts clean.
        if os.path.exists(part):
            os.remove(part)

print('\nDownload complete!')
# Pass out_dir as an argv element (no shell) so path metacharacters can't be
# interpreted as commands. -f suppresses errors on entries we can't chmod.
subprocess.run(["chmod", "-R", "-f", "ug+rwx", out_dir], check=False)
    