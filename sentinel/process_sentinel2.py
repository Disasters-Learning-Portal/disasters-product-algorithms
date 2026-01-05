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
from sentinel.sentinel2_functions import *

then = datetime.now()

parser=argparse.ArgumentParser(
        description='''This script unzips and processes Sentinel-2 L2A data\
                     and produces true color, natural color, color infrared\
                     NDWI, MNDWI, NDVI, NBR, or Water Extent.''',
        usage='process_sentinel2.py input [-h] [-p [P ...]] [-we_nstd [WE_NSTD ...]] [-date [DATE ...]] [-tile [TILE ...]] [-merge] [-mask] [-force] [-unzip_only]')
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
        check = glob.glob(prod_name)

        # produce cloud mask
        if len(check) == 0 or args.force:
          print('\n* Producing Cloud Mask')
          cloudMask = gen_cloudMask(ddir, prod_name, level)
        else:
          cloudMask = prod_name
          print('\n* Cloud Mask already produced. Use -force in your command to overwrite.')
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
      check = glob.glob(prod_name)

      # produce true color image
      if len(check) == 0 or args.force:
        print('\n* Processing true color')
        gen_true_color(ddir, prod_name, level, cloudMask, ray)
      else:
        print('\n* True color already processed. Use -force in your command to overwrite.')
    
    nat_variants = ['nat', 'natural', 'naturalcolor']
    if next((True for p in products if p.lower() in nat_variants), False):
      # check for natural color image
      prod_dir = os.path.join(out_date_dir, 'naturalColor')
      prod_dirs.append(prod_dir)
      if not os.path.isdir(prod_dir):
          os.mkdir(prod_dir)
      prod_name = os.path.join(prod_dir, f'{sat}_{level}_naturalColor_{date}_{time}_{tile}.tif')
      check = glob.glob(prod_name)

      # produce natural color image
      if len(check) == 0 or args.force:
        print('\n* Processing natural color')
        gen_natural_color(ddir, prod_name, level, cloudMask, ray)
      else:
        print('\n* Natural color already processed. Use -force in your command to overwrite.')
    
    swir_variants = ['swir', 'shortwaveir', 'shortwaveinfrared']
    if next((True for p in products if p.lower() in swir_variants), False):
      # check for SWIR image
      prod_dir = os.path.join(out_date_dir, 'shortwaveInfrared')
      prod_dirs.append(prod_dir)
      if not os.path.isdir(prod_dir):
          os.mkdir(prod_dir)
      prod_name = os.path.join(prod_dir, f'{sat}_{level}_shortwaveInfrared_{date}_{time}_{tile}.tif')
      check = glob.glob(prod_name)

      # produce SWIR image
      if len(check) == 0 or args.force:
        print('\n* Porcessing short wave infrared')
        gen_swir(ddir, prod_name, level, cloudMask, ray)
      else:
        print('\n* Short wave infrared already processed. Use -force in your command to overwrite.')
    
    cir_variants = ['cir', 'colorir', 'colorinfrared']
    if next((True for p in products if p.lower() in cir_variants), False):
      # check for color infrared image
      prod_dir = os.path.join(out_date_dir, 'colorInfrared')
      prod_dirs.append(prod_dir)
      if not os.path.isdir(prod_dir):
          os.mkdir(prod_dir)
      prod_name = os.path.join(prod_dir, f'{sat}_{level}_colorInfrared_{date}_{time}_{tile}.tif')
      check = glob.glob(prod_name)

      # produce color infrared image
      if len(check) == 0 or args.force:
        print('\n* Processing color infrared')
        gen_color_infrared(ddir, prod_name, level, cloudMask, ray)
      else:
        print('\n* Color infrared already processed. Use -force in your command to overwrite.')
    
    if next((True for p in products if p.lower() == 'ndwi'), False):
      # check for NDWI
      prod_dir = os.path.join(out_date_dir, 'NDWI')
      prod_dirs.append(prod_dir)
      if not os.path.isdir(prod_dir):
          os.mkdir(prod_dir)
      prod_name = os.path.join(prod_dir, f'{sat}_{level}_NDWI_{date}_{time}_{tile}.tif')
      check = glob.glob(prod_name)
      
      # produce NDWI image
      if len(check) == 0 or args.force:
        print('\n* Processing NDWI')
        gen_ndwi(ddir, prod_name, level, cloudMask, ray)
      else:
        print('\n* NDWI already processed. Use -force in your command to overwrite.')
    
    if next((True for p in products if p.lower() == 'mndwi'), False):
      # check for mNDWI image
      prod_dir = os.path.join(out_date_dir, 'MNDWI')
      prod_dirs.append(prod_dir)
      if not os.path.isdir(prod_dir):
          os.mkdir(prod_dir)
      prod_name = os.path.join(prod_dir, f'{sat}_{level}_MNDWI_{date}_{time}_{tile}.tif')
      check = glob.glob(prod_name)
      
      # produce mNDWI image
      if len(check) == 0 or args.force:
        print('\n* Processing MNDWI')
        gen_mndwi(ddir, prod_name, level, cloudMask, ray)
      else:
        print('\n* MNDWI already processed. Use -force in your command to overwrite.')
    
    if next((True for p in products if p.lower() == 'ndvi'), False):
      # check for NDVI
      prod_dir = os.path.join(out_date_dir, 'NDVI')
      prod_dirs.append(prod_dir)
      if not os.path.isdir(prod_dir):
          os.mkdir(prod_dir)
      prod_name = os.path.join(prod_dir, f'{sat}_{level}_NDVI_{date}_{time}_{tile}.tif')
      check = glob.glob(prod_name)
      
      # produce for NDVI image
      if len(check) == 0 or args.force:
        print('\n* Processing NDVI')
        gen_ndvi(ddir, prod_name, level, cloudMask, ray)
      else:
        print('\n* NDVI already processed. Use -force in your command to overwrite.')
    
    if next((True for p in products if p.lower() == 'nbr'), False):
      # check for NBR image
      prod_dir = os.path.join(out_date_dir, 'NBR')
      prod_dirs.append(prod_dir)
      if not os.path.isdir(prod_dir):
          os.mkdir(prod_dir)
      prod_name = os.path.join(prod_dir, f'{sat}_{level}_NBR_{date}_{time}_{tile}.tif')
      check = glob.glob(prod_name)
      
      # produce NBR image
      if len(check) == 0 or args.force:
        print('\n* Processing NBR')
        gen_nbr(ddir, prod_name, level, cloudMask)
      else:
        print('\n* NBR already processed. Use -force in your command to overwrite.')

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
          check = glob.glob(prod_name)
          if len(check) == 0 or args.force:
            # produce water extent
            print(f'\n* Processing Water Extent (NSTD: {nstd})')
            gen_water_extent(date_dir, float(nstd), prod_name, cloudMask)
          else:
            print('\n* Water Extent already processed. Use -force in your command to overwrite.')

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
