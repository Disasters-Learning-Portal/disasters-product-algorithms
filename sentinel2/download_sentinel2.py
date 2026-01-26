"""
download_sentinel2v2.py

Name:           Aaron Serre and Kaylee Sharp
Date:           October 2024, updated January 2026
"""

import requests
import os
import geopandas
import argparse
from datetime import datetime
from datetime import timedelta
import gc

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

# Logic to handle credentials via arguments (subprocess) or manual input
if args.user and args.password:
    cop_user = args.user[0]
    cop_pass = args.password[0]
    print("Credentials received via arguments.")
else:
    # Manual fallback for interactive use
    cop_user = input('Copernicus Username (email): ')
    cop_pass = input('Copernicus Password: ')

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

# Search Logic
collection_url = f"https://catalogue.dataspace.copernicus.eu/odata/v1/Products?$filter=Collection/Name eq 'SENTINEL-2'"
prod_filter = f" and Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'productType' and att/OData.CSC.StringAttribute/Value eq '{level}')"
date_filter = f" and ContentDate/Start gt {start_date}T00:00:00.000Z and ContentDate/Start lt {end_date}T23:59:59.999Z"
base_url = collection_url + prod_filter + date_filter
prods_to_download = []

if args.tile:
    for tile in tile_ids:
        if tile[0] == 'T': tile = tile[1:]
        tile_search = base_url + f" and Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'tileId' and att/OData.CSC.StringAttribute/Value eq '{tile}')&$top={limit}"
        json=requests.get(tile_search).json()
        if json.get('value'):
            for result in json['value']:
                prods_to_download.append((result['Id'], result['Name'], result['ContentLength']))

elif args.polygon:
    aoi_str = str(aoi_poly)
    poly_search = base_url + f" and OData.CSC.Intersects(area=geography'SRID=4326;{aoi_str}')&$top={limit}"
    json = requests.get(poly_search).json()
    if json.get('value'):
        for result in json['value']:
            prods_to_download.append((result['Id'], result['Name'], result['ContentLength']))

elif args.point:
    point_search = base_url + f" and OData.CSC.Intersects(area=geography'SRID=4326;POINT({lon} {lat})')&$top={limit}"
    json = requests.get(point_search).json()
    if json.get('value'):
        for result in json['value']:
            prods_to_download.append((result['Id'], result['Name'], result['ContentLength']))

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

print('\n')
for i, prod in enumerate(prods_to_download):
    id, safe_name, length = prod
    outname = os.path.join(out_dir, safe_name+'.zip')

    if os.path.isfile(outname) and os.path.getsize(outname) == length:
        print(f'{safe_name} already exists!', f'\t{i+1}/{num_of_prods}')
        continue
    
    print('Downloading: ', safe_name, f'\t{i+1}/{num_of_prods}')
    then = datetime.now()
    try:
        url = f"https://zipper.dataspace.copernicus.eu/odata/v1/Products({id})/$value"
        response = session.get(url, allow_redirects=True, stream=True)

        with open(outname, "wb") as file:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk: file.write(chunk)

        print('\t* Download Time (s): ', (datetime.now()-then).total_seconds())
        
        # Refresh for next item
        keycloak_token, refresh_token = get_refresh(refresh_token)
        session.headers.update({"Authorization": f"Bearer {keycloak_token}"})
    except Exception as e:
        print(f'\t* Error downloading {safe_name}: {e}')

print('\nDownload complete!')
os.system(f'chmod -R -f ug+rwx {out_dir}')
    