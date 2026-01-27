#/usr/local/anaconda3/bin/python

"""
process_landsat89.py

Name:           Kaylee Sharp
Date:           February 2025

"""

from datetime import datetime
from datetime import date
import glob
import os
import time
import argparse
from pathlib import Path
from lxml import etree
from landsat.landsat89_functions import *
from shared_utils.cog_utils import convert_to_cog, rename_with_event, get_final_filename
from tqdm import tqdm
import traceback
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

if __name__ == "__main__":
    then = datetime.now()

    # Get inputs from user
    parser=argparse.ArgumentParser(
    	description='''This script unzips and processes Landsat8/9 surface reflectance\
    		     and produces true color, panchromatic, natural color, color infrared\
                         NDVI, NDWI, MNDWI, EVI, NBR, or Water Extent.''',
            usage='process_landsat89.py input [-h] [-p [P ...]] [-we_nstd [WE_NSTD ...]] [-date [DATE ...]] [-tile [TILE ...]] [-merge] [-mask] [-force] [-unzip_only]')
    parser.add_argument('input', nargs=1, help= 'Path to directory containing the .tar / .zip files (e.g.\
    		    /data/esops/eventData/2023/TurkeyEarthquake/landsat8).')
    parser.add_argument('-zip', nargs='*', default=False, help='Specific .tar / .zip files to unzip and process. \
                        Will not unzip or process other .zip files')
    parser.add_argument('-dir', nargs='*', default=False, help='Specify unpacked directories to process. Will not process other\
                         unpacked directories')
    parser.add_argument('-p', nargs='*', default=['true'], help='List of products to produce (all = everything, true = true color,\
    		     pan = panchromatic, nat = natural, colorIR = color infrared, mndwi = MNDWI, ndvi = NDVI,\
    		    evi = EVI, ndwi = NDWI, nbr = NBR, we = water extent)')
    parser.add_argument('-we_nstd', nargs='*', default=[1], help='Produce water extents with given number(s) of standard deviations (default is 1).\
                        More standard deviations = higher NIR threshold = more pixels classified as water.')
    parser.add_argument('-date', nargs='*', default = False, help='Date(s) (e.g. 20230116). If no date(s) or tile(s) is entered,\
    		    all dates found will be processed.')
    parser.add_argument('-tile', nargs ='*', default=False, help='Path/Row (e.g. 171035) for specific tile(s),\
                        If no tile(s) or date(s) is entered, all tiles found will be processed.')
    parser.add_argument('-merge', default=False, action='store_true', help='Merge all images by date and product.')
    parser.add_argument('-mask', default=False, action='store_true', help='Generate cloud mask and mask all images (non-masked version \
                        preserved as well).')
    parser.add_argument('-force', default=False, action="store_true", help='Force overwrite existing products.')
    parser.add_argument('-unzip_only', default=False, action="store_true", help='Just unzip all .tar files. Do not process.')
    parser.add_argument('-tif_only', default=False, action="store_true", help='Skip COG conversion and keep regular GeoTIFF format (COG is default).')
    parser.add_argument('-nodata', type=float, default=None, help='No-data value for COG outputs (auto-detected if not specified).')
    parser.add_argument('-compression', type=str, default='ZSTD', help='Compression type for COG (default: ZSTD).')
    parser.add_argument('-compression_level', type=int, default=22, help='Compression level for COG (default: 22 for ZSTD).')
    parser.add_argument('-dst_crs', type=str, default='EPSG:4326', help='Target CRS for COG output (default: EPSG:4326, use "native" to preserve original CRS).')
    parser.add_argument('-event', type=str, default=None, help='Event name for filename prefix (e.g., 202512_Flood_WA). Adds formatted date suffix.')
    args=parser.parse_args()

    # Handle dst_crs argument (convert "native" to None)
    dst_crs_value = None if args.dst_crs.lower() == 'native' else args.dst_crs

    print('\nInput:', args.input[0])
    input_dir = args.input[0]

    if not args.unzip_only:
      if 'all' in args.p:
        products = ['true', 'pan', 'nat', 'colorIR', 'mndwi', 'ndvi', 'evi', 'ndwi', 'nbr', 'we']
      else:
        # check for valid product types
        products = args.p
        product_variants = ['true', 'truecolor', 'tc', 'pan', 'panchromatic', 'nat', 'natural', 'nc', 'naturalcolor', 'colorir', 'colorinfrared', 'cir', 'mndwi', 'ndvi', 'evi', 'ndwi', 'nbr', 'we', 'waterExtent']
        invalid_products = [p for p in products if p.lower() not in product_variants]
        if invalid_products:
            print('Invalid product type(s): ', invalid_products)
            print('Please enter a product from the following list (not case sensitive): true (tc, truecolor), colorIR (cir), pan (panchromatic), nat (natural, naturalcolor), ndvi, ndwi, evi, nbr, we (waterExtent)')
            quit()
      print('Product(s) to produce:', products)
  
      # display and store inputted dates
      if args.date:
        print('Processing date(s):', args.date)
        dates = args.date
  
      # display and store inputted tiles
      if args.tile:
        print('Processing tile(s):', args.tile)
        tiles = args.tile

    unpacked_dir = os.path.join(input_dir, 'unpacked')

    if args.zip:
        # unzip files
        zip_files = args.zip
        zip_files = [os.path.join(input_dir, zip_file) for zip_file in zip_files if not os.path.isabs(zip_file)]
        unpacked_dirs = unzip_landsat(zip_files, unpacked_dir)
    elif args.dir:
        # gather inputted unpacked data directories
        unpacked_dirs = [os.path.join(unpacked_dir, p) for p in args.dir if not os.path.isabs(p)]
    else:
        tars = glob.glob(os.path.join(input_dir, '*.tar'))
        zips = glob.glob(os.path.join(input_dir, '*.zip'))
        if tars:
            zip_files = tars
        elif zips:
            zip_files = zips

        unpacked_dirs = glob.glob(os.path.join(unpacked_dir, '*/*unpacked'))
    
        if (zip_files and unpacked_dirs):
            # unzip files and add to the list of unpacked data directories
            new_unpacked_dirs = unzip_landsat(zip_files, unpacked_dir)
            unpacked_dirs = unpacked_dirs + new_unpacked_dirs
        elif zip_files:
            # unzip files
            unpacked_dirs = unzip_landsat(zip_files, unpacked_dir)
        elif not unpacked_dirs:
            # no files found, quit program 
            print('No .tar / .zip files or unpacked directories found! Check input path.')
            quit()
    
    # Gather directories mathcing inputted date and/or tile
    if  not args.unzip_only:
        prod_dirs = []  # store product directory paths; used for merging later
        data_dirs = []  # store data directory paths; used for processing
        if (args.date and args.tile):
            for unpacked_dir in unpacked_dirs:
                dir_base = os.path.basename(unpacked_dir)
                dir_parts = dir_base.split('_')
                tile = dir_parts[2]
                date = dir_parts[3]
                if (tile in tiles) and (date in dates):
                    data_dirs.append(unpacked_dir)
        elif args.date:
            for unpacked_dir in unpacked_dirs:
                dir_base = os.path.basename(unpacked_dir)
                dir_parts = dir_base.split('_')
                date = dir_parts[3]
                if date in dates:
                    data_dirs.append(unpacked_dir)
        elif args.tile:
            for unpacked_dir in unpacked_dirs:
                dir_base = os.path.basename(unpacked_dir)
                dir_parts = dir_base.split('_')
                tile = dir_parts[2]
                if tile in tiles:
                    data_dirs.append(unpacked_dir)
        else:
            data_dirs = unpacked_dirs
  
        # number of directories to process
        num_dirs = len(data_dirs)
        print(f'\nProcessing {num_dirs} directories.')

        # create output directory
        out_dir = os.path.join(input_dir, 'output')
        if not os.path.isdir(out_dir):
            os.mkdir(out_dir)

        # Initialize error tracking
        processing_errors = []  # List of (scene, product, error_message) tuples
        processing_success = []  # List of (scene, product) tuples

        # Create log file path
        log_file = os.path.join(out_dir, f'processing_log_{datetime.now().strftime("%Y%m%d_%H%M%S")}.txt')

        for i,ddir in enumerate(tqdm(sorted(data_dirs), desc="Processing scenes", unit="scene")):
            print(f'\nWorking on: {os.path.basename(ddir)}')
        
            # parse metadata xml file for scene time
            metadata_xml = glob.glob(os.path.join(ddir, '*xml'))[0]
            tree = etree.parse(metadata_xml)
            root = tree.getroot()
            image_attrib = root.xpath('IMAGE_ATTRIBUTES')
            time = image_attrib[0].xpath('SCENE_CENTER_TIME')[0].text
            time_parts = time.split(':')
            hour = time_parts[0]
            minute = time_parts[1]
            second = str(round(float(time_parts[2][:-1])))
            time = hour+minute+second

            # gather other data attributes
            file_parts = os.path.basename(metadata_xml).split("_")
            sensor = file_parts[0]
            pthrw = file_parts[2]
            date = file_parts[3]
            sun_zen = getSolarZenithAngle(ddir)

            # check for two different band filenames
            # sometimes there is a 0 in the band name for some reason
            b1_file = glob.glob(ddir + '/' + '*_B1.TIF')
            if len(b1_file) == 1:
                b2_file = glob.glob(ddir + '/' + '*_B2.TIF')
                b3_file = glob.glob(ddir + '/' + '*_B3.TIF')
                b4_file = glob.glob(ddir + '/' + '*_B4.TIF')
                b5_file = glob.glob(ddir + '/' + '*_B5.TIF')
                b6_file = glob.glob(ddir + '/' + '*_B6.TIF')
                b7_file = glob.glob(ddir + '/' + '*_B7.TIF')
                b8_file = glob.glob(ddir + '/' + '*_B8.TIF')
            else:
                b1_file = glob.glob(ddir + '/' + '*_B01.TIF')
                b2_file = glob.glob(ddir + '/' + '*_B02.TIF')
                b3_file = glob.glob(ddir + '/' + '*_B03.TIF')
                b4_file = glob.glob(ddir + '/' + '*_B04.TIF')
                b5_file = glob.glob(ddir + '/' + '*_B05.TIF')
                b6_file = glob.glob(ddir + '/' + '*_B06.TIF')
                b7_file = glob.glob(ddir + '/' + '*_B07.TIF')
                b8_file = glob.glob(ddir + '/' + '*_B08.TIF')
        
            qa_file = glob.glob(ddir + '/' + '*_QA_PIXEL.TIF')
        
            # create date directory within the output directory
            out_date_dir = os.path.join(out_dir, date)
            if not os.path.isdir(out_date_dir):
                os.mkdir(out_date_dir)
        
            water_variants = ['we', 'waterextent']
            if next((True for p in products if p.lower() in water_variants), False) or args.mask:
                # check for cloud mask
                prod_dir = os.path.join(out_date_dir, 'cloudMask')
                prod_dirs.append(prod_dir)
                if not os.path.isdir(prod_dir):
                    os.mkdir(prod_dir)
                prod_name = os.path.join(prod_dir, f'{sensor}_cloudMask_{date}_{time}_{pthrw}.tif')

                # Check if final output file already exists
                final_name = get_final_filename(prod_name, args.event, args.tif_only)
                if os.path.exists(final_name) and not args.force:
                    print(f'\n* Cloud Mask already exists: {os.path.basename(final_name)}. Use \"-force\" to overwrite.')
                    cloudMask = final_name
                    processing_success.append((os.path.basename(ddir), 'Cloud Mask', 'Skipped - already exists'))
                else:
                    try:
                        # produce cloud mask
                        print('\n* Producing Cloud Mask')
                        cloudMask = gen_cloudMask(ddir, prod_name)

                        # Convert to COG (default) and optionally rename with event
                        # Skip COG conversion if merging - will convert merged file instead

                        if not args.tif_only and not args.merge:

                            cog_path = convert_to_cog(

                                prod_name,

                                nodata=args.nodata,

                                dst_crs=dst_crs_value,

                                compression=args.compression,

                                compression_level=args.compression_level

                            )

                            if args.event:

                                rename_with_event(cog_path, args.event)

                        elif args.event and not args.merge:
                            # Rename TIF without COG conversion
                            rename_with_event(prod_name, args.event)

                        processing_success.append((os.path.basename(ddir), 'Cloud Mask', 'Success'))
                    except Exception as e:
                        error_msg = f"{type(e).__name__}: {str(e)}"
                        processing_errors.append((os.path.basename(ddir), 'Cloud Mask', error_msg))
                        print(f'\n  ✗ ERROR processing Cloud Mask: {error_msg}')
                        print(f'  Continuing with next product...')
                        cloudMask = None  # Set to None so other products can continue

                if not args.mask:
                    cloudMask=None
            else:
                cloudMask = None

            true_variants = ['true', 'tc', 'truecolor']
            if next((True for p in products if p.lower() in true_variants), False):
                # check for true color image
                prod_dir = os.path.join(out_date_dir, 'trueColor')
                prod_dirs.append(prod_dir)
                if not os.path.isdir(prod_dir):
                    os.mkdir(prod_dir)
                prod_name = os.path.join(prod_dir, f'{sensor}_trueColor_{date}_{time}_{pthrw}.tif')

                # Check if final output file already exists
                final_name = get_final_filename(prod_name, args.event, args.tif_only)
                if os.path.exists(final_name) and not args.force:
                    print(f'\n* True color already exists: {os.path.basename(final_name)}. Use "-force" to overwrite.')
                    processing_success.append((os.path.basename(ddir), 'True Color', 'Skipped - already exists'))
                else:
                    try:
                        # produce true color image
                        print('\n* Processing true color')
                        genTrueColor(b4_file, b3_file, b2_file, qa_file, sun_zen, prod_name, cloudMask)

                        # Convert to COG (default) and optionally rename with event
                        if not args.tif_only:
                            cog_path = convert_to_cog(
                                prod_name,
                                nodata=args.nodata,
                                dst_crs=dst_crs_value,
                                compression=args.compression,
                                compression_level=args.compression_level
                            )
                            if args.event and not args.merge:
                                rename_with_event(cog_path, args.event)
                        elif args.event and not args.merge:
                            # Rename TIF without COG conversion
                            rename_with_event(prod_name, args.event)

                        processing_success.append((os.path.basename(ddir), 'True Color', 'Success'))
                    except Exception as e:
                        error_msg = f"{type(e).__name__}: {str(e)}"
                        processing_errors.append((os.path.basename(ddir), 'True Color', error_msg))
                        print(f'\n  ✗ ERROR processing True Color: {error_msg}')
                        print(f'  Continuing with next product...')

            pan_variants = ['pan', 'panchromatic']
            if next((True for p in products if p.lower() in pan_variants), False):
                # check for panchromatic band
                if not b8_file:
                    print('* Cannot process panchromatic: No panchromatic band (B8).')
                else:
                    # check for panchromatic image
                    prod_dir = os.path.join(out_date_dir, 'panchromatic')
                    prod_dirs.append(prod_dir)
                    if not os.path.isdir(prod_dir):
                        os.mkdir(prod_dir)
                    prod_name = os.path.join(prod_dir, f'{sensor}_panchromatic_{date}_{time}_{pthrw}.tif')

                    # Check if final output file already exists
                    final_name = get_final_filename(prod_name, args.event, args.tif_only)
                    if os.path.exists(final_name) and not args.force:
                        print(f'\n* Panchromatic already exists: {os.path.basename(final_name)}. Use "-force" to overwrite.')
                        processing_success.append((os.path.basename(ddir), 'Panchromatic', 'Skipped - already exists'))
                    else:
                        try:
                            print('\n* Processing panchromatic')
                            genPanchromatic(b8_file, sun_zen, prod_name, cloudMask)

                            # Convert to COG (default) and optionally rename with event
                            # Skip COG conversion if merging - will convert merged file instead
                            if not args.tif_only and not args.merge:
                                cog_path = convert_to_cog(
                                    prod_name,
                                    nodata=args.nodata,
                                    dst_crs=dst_crs_value,
                                    compression=args.compression,
                                    compression_level=args.compression_level
                                )
                                if args.event:
                                    rename_with_event(cog_path, args.event)
                            elif args.event and not args.merge:
                                # Rename TIF without COG conversion
                                rename_with_event(prod_name, args.event)

                            processing_success.append((os.path.basename(ddir), 'Panchromatic', 'Success'))
                        except Exception as e:
                            error_msg = f"{type(e).__name__}: {str(e)}"
                            processing_errors.append((os.path.basename(ddir), 'Panchromatic', error_msg))
                            print(f'\n  ✗ ERROR processing Panchromatic: {error_msg}')
                            print(f'  Continuing with next product...')
        
            nat_variants = ['nat', 'natural', 'naturalcolor', 'nc']
            if next((True for p in products if p.lower() in nat_variants), False):
                # check for natural color image
                prod_dir = os.path.join(out_date_dir, 'naturalColor')
                prod_dirs.append(prod_dir)
                if not os.path.isdir(prod_dir):
                    os.mkdir(prod_dir)
                prod_name = os.path.join(prod_dir, f'{sensor}_naturalColor_{date}_{time}_{pthrw}.tif')

                # Check if final output file already exists
                final_name = get_final_filename(prod_name, args.event, args.tif_only)
                if os.path.exists(final_name) and not args.force:
                    print(f'\n* Natural color already exists: {os.path.basename(final_name)}. Use "-force" to overwrite.')
                    processing_success.append((os.path.basename(ddir), 'Natural Color', 'Skipped - already exists'))
                else:
                    try:
                        print('\n* Processing natural color')
                        genNaturalColor(b6_file, b5_file, b4_file, qa_file, sun_zen, prod_name, cloudMask)

                        # Convert to COG (default) and optionally rename with event
                        # Skip COG conversion if merging - will convert merged file instead

                        if not args.tif_only and not args.merge:

                            cog_path = convert_to_cog(

                                prod_name,

                                nodata=args.nodata,

                                dst_crs=dst_crs_value,

                                compression=args.compression,

                                compression_level=args.compression_level

                            )

                            if args.event:

                                rename_with_event(cog_path, args.event)

                        elif args.event and not args.merge:

                            # Rename TIF without COG conversion

                            rename_with_event(prod_name, args.event)

                        processing_success.append((os.path.basename(ddir), 'Natural Color', 'Success'))
                    except Exception as e:
                        error_msg = f"{type(e).__name__}: {str(e)}"
                        processing_errors.append((os.path.basename(ddir), 'Natural Color', error_msg))
                        print(f'\n  ✗ ERROR processing Natural Color: {error_msg}')
                        print(f'  Continuing with next product...')
        
            cir_variants = ['cir', 'colorir', 'colorinfrared']
            if next((True for p in products if p.lower() in cir_variants), False):
                # check for color infrared image
                prod_dir = os.path.join(out_date_dir, 'colorInfrared')
                prod_dirs.append(prod_dir)
                if not os.path.isdir(prod_dir):
                    os.mkdir(prod_dir)
                prod_name = os.path.join(prod_dir, f'{sensor}_colorInfrared_{date}_{time}_{pthrw}.tif')

                # Check if final output file already exists
                final_name = get_final_filename(prod_name, args.event, args.tif_only)
                if os.path.exists(final_name) and not args.force:
                    print(f'\n* Color infrared already exists: {os.path.basename(final_name)}. Use "-force" to overwrite.')
                    processing_success.append((os.path.basename(ddir), 'Color Infrared', 'Skipped - already exists'))
                else:
                    try:
                        print('\n* Processing color infrared')
                        genColorInfrared(b5_file, b4_file, b3_file, qa_file, sun_zen, prod_name, cloudMask)

                        # Convert to COG (default) and optionally rename with event
                        # Skip COG conversion if merging - will convert merged file instead

                        if not args.tif_only and not args.merge:

                            cog_path = convert_to_cog(

                                prod_name,

                                nodata=args.nodata,

                                dst_crs=dst_crs_value,

                                compression=args.compression,

                                compression_level=args.compression_level

                            )

                            if args.event:

                                rename_with_event(cog_path, args.event)

                        elif args.event and not args.merge:

                            # Rename TIF without COG conversion

                            rename_with_event(prod_name, args.event)

                        processing_success.append((os.path.basename(ddir), 'Color Infrared', 'Success'))
                    except Exception as e:
                        error_msg = f"{type(e).__name__}: {str(e)}"
                        processing_errors.append((os.path.basename(ddir), 'Color Infrared', error_msg))
                        print(f'\n  ✗ ERROR processing Color Infrared: {error_msg}')
                        print(f'  Continuing with next product...')

            if next((True for p in products if p.lower() == 'ndvi'), False):
                # check for NDVI image
                prod_dir = os.path.join(out_date_dir, 'NDVI')
                prod_dirs.append(prod_dir)
                if not os.path.isdir(prod_dir):
                    os.mkdir(prod_dir)
                prod_name = os.path.join(prod_dir, f'{sensor}_NDVI_{date}_{time}_{pthrw}.tif')

                # Check if final output file already exists
                final_name = get_final_filename(prod_name, args.event, args.tif_only)
                if os.path.exists(final_name) and not args.force:
                    print(f'\n* NDVI already exists: {os.path.basename(final_name)}. Use "-force" to overwrite.')
                    processing_success.append((os.path.basename(ddir), 'NDVI', 'Skipped - already exists'))
                else:
                    try:
                        print('\n* Processing NDVI')
                        genNdvi(b5_file, b4_file, qa_file, sun_zen, prod_name, cloudMask)

                        # Convert to COG (default) and optionally rename with event
                        # Skip COG conversion if merging - will convert merged file instead

                        if not args.tif_only and not args.merge:

                            cog_path = convert_to_cog(

                                prod_name,

                                nodata=args.nodata,

                                dst_crs=dst_crs_value,

                                compression=args.compression,

                                compression_level=args.compression_level

                            )

                            if args.event:

                                rename_with_event(cog_path, args.event)

                        elif args.event and not args.merge:

                            # Rename TIF without COG conversion

                            rename_with_event(prod_name, args.event)

                        processing_success.append((os.path.basename(ddir), 'NDVI', 'Success'))
                    except Exception as e:
                        error_msg = f"{type(e).__name__}: {str(e)}"
                        processing_errors.append((os.path.basename(ddir), 'NDVI', error_msg))
                        print(f'\n  ✗ ERROR processing NDVI: {error_msg}')
                        print(f'  Continuing with next product...')

            if next((True for p in products if p.lower() == 'ndwi'), False):
                # check for NDWI image
                prod_dir = os.path.join(out_date_dir, 'NDWI')
                prod_dirs.append(prod_dir)
                if not os.path.isdir(prod_dir):
                    os.mkdir(prod_dir)
                prod_name = os.path.join(prod_dir, f'{sensor}_NDWI_{date}_{time}_{pthrw}.tif')

                # Check if final output file already exists
                final_name = get_final_filename(prod_name, args.event, args.tif_only)
                if os.path.exists(final_name) and not args.force:
                    print(f'\n* NDWI already exists: {os.path.basename(final_name)}. Use "-force" to overwrite.')
                    processing_success.append((os.path.basename(ddir), 'NDWI', 'Skipped - already exists'))
                else:
                    try:
                        print('\n* Processing NDWI')
                        genNdwi(b3_file, b5_file, qa_file, sun_zen, prod_name, cloudMask)

                        # Convert to COG (default) and optionally rename with event
                        # Skip COG conversion if merging - will convert merged file instead

                        if not args.tif_only and not args.merge:

                            cog_path = convert_to_cog(

                                prod_name,

                                nodata=args.nodata,

                                dst_crs=dst_crs_value,

                                compression=args.compression,

                                compression_level=args.compression_level

                            )

                            if args.event:

                                rename_with_event(cog_path, args.event)

                        elif args.event and not args.merge:

                            # Rename TIF without COG conversion

                            rename_with_event(prod_name, args.event)

                        processing_success.append((os.path.basename(ddir), 'NDWI', 'Success'))
                    except Exception as e:
                        error_msg = f"{type(e).__name__}: {str(e)}"
                        processing_errors.append((os.path.basename(ddir), 'NDWI', error_msg))
                        print(f'\n  ✗ ERROR processing NDWI: {error_msg}')
                        print(f'  Continuing with next product...')

            if next((True for p in products if p.lower() == 'mndwi'), False):
                # check for mNDWI image
                prod_dir = os.path.join(out_date_dir, 'MNDWI')
                prod_dirs.append(prod_dir)
                if not os.path.isdir(prod_dir):
                    os.mkdir(prod_dir)
                prod_name = os.path.join(prod_dir, f'{sensor}_MNDWI_{date}_{time}_{pthrw}.tif')

                # Check if final output file already exists
                final_name = get_final_filename(prod_name, args.event, args.tif_only)
                if os.path.exists(final_name) and not args.force:
                    print(f'\n* MNDWI already exists: {os.path.basename(final_name)}. Use "-force" to overwrite.')
                    processing_success.append((os.path.basename(ddir), 'MNDWI', 'Skipped - already exists'))
                else:
                    try:
                        print('\n* Processing MNDWI')
                        genmNdwi(b3_file, b6_file, qa_file, sun_zen, prod_name, cloudMask)

                        # Convert to COG (default) and optionally rename with event
                        # Skip COG conversion if merging - will convert merged file instead

                        if not args.tif_only and not args.merge:

                            cog_path = convert_to_cog(

                                prod_name,

                                nodata=args.nodata,

                                dst_crs=dst_crs_value,

                                compression=args.compression,

                                compression_level=args.compression_level

                            )

                            if args.event:

                                rename_with_event(cog_path, args.event)

                        elif args.event and not args.merge:

                            # Rename TIF without COG conversion

                            rename_with_event(prod_name, args.event)

                        processing_success.append((os.path.basename(ddir), 'MNDWI', 'Success'))
                    except Exception as e:
                        error_msg = f"{type(e).__name__}: {str(e)}"
                        processing_errors.append((os.path.basename(ddir), 'MNDWI', error_msg))
                        print(f'\n  ✗ ERROR processing MNDWI: {error_msg}')
                        print(f'  Continuing with next product...')

            if next((True for p in products if p.lower() == 'evi'), False):
                # check for EVI image
                prod_dir = os.path.join(out_date_dir, 'EVI')
                prod_dirs.append(prod_dir)
                if not os.path.isdir(prod_dir):
                    os.mkdir(prod_dir)
                prod_name = os.path.join(prod_dir, f'{sensor}_EVI_{date}_{time}_{pthrw}.tif')

                # Check if final output file already exists
                final_name = get_final_filename(prod_name, args.event, args.tif_only)
                if os.path.exists(final_name) and not args.force:
                    print(f'\n* EVI already exists: {os.path.basename(final_name)}. Use "-force" to overwrite.')
                    processing_success.append((os.path.basename(ddir), 'EVI', 'Skipped - already exists'))
                else:
                    try:
                        print('\n* Processing EVI')
                        genEvi(b5_file, b4_file, b2_file, qa_file, sun_zen, prod_name ,cloudMask)

                        # Convert to COG (default) and optionally rename with event
                        # Skip COG conversion if merging - will convert merged file instead

                        if not args.tif_only and not args.merge:

                            cog_path = convert_to_cog(

                                prod_name,

                                nodata=args.nodata,

                                dst_crs=dst_crs_value,

                                compression=args.compression,

                                compression_level=args.compression_level

                            )

                            if args.event:

                                rename_with_event(cog_path, args.event)

                        elif args.event and not args.merge:

                            # Rename TIF without COG conversion

                            rename_with_event(prod_name, args.event)

                        processing_success.append((os.path.basename(ddir), 'EVI', 'Success'))
                    except Exception as e:
                        error_msg = f"{type(e).__name__}: {str(e)}"
                        processing_errors.append((os.path.basename(ddir), 'EVI', error_msg))
                        print(f'\n  ✗ ERROR processing EVI: {error_msg}')
                        print(f'  Continuing with next product...')

            if next((True for p in products if p.lower() == 'nbr'), False):
                # check for NBR image
                prod_dir = os.path.join(out_date_dir, 'NBR')
                prod_dirs.append(prod_dir)
                if not os.path.isdir(prod_dir):
                    os.mkdir(prod_dir)
                prod_name = os.path.join(prod_dir, f'{sensor}_NBR_{date}_{time}_{pthrw}.tif')

                # Check if final output file already exists
                final_name = get_final_filename(prod_name, args.event, args.tif_only)
                if os.path.exists(final_name) and not args.force:
                    print(f'\n* NBR already exists: {os.path.basename(final_name)}. Use "-force" to overwrite.')
                    processing_success.append((os.path.basename(ddir), 'NBR', 'Skipped - already exists'))
                else:
                    try:
                        print('\n* Processing NBR')
                        genNbr(b5_file, b7_file, qa_file, sun_zen, prod_name, cloudMask)

                        # Convert to COG (default) and optionally rename with event
                        # Skip COG conversion if merging - will convert merged file instead

                        if not args.tif_only and not args.merge:

                            cog_path = convert_to_cog(

                                prod_name,

                                nodata=args.nodata,

                                dst_crs=dst_crs_value,

                                compression=args.compression,

                                compression_level=args.compression_level

                            )

                            if args.event:

                                rename_with_event(cog_path, args.event)

                        elif args.event and not args.merge:

                            # Rename TIF without COG conversion

                            rename_with_event(prod_name, args.event)

                        processing_success.append((os.path.basename(ddir), 'NBR', 'Success'))
                    except Exception as e:
                        error_msg = f"{type(e).__name__}: {str(e)}"
                        processing_errors.append((os.path.basename(ddir), 'NBR', error_msg))
                        print(f'\n  ✗ ERROR processing NBR: {error_msg}')
                        print(f'  Continuing with next product...')

            # update permissions
            cmd = '/bin/chmod -R -f ug+rwx '+ddir
            os.system(cmd) 
            print(f'\nProgress: {i+1}/{num_dirs}')

        if next((True for p in products if p.lower() in water_variants), False):
            # gather all of the date (parent) directories of the processed data directories
            date_dirs = set([Path(ddir).parent for ddir in data_dirs])

            # a water extent is produced for each date
            for date_dir in date_dirs:
                # create water extent directory
                date = os.path.basename(date_dir)
                prod_dir = os.path.join(out_dir, date, 'waterExtent')
                if not os.path.isdir(prod_dir):
                    os.mkdir(prod_dir)
                
                # check for merged cloud mask
                cloud_dir = os.path.join(out_dir, date, 'cloudMask')
                cloudMask_check = glob.glob(os.path.join(cloud_dir,'*merged.tif'))
                if not cloudMask_check:
                    # merge all cloud masks for the given date
                    print('\n* Merging Cloud Masks.')
                    cloudMask = ls_merge(cloud_dir, False)
                else:
                    cloudMask = cloudMask_check[0]
            
                # a water extent can be produced for several inputted numbers of standard deviation
                for nstd in args.we_nstd:
                    # format output filename
                    nstd_str = str(nstd).replace('.', '_')
                    prod_name = os.path.join(prod_dir, f'{sensor}_waterExtent_NSTD_{nstd_str}_{date}.tif')
                    check = glob.glob(prod_name)
                    if len(check) == 0 or args.force:
                        try:
                            # produce water extent
                            print(f'\n* Processing Water Extent (NSTD: {nstd})')
                            gen_water_extent(date_dir, float(nstd), prod_name, cloudMask, sun_zen)

                            # Convert to COG (default) and optionally rename with event
                            # Skip COG conversion if merging - will convert merged file instead

                            if not args.tif_only and not args.merge:

                                cog_path = convert_to_cog(

                                    prod_name,

                                    nodata=args.nodata,

                                    dst_crs=dst_crs_value,

                                    compression=args.compression,

                                    compression_level=args.compression_level

                                )

                                if args.event:

                                    rename_with_event(cog_path, args.event)

                            elif args.event and not args.merge:

                                # Rename TIF without COG conversion

                                rename_with_event(prod_name, args.event)

                            processing_success.append((date, f'Water Extent (NSTD: {nstd})', 'Success'))
                        except Exception as e:
                            error_msg = f"{type(e).__name__}: {str(e)}"
                            processing_errors.append((date, f'Water Extent (NSTD: {nstd})', error_msg))
                            print(f'\n  ✗ ERROR processing Water Extent (NSTD: {nstd}): {error_msg}')
                            print(f'  Continuing with next product...')
                    else:
                        processing_success.append((date, f'Water Extent (NSTD: {nstd})', 'Skipped - already exists'))

        if args.merge:
            dirs_to_merge = list(set(prod_dirs))
            cm_dirs = [prod_dir for prod_dir in dirs_to_merge if 'cloud' in prod_dir]
            for cm_dir in cm_dirs:
                # merge cloud masks separately so that they are not masked themselves
                # need to merge cloud masks first b/c this mask can be used
                # to mask the merged product below
                print('Merging:', cm_dir)
                dirs_to_merge.remove(cm_dir)
                merged_file = ls_merge(cm_dir, mask=False)

                # Convert merged file to COG (default) and optionally rename with event
                if not args.tif_only:
                    cog_path = convert_to_cog(
                        merged_file,
                        nodata=args.nodata,
                        dst_crs=dst_crs_value,
                        compression=args.compression,
                        compression_level=args.compression_level
                    )
                    if args.event:
                        rename_with_event(cog_path, args.event)
                elif args.event:
                    # Rename TIF without COG conversion
                    rename_with_event(merged_file, args.event)

                # Also rename individual files with event (if event name provided)
                if args.event:
                    individual_files = glob.glob(os.path.join(prod_dir, '*.tif'))
                    for indiv_file in individual_files:
                        # Skip files that are already renamed or are merged files
                        basename = os.path.basename(indiv_file)
                        if 'merged' not in basename and not basename.startswith(args.event):
                            try:
                                rename_with_event(indiv_file, args.event)
                            except Exception as e:
                                print(f"  Warning: Could not rename {basename}: {e}")

            for prod_dir in dirs_to_merge:
                # merge products of the same date
                print('Merging:', prod_dir)
                merged_file = ls_merge(prod_dir, args.mask)

                # Convert merged file to COG (default) and optionally rename with event
                if not args.tif_only:
                    cog_path = convert_to_cog(
                        merged_file,
                        nodata=args.nodata,
                        dst_crs=dst_crs_value,
                        compression=args.compression,
                        compression_level=args.compression_level
                    )
                    if args.event:
                        rename_with_event(cog_path, args.event)
                elif args.event:
                    # Rename TIF without COG conversion
                    rename_with_event(merged_file, args.event)

                # Also rename individual files with event (if event name provided)
                if args.event:
                    individual_files = glob.glob(os.path.join(prod_dir, '*.tif'))
                    for indiv_file in individual_files:
                        # Skip files that are already renamed or are merged files
                        basename = os.path.basename(indiv_file)
                        if 'merged' not in basename and not basename.startswith(args.event):
                            try:
                                rename_with_event(indiv_file, args.event)
                            except Exception as e:
                                print(f"  Warning: Could not rename {basename}: {e}")

    if not args.unzip_only:
        # Write processing summary and log file
        total_processed = len(processing_success) + len(processing_errors)

        print("\n" + "="*70)
        print("PROCESSING SUMMARY")
        print("="*70)
        print(f"Total products processed: {total_processed}")
        print(f"✓ Successful: {len(processing_success)}")
        print(f"✗ Failed: {len(processing_errors)}")

        if processing_errors:
            print("\n" + "="*70)
            print("ERRORS ENCOUNTERED:")
            print("="*70)
            for scene, product, error in processing_errors:
                print(f"  ✗ {scene} - {product}")
                print(f"     Error: {error}")

        # Write detailed log file
        with open(log_file, 'w') as f:
            f.write("LANDSAT 8/9 PROCESSING LOG\n")
            f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("="*70 + "\n\n")

            f.write(f"Total products processed: {total_processed}\n")
            f.write(f"Successful: {len(processing_success)}\n")
            f.write(f"Failed: {len(processing_errors)}\n\n")

            f.write("="*70 + "\n")
            f.write("SUCCESSFUL PROCESSING:\n")
            f.write("="*70 + "\n")
            for scene, product, status in processing_success:
                f.write(f"✓ {scene} - {product}: {status}\n")

            if processing_errors:
                f.write("\n" + "="*70 + "\n")
                f.write("FAILED PROCESSING:\n")
                f.write("="*70 + "\n")
                for scene, product, error in processing_errors:
                    f.write(f"✗ {scene} - {product}\n")
                    f.write(f"   Error: {error}\n\n")

        print(f"\n📄 Detailed log written to: {log_file}")
        print("="*70)

    print("\nCompleted Landsat surface reflectance processing and product generation.")

    if not args.unzip_only and processing_errors:
        print(f"\n⚠ Processing completed with {len(processing_errors)} error(s). See log file for details.")
    elif not args.unzip_only:
        print("\n✓ Processing completed successfully!")

    cmd = f"chmod -R -f ug+rwx {input_dir}"
    os.system(cmd)

    now = datetime.now()

    print('\nProcessing Time (s): ', (now-then).total_seconds())
    print('Processing Time (m): ', (now-then)/60.)
