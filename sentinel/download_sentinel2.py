"""
download_sentinel2.py

Name:           Kaylee Sharp
Date:           October 2024
"""

import requests
import os
import geopandas
import argparse
from datetime import datetime
from datetime import timedelta

parser=argparse.ArgumentParser(
        description='''This script downloads Sentinel-2 from the Copernicus OData API by list of tile IDs, polygon shapefile, or a single point.''',
        usage='process_sentinel2.py output [-h] [-date [DATE ...]] [-tile [TILE ...]] [-polygon [/PATH/TO/FILE.SHP]] [-point [LON LAT]]')
parser.add_argument('output', nargs=1, help= 'Path to directory to download the .zip files (e.g.\
                    /data/esops/eventData/2023/TurkeyEarthquake/sentinel2).')
parser.add_argument('-date', nargs='*', default=False, help='Date (e.g. 20230116) or start/end dates (e.g. 20230116 20230120). \
                    Default is last 10 days.')
parser.add_argument('-tile', nargs ='*', default=False, help='List of tile IDs (e.g. 17RLN 17RLP 17RLQ) or path to .txt file\
                   where each tile ID is on a new line.')
parser.add_argument('-polygon', nargs=1, default=False, help='Absolute path to shapefile containing one polygon in WGS84 (4326). The .shx file must \
                    must also be in the same directory as the .shp file.')
parser.add_argument('-point', nargs=2, default=False, help='Longitude and latitude of single point (e.g. -86.6 34.5).')
parser.add_argument('-level', nargs=1, default='2', help='Processing level (1 = L1C, 2 = L2A).')
parser.add_argument('-limit', nargs=1, default='50', help='Limit number of search results returned from API. Default is 50.')
args=parser.parse_args()


###########INPUTS###########
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
        print('ERROR: Invalid polygon. Ensure the following:')
        print('\t* Inputted path is absolute path to .shp file, NOT a directory.')
        print('\t* The .shx file is in the same directory as the .shp file.')
        print('\t* The shapefile contains a single polygon (not line or point).')
        print('\t* The polygon is in WGS84 (EPSG:4326)')
elif (args.point):
    # format lat/lon point
    lon = args.point[0]
    lat = args.point[1]
    print('Point:', args.point)

############################

def get_keycloak(username: str, password: str):
    # get keycloack token from Copernicus
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
        raise Exception(
            f"Keycloak token creation failed. Reponse from the server was: {r.json()}"
        )
    return (r.json()["access_token"], r.json()["refresh_token"])

def get_refresh(refresh):
    # refresh token
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
        raise Exception(
            f"Keycloak token creation failed. Reponse from the server was: {r.json()}"
        )
    return (r.json()["access_token"], r.json()["refresh_token"])

# create base url for searching
collection_url = f"https://catalogue.dataspace.copernicus.eu/odata/v1/Products?$filter=Collection/Name eq 'SENTINEL-2'"
prod_filter = f" and Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'productType' and att/OData.CSC.StringAttribute/Value eq '{level}')"
date_filter = f" and ContentDate/Start gt {start_date}T00:00:00.000Z and ContentDate/Start lt {end_date}T23:59:59.999Z"
base_url = collection_url + prod_filter + date_filter
prods_to_download = []

if args.tile:
    for tile in tile_ids:
        if tile[0] == 'T':
            tile = tile[1:]
        # format search based on tile ID
        tile_search = base_url + f" and Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'tileId' and att/OData.CSC.StringAttribute/Value eq '{tile}')&$top={limit}"
        json=requests.get(tile_search).json()
        if not json['value']:
            print('No images found for tile: ', tile)
        else:
            # compile all products to download
            for result in json['value']:
                safe_name = result['Name']
                id = result['Id']
                length =  result['ContentLength']
                prods_to_download.append((id, safe_name, length))

elif args.polygon:
    # format search based on polygon geometry
    aoi_str = str(aoi_poly)
    poly_search = base_url + f" and OData.CSC.Intersects(area=geography'SRID=4326;{aoi_str}')&$top={limit}"
    json = requests.get(poly_search).json()
    if not json['value']:
        print('No images found intersecting polygon.')
    else:
        # compile all products to download
        for result in json['value']:
            safe_name = result['Name']
            id = result['Id']
            length = result['ContentLength']
            prods_to_download.append((id, safe_name, length))

elif args.point:
    # format search based on lat/lon coordinate
    point_search = base_url + f" and OData.CSC.Intersects(area=geography'SRID=4326;POINT({lon} {lat})')&$top={limit}"
    json = requests.get(point_search).json()
    if not json['value']:
        print('No images found intersecting point.')
    else:
        # compile all products to download
        for result in json['value']:
            safe_name = result['Name']
            id = result['Id']
            length = result['ContentLength']
            prods_to_download.append((id, safe_name, length))

# display number of products and estimate time to download
num_of_prods = len(prods_to_download)
est_time = num_of_prods*2
hours = est_time // 60
remaining_minutes = est_time % 60
print('\nNumber of products to download: ', num_of_prods)
print(f'Estimated download time: {hours} hr {remaining_minutes} min')

# confirm download
confirm = input('Confirm download (y/n): ').strip().lower()
if confirm == 'n':
    print('Goodbye!')
    quit()

# set up requests session and tokens
session = requests.Session()
keycloak_token, refresh_token = get_keycloak(cop_user,cop_pass)
session.headers.update({"Authorization": f"Bearer {keycloak_token}"})

print('\n')
for i,prod in enumerate(prods_to_download):
    # create output filename
    id, safe_name, length = prod
    outname = os.path.join(out_dir, safe_name+'.zip')

    # check for filename and correct size
    if os.path.isfile(outname):
        if os.path.getsize(outname) == length:
            print(f'{safe_name} already downloaded!', f'\t{i+1}/{num_of_prods}')
            continue
        else:
            # file did not download completely
            print('Downloading: ', safe_name, f'\t{i+1}/{num_of_prods}')
    else:
        # new download
        print('Downloading: ', safe_name, f'\t{i+1}/{num_of_prods}')
    
    then = datetime.now()
    try:
        # request product
        url = f"https://zipper.dataspace.copernicus.eu/odata/v1/Products({id})/$value"
        response = session.get(url, allow_redirects=True)

        # write product to file
        with open(outname, "wb") as file:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    file.write(chunk)
        file.close()

        # compute download time
        now = datetime.now()
        print('\t* Download Time (s): ', (now-then).total_seconds())

        # create new session and refresh token
        # while not necessary each time, I chose to refresh to
        # guarantee the session/token doesn't expire
        session = requests.Session()
        keycloak_token,refresh_token = get_refresh(refresh_token)
        session.headers.update({"Authorization": f"Bearer {keycloak_token}"})
    except:
        print('\t* Server Error')

# update permissions
print('\nDownload complete!')
cmd = f'chmod -R -f ug+rwx {out_dir}'
os.system(cmd)
    