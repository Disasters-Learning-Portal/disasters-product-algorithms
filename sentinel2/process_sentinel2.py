"""
process_sentinel2.py

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
from sentinel.sentinel2_functions import *
from shared_utils.cog_utils import convert_to_cog, rename_with_event, get_final_filename

then = datetime.now()

parser=argparse.ArgumentParser(
        description='''This script unzips and processes Sentinel-2 L2A data\
                     and produces true color, natural color, color infrared\
                     NDWI, MNDWI, NDVI, NBR, or Water Extent.''',
        usage='process_sentinel2.py input [-h] [-p [P ...]] [-we_nstd [WE_NSTD ...]] [-date [DATE ...]] [-tile [TILE ...]] [-merge] [-mask] [-force] [-unzip_only] [-tif_only] [-nodata NODATA] [-compression COMPRESSION] [-compression_level LEVEL] [-event EVENT]')
parser.add_argument('input', nargs=1, help= 'Path to directory containing the .zip files (e.g.\
                    /data/esops/eventData/2023/TurkeyEarthquake/sentinel2).')
parser.add_argument('-p', nargs='*', default=['true'], help='List of products to produce (all = everything, true = true color,\
                     nat = natural, colorIR = color infrared, swir = shortwave infrared, ndwi = NDWI, mndwi = MNDWI, ndvi = NDVI, \
                    nbr = NBR, we = water extent')
parser.add_argument('-we_nstd', nargs='*', default=[1], help='Produce water extents with given number(s) of standard deviations (default is 1).\
                    More standard deviations = higher NIR threshold = more pixels classified as water.')
parser.add_argument('-date', nargs='*', default = False, help='Date(s) (e.g. 20230116). If no date(s) or tile(s) is entered,\
                    all dates found will be processed.')
parser.add_argument('-tile', nargs ='*', default=False, help='Identifier (e.g. T36SYD) for specific tile(s),\
                    If no tile(s) or date(s) is entered, all tiles found will be processed.')
parser.add_argument('-merge', default=False, action='store_true', help='Merge all images by date and product.')
parser.add_argument('-mask', default=False, action='store_true', help='Generate cloud mask and mask all images (non-masked version \
                    preserved as well).')
parser.add_argument('-force', default=False, action="store_true", help='Force overwrite existing products.')
parser.add_argument('-unzip_only', default=False, action="store_true", help='Just unzip all .zip files. Do not process.')
parser.add_argument('-tif_only', default=False, action="store_true", help='Skip COG conversion and keep regular GeoTIFF format (COG is default).')
parser.add_argument('-nodata', type=float, default=None, help='No-data value for COG outputs (auto-detected if not specified).')
parser.add_argument('-compression', type=str, default='ZSTD', help='Compression type for COG (default: ZSTD).')
parser.add_argument('-compression_level', type=int, default=22, help='Compression level for COG (default: 22 for ZSTD).')
parser.add_argument('-event', type=str, default=None, help='Event name for filename prefix (e.g., 202512_Flood_WA). Adds formatted date suffix.')
args=parser.parse_args()

print('\nInput:', args.input[0])
input_dir = args.input[0]

if not args.unzip_only:
  if 'all' in args.p:
    products = ['true', 'nat', 'swir', 'colorIR', 'ndwi', 'mndwi', 'ndvi', 'nbr', 'we']
  else:
    # check for valid product types
    products = args.p
    product_variants = ['true', 'tc', 'truecolor', 'colorir', 'cir', 'colorinfrared', 'nat', 'natural', 'naturalcolor',\
                        'swir', 'shortwaveir', 'shortwaveinfrared', 'ndwi', 'mndwi', 'ndvi', 'nbr', 'we', 'waterextent']
    invalid_products = [p for p in products if p.lower() not in product_variants]
    if invalid_products:
        print('Invalid product type(s): ', invalid_products)
        print('Please enter a product from the following list (not case sensitive): true (tc, truecolor),\
                nat (natural, naturalcolor), swir (shortwaveir, shortwaveinfrared), colorIR (cir, colorinfrared),\
                ndwi, mndwi, ndvi, nbr.')
        quit()
  print('Product(s) to produce:', products)

  # display and store inputted dates
  if args.date:
    print('Processing date(s):', args.date)
    dates = args.date
  else:
    print('Processing all dates')
  
  # display and store inputted tiles
  if args.tile:
    tiles = args.tile
    print('Processing tile(s):', tiles)
  else:
    print('Processing all tiles')

# gather .zip files
zips = glob.glob(os.path.join(input_dir, '*.zip'))
unpacked_dir = os.path.join(input_dir, 'unpacked')
if zips:
    # create unpacked directory
    print('\nNumber of .zip files:', len(zips))
    if not os.path.isdir(unpacked_dir):
        os.mkdir(unpacked_dir)
else:
    print('No .zip files found. Looking for .SAFE directories in: ', unpacked_dir)
    if not unpacked_dir:
        print(f'{unpacked_dir} does not exist! Check input path.')
        quit()
    else:
        # checking for .SAFE files
        safe_dirs_l1 = glob.glob(os.path.join(unpacked_dir, '*/*SAFE'))
        safe_dirs_l2 = glob.glob(os.path.join(unpacked_dir, '*/*/*SAFE'))
        safe_dirs = safe_dirs_l1 + safe_dirs_l2
        if safe_dirs:
           print('.SAFE directories found!')
        else:
           print('No .SAFE directories found!')
           quit()

if zips:
    for zip_file in zips:
        zip_base = os.path.basename(zip_file)
        zip_base_parts = zip_base.split('_')

        # files from Copernicus browser/download script
        if zip_base[0:2] == 'S2':
            # format output .SAFE file
            date = zip_base_parts[2][0:8]
            tile = zip_base_parts[5]
            safe = os.path.join(unpacked_dir, date, zip_base.replace('.SAFE.zip', '.SAFE'))

            if os.path.isdir(safe):
                # check for existing .SAFE file
                print(f'\t{zip_base} already unzipped.')
            else:
                # make date directory
                date_dir = os.path.join(unpacked_dir, date)
                if not os.path.isdir(date_dir):
                    os.mkdir(date_dir)
                
                # unzip
                print("\nUnzipping:", zip_base)
                cmd = f'7z x {zip_file} -o{date_dir}'
                os.system(cmd)
        
        # files from HDDS
        elif zip_base[0:2] == 'SN':
            # format output filename
            outname = zip_base.replace('.zip', '')
            check = glob.glob(os.path.join(unpacked_dir, '*', outname))
            
            if check:
               # check if already unzipped
               print(f'\t{zip_base} already unzipped.')
            else:
               # unzip file to a temporary directory
               temp = os.path.join(unpacked_dir, outname)
               print('\nUnzipping:', zip_base)
               cmd = f'7z x {zip_file} -o{temp}'
               os.system(cmd)

               # find the .SAFE directory within the temporary directory
               safe = glob.glob(os.path.join(temp, '*.SAFE'))
               
               if not safe:
                  print('Data missing! No .SAFE directory found.')
                  continue
               else:
                  # create date directory
                  date = os.path.basename(safe[0]).split('_')[2][0:8]
                  date_dir = os.path.join(unpacked_dir, date)
                  if not os.path.isdir(date_dir):
                      os.mkdir(date_dir)

                  # move temporary directory to the date directory
                  shutil.move(temp, date_dir)
        else:
           print('Filename format unrecognized. Expecting S2*.SAFE.zip or SN*.zip.')
                  
# Gather directories mathcing inputted date and/or tile
if not args.unzip_only:
  prod_dirs = []    # store product directory paths; used for merging later
  data_dirs = []    # store data directory paths; used for processing
  if args.date and args.tile:
    for date in dates:
      for tile in tiles:
        try:
          data_dir = glob.glob(os.path.join(unpacked_dir, date, f'*{date}_{tile}'))[0]
        except:
          data_dir = glob.glob(os.path.join(unpacked_dir, date, f'*/*{date}_{tile}'))[0]
        finally:
          data_dirs.append(data_dir)
  elif args.date:
    for date in dates:
      data_dir1 = glob.glob(os.path.join(unpacked_dir, date, f'*{date}*SAFE'))
      data_dir2 = glob.glob(os.path.join(unpacked_dir, date, f'*/*{date}*SAFE'))
      data_dirs = data_dirs + data_dir1 + data_dir2
  elif args.tile:
    for tile in tiles:
      data_dir1 = glob.glob(os.path.join(unpacked_dir, '*' , f'*{tile}*SAFE'))
      data_dir2 = glob.glob(os.path.join(unpacked_dir, '*' , f'*/*{tile}*SAFE'))
      data_dirs = data_dirs + data_dir1 + data_dir2
  else:
    data_dirs1 = glob.glob(os.path.join(unpacked_dir, '*', 'S2*SAFE'))
    data_dirs2 = glob.glob(os.path.join(unpacked_dir, '*', 'SN*/S2*SAFE'))
    data_dirs = data_dirs1 + data_dirs2

else:
   quit()

# check for data directories
num_dirs = len(data_dirs)
if num_dirs == 0:
  print('No directories matching inputted date(s) and/or tile(s)')
else:
  # create output directory
  out_dir = os.path.join(input_dir, 'output')
  if not os.path.isdir(out_dir):
      os.mkdir(out_dir)

  print('\nNumber of directories to process:', num_dirs)
  for i,ddir in enumerate(data_dirs):
    print('\nWorking on:', os.path.basename(ddir))

    # parse filename for metadata
    ddir_parts = os.path.basename(ddir).split('_')
    date_time = ddir_parts[2].split('T')
    date = date_time[0]
    time = date_time[1]
    tile = ddir_parts[5]
    sat = ddir_parts[0]
    level = ddir_parts[1]
    if level == 'MSIL1C':
       # will perform Rayleigh correction for true color image
       ray = True
    else:
       ray = False

    # create date directory within the output directory
    out_date_dir = os.path.join(out_dir, date)
    if not os.path.isdir(out_date_dir):
        os.mkdir(out_date_dir)

    water_variants = ['we', 'waterextent']
    if next((True for p in products if p.lower() in water_variants), False) or args.mask:
      if level != 'MSIL2A':
         print('\n* Cloud Mask can only be produced from Level-2 data!')
         cloudMask = None
      else:
        # check for cloud mask
        prod_dir = os.path.join(out_date_dir, 'cloudMask')
        prod_dirs.append(prod_dir)
        if not os.path.isdir(prod_dir):
            os.mkdir(prod_dir)
        prod_name = os.path.join(prod_dir, f'{sat}_cloudMask_{date}_{time}_{tile}.tif')

        # Check if final output file already exists
        final_name = get_final_filename(prod_name, args.event, args.tif_only)
        if os.path.exists(final_name) and not args.force:
            print(f'\n* Cloud Mask already exists: {os.path.basename(final_name)}. Use "-force" to overwrite.')
            cloudMask = final_name
        else:
            # produce cloud mask
            print('\n* Producing Cloud Mask')
            cloudMask = gen_cloudMask(ddir, prod_name, level)

            # Convert to COG (default) and optionally rename with event
            if not args.tif_only:
                cog_path = convert_to_cog(
                    prod_name,
                    nodata=args.nodata,
                    compression=args.compression,
                    compression_level=args.compression_level
                )
                if args.event and not args.merge:
                    rename_with_event(cog_path, args.event)
            elif args.event and not args.merge:
                # Rename TIF without COG conversion
                rename_with_event(prod_name, args.event)
    else:
       cloudMask = None

    true_variants = ['true','tc', 'truecolor'] 
    if next((True for p in products if p.lower() in true_variants), False):
      # check for true color image
      prod_dir = os.path.join(out_date_dir, 'trueColor')
      prod_dirs.append(prod_dir)
      if not os.path.isdir(prod_dir):
          os.mkdir(prod_dir)
      prod_name = os.path.join(prod_dir, f'{sat}_{level}_trueColor_{date}_{time}_{tile}.tif')

      # Check if final output file already exists
      final_name = get_final_filename(prod_name, args.event, args.tif_only)
      if os.path.exists(final_name) and not args.force:
        print(f'\n* True color already exists: {os.path.basename(final_name)}. Use "-force" to overwrite.')
      else:
        # produce true color image
        print('\n* Processing true color')
        gen_true_color(ddir, prod_name, level, cloudMask, ray)

        # Convert to COG (default) and optionally rename with event
        if not args.tif_only:
            cog_path = convert_to_cog(
                prod_name,
                nodata=args.nodata,
                compression=args.compression,
                compression_level=args.compression_level
            )
            if args.event and not args.merge:
                rename_with_event(cog_path, args.event)
        elif args.event and not args.merge:
            # Rename TIF without COG conversion
            rename_with_event(prod_name, args.event)
    
    nat_variants = ['nat', 'natural', 'naturalcolor']
    if next((True for p in products if p.lower() in nat_variants), False):
      # check for natural color image
      prod_dir = os.path.join(out_date_dir, 'naturalColor')
      prod_dirs.append(prod_dir)
      if not os.path.isdir(prod_dir):
          os.mkdir(prod_dir)
      prod_name = os.path.join(prod_dir, f'{sat}_{level}_naturalColor_{date}_{time}_{tile}.tif')

      # Check if final output file already exists
      final_name = get_final_filename(prod_name, args.event, args.tif_only)
      if os.path.exists(final_name) and not args.force:
        print(f'\n* Natural color already exists: {os.path.basename(final_name)}. Use "-force" to overwrite.')
      else:
        # produce natural color image
        print('\n* Processing natural color')
        gen_natural_color(ddir, prod_name, level, cloudMask, ray)

        # Convert to COG (default) and optionally rename with event
        if not args.tif_only:
            cog_path = convert_to_cog(
                prod_name,
                nodata=args.nodata,
                compression=args.compression,
                compression_level=args.compression_level
            )
            if args.event and not args.merge:
                rename_with_event(cog_path, args.event)
        elif args.event and not args.merge:
            # Rename TIF without COG conversion
            rename_with_event(prod_name, args.event)
    
    swir_variants = ['swir', 'shortwaveir', 'shortwaveinfrared']
    if next((True for p in products if p.lower() in swir_variants), False):
      # check for SWIR image
      prod_dir = os.path.join(out_date_dir, 'shortwaveInfrared')
      prod_dirs.append(prod_dir)
      if not os.path.isdir(prod_dir):
          os.mkdir(prod_dir)
      prod_name = os.path.join(prod_dir, f'{sat}_{level}_shortwaveInfrared_{date}_{time}_{tile}.tif')

      # Check if final output file already exists
      final_name = get_final_filename(prod_name, args.event, args.tif_only)
      if os.path.exists(final_name) and not args.force:
        print(f'\n* Short wave infrared already exists: {os.path.basename(final_name)}. Use "-force" to overwrite.')
      else:
        # produce SWIR image
        print('\n* Porcessing short wave infrared')
        gen_swir(ddir, prod_name, level, cloudMask, ray)

        # Convert to COG (default) and optionally rename with event
        if not args.tif_only:
            cog_path = convert_to_cog(
                prod_name,
                nodata=args.nodata,
                compression=args.compression,
                compression_level=args.compression_level
            )
            if args.event and not args.merge:
                rename_with_event(cog_path, args.event)
        elif args.event and not args.merge:
            # Rename TIF without COG conversion
            rename_with_event(prod_name, args.event)
    
    cir_variants = ['cir', 'colorir', 'colorinfrared']
    if next((True for p in products if p.lower() in cir_variants), False):
      # check for color infrared image
      prod_dir = os.path.join(out_date_dir, 'colorInfrared')
      prod_dirs.append(prod_dir)
      if not os.path.isdir(prod_dir):
          os.mkdir(prod_dir)
      prod_name = os.path.join(prod_dir, f'{sat}_{level}_colorInfrared_{date}_{time}_{tile}.tif')

      # Check if final output file already exists
      final_name = get_final_filename(prod_name, args.event, args.tif_only)
      if os.path.exists(final_name) and not args.force:
        print(f'\n* Color infrared already exists: {os.path.basename(final_name)}. Use "-force" to overwrite.')
      else:
        # produce color infrared image
        print('\n* Processing color infrared')
        gen_color_infrared(ddir, prod_name, level, cloudMask, ray)

        # Convert to COG (default) and optionally rename with event
        if not args.tif_only:
            cog_path = convert_to_cog(
                prod_name,
                nodata=args.nodata,
                compression=args.compression,
                compression_level=args.compression_level
            )
            if args.event and not args.merge:
                rename_with_event(cog_path, args.event)
        elif args.event and not args.merge:
            # Rename TIF without COG conversion
            rename_with_event(prod_name, args.event)
    
    if next((True for p in products if p.lower() == 'ndwi'), False):
      # check for NDWI
      prod_dir = os.path.join(out_date_dir, 'NDWI')
      prod_dirs.append(prod_dir)
      if not os.path.isdir(prod_dir):
          os.mkdir(prod_dir)
      prod_name = os.path.join(prod_dir, f'{sat}_{level}_NDWI_{date}_{time}_{tile}.tif')

      # Check if final output file already exists
      final_name = get_final_filename(prod_name, args.event, args.tif_only)
      if os.path.exists(final_name) and not args.force:
        print(f'\n* NDWI already exists: {os.path.basename(final_name)}. Use "-force" to overwrite.')
      else:
        # produce NDWI image
        print('\n* Processing NDWI')
        gen_ndwi(ddir, prod_name, level, cloudMask, ray)

        # Convert to COG (default) and optionally rename with event
        if not args.tif_only:
            cog_path = convert_to_cog(
                prod_name,
                nodata=args.nodata,
                compression=args.compression,
                compression_level=args.compression_level
            )
            if args.event and not args.merge:
                rename_with_event(cog_path, args.event)
        elif args.event and not args.merge:
            # Rename TIF without COG conversion
            rename_with_event(prod_name, args.event)
    
    if next((True for p in products if p.lower() == 'mndwi'), False):
      # check for mNDWI image
      prod_dir = os.path.join(out_date_dir, 'MNDWI')
      prod_dirs.append(prod_dir)
      if not os.path.isdir(prod_dir):
          os.mkdir(prod_dir)
      prod_name = os.path.join(prod_dir, f'{sat}_{level}_MNDWI_{date}_{time}_{tile}.tif')

      # Check if final output file already exists
      final_name = get_final_filename(prod_name, args.event, args.tif_only)
      if os.path.exists(final_name) and not args.force:
        print(f'\n* MNDWI already exists: {os.path.basename(final_name)}. Use "-force" to overwrite.')
      else:
        # produce mNDWI image
        print('\n* Processing MNDWI')
        gen_mndwi(ddir, prod_name, level, cloudMask, ray)

        # Convert to COG (default) and optionally rename with event
        if not args.tif_only:
            cog_path = convert_to_cog(
                prod_name,
                nodata=args.nodata,
                compression=args.compression,
                compression_level=args.compression_level
            )
            if args.event and not args.merge:
                rename_with_event(cog_path, args.event)
        elif args.event and not args.merge:
            # Rename TIF without COG conversion
            rename_with_event(prod_name, args.event)
    
    if next((True for p in products if p.lower() == 'ndvi'), False):
      # check for NDVI
      prod_dir = os.path.join(out_date_dir, 'NDVI')
      prod_dirs.append(prod_dir)
      if not os.path.isdir(prod_dir):
          os.mkdir(prod_dir)
      prod_name = os.path.join(prod_dir, f'{sat}_{level}_NDVI_{date}_{time}_{tile}.tif')

      # Check if final output file already exists
      final_name = get_final_filename(prod_name, args.event, args.tif_only)
      if os.path.exists(final_name) and not args.force:
        print(f'\n* NDVI already exists: {os.path.basename(final_name)}. Use "-force" to overwrite.')
      else:
        # produce for NDVI image
        print('\n* Processing NDVI')
        gen_ndvi(ddir, prod_name, level, cloudMask, ray)

        # Convert to COG (default) and optionally rename with event
        if not args.tif_only:
            cog_path = convert_to_cog(
                prod_name,
                nodata=args.nodata,
                compression=args.compression,
                compression_level=args.compression_level
            )
            if args.event and not args.merge:
                rename_with_event(cog_path, args.event)
        elif args.event and not args.merge:
            # Rename TIF without COG conversion
            rename_with_event(prod_name, args.event)
    
    if next((True for p in products if p.lower() == 'nbr'), False):
      # check for NBR image
      prod_dir = os.path.join(out_date_dir, 'NBR')
      prod_dirs.append(prod_dir)
      if not os.path.isdir(prod_dir):
          os.mkdir(prod_dir)
      prod_name = os.path.join(prod_dir, f'{sat}_{level}_NBR_{date}_{time}_{tile}.tif')

      # Check if final output file already exists
      final_name = get_final_filename(prod_name, args.event, args.tif_only)
      if os.path.exists(final_name) and not args.force:
        print(f'\n* NBR already exists: {os.path.basename(final_name)}. Use "-force" to overwrite.')
      else:
        # produce NBR image
        print('\n* Processing NBR')
        gen_nbr(ddir, prod_name, level, cloudMask)

        # Convert to COG (default) and optionally rename with event
        if not args.tif_only:
            cog_path = convert_to_cog(
                prod_name,
                nodata=args.nodata,
                compression=args.compression,
                compression_level=args.compression_level
            )
            if args.event and not args.merge:
                rename_with_event(cog_path, args.event)
        elif args.event and not args.merge:
            # Rename TIF without COG conversion
            rename_with_event(prod_name, args.event)

    # update permissions
    print(f'\nProgress: {i+1}/{num_dirs}')
    cmd = '/bin/chmod -f -R ug+rwx '+ out_dir
    os.system(cmd)

  if next((True for p in products if p.lower() in water_variants), False):
    # compile unique date directories
    date_dirs = set([Path(ddir).parent for ddir in data_dirs if 'MSIL2A' in ddir])

    # a water extent is produced for each date
    for date_dir in date_dirs:
      date = os.path.basename(date_dir)

      # create water extent directory
      prod_dir = os.path.join(out_dir, date, 'waterExtent')
      if not os.path.isdir(prod_dir):
          os.mkdir(prod_dir)
      
      # check for merged cloud mask
      cloud_dir = os.path.join(out_dir, date, 'cloudMask')
      cloudMask_check = glob.glob(os.path.join(cloud_dir,'*merged.tif'))
      if not cloudMask_check:
         # merge all cloud masks for a given daye
         print('\n* Merging Cloud Masks.')
         cloudMask = s2_merge(cloud_dir, False)
      else:
         cloudMask = cloudMask_check[0]
      
      # a water extent can be produced for several inputted numbers of standard deviation
      for nstd in args.we_nstd:
          # format output filename
          nstd_str = str(nstd).replace('.', '_')
          prod_name = os.path.join(prod_dir, f'{sat}_waterExtent_NSTD_{nstd_str}_{date}.tif')

          # Check if final output file already exists
          final_name = get_final_filename(prod_name, args.event, args.tif_only)
          if os.path.exists(final_name) and not args.force:
            print(f'\n* Water Extent already exists: {os.path.basename(final_name)}. Use "-force" to overwrite.')
          else:
            # produce water extent
            print(f'\n* Processing Water Extent (NSTD: {nstd})')
            gen_water_extent(date_dir, float(nstd), prod_name, cloudMask)

            # Convert to COG (default) and optionally rename with event
            if not args.tif_only:
                cog_path = convert_to_cog(
                    prod_name,
                    nodata=args.nodata,
                    compression=args.compression,
                    compression_level=args.compression_level
                )
                if args.event:
                    rename_with_event(cog_path, args.event)
            elif args.event:
                # Rename TIF without COG conversion
                rename_with_event(prod_name, args.event)

  if args.merge:
     print('\n')
     dirs_to_merge = list(set(prod_dirs))
     cm_dirs = [prod_dir for prod_dir in dirs_to_merge if 'cloud' in prod_dir]
     for cm_dir in cm_dirs:
        # merge cloud masks separately so that they are not masked themselves
        # need to merge cloud masks first b/c this mask can be used
        # to mask the merged product below
        print('Merging:', cm_dir)
        dirs_to_merge.remove(cm_dir)
        s2_merge(cm_dir, mask=False)
     for prod_dir in dirs_to_merge:
        # merge products of the same date
        print('Merging:', prod_dir)
        s2_merge(prod_dir, args.mask)

  print("\nCompleted Sentinel-2 processing and product generation\n")

# update permissions
cmd = f"chmod -R -f ug+rwx {input_dir}"
os.system(cmd)

now = datetime.now()
print('Processing Time (s): ', (now-then).total_seconds())
print('Processing Time (m): ', (now-then)/60.)
