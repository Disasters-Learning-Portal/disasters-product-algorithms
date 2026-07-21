cwlVersion: v1.2
$graph:
- class: Workflow
  label: landsat-8-9-ogc-test
  doc: 'Process Landsat 8/9 Collection 2 Level-2 granule archives (.tar/.zip) into
    Cloud Optimized GeoTIFF disaster-response products (true color, NDVI, water extent,
    etc.) with reprojection and activation-event GeoTIFF metadata tags. OGC test build:
    the granule File is required, every other input is optional in the schema so the
    Submit form never blocks on a falsy default; run.sh enforces the real requirements
    (granule, source, non-placeholder activation_event).'
  id: landsat-8-9-ogc-test
  inputs:
    file_path_of_raw_data:
      doc: A Landsat Collection 2 Level-2 granule archive (.tar or .zip). REQUIRED.
      label: Raw data file
      type: File
    activation_event:
      doc: Activation event, e.g. 202511_Flood_TX. The placeholder YYYYMM_Hazard_Location
        is REJECTED at run time -- set a real value for a real run.
      label: Activation event
      type: string?
      default: YYYYMM_Hazard_Location
    products:
      doc: Space-separated list (true pan nat colorIR ndvi ndwi mndwi evi nbr we)
        or 'all'.
      label: Products
      type: string?
      default: 'true'
    source_label:
      doc: Data origin, e.g. USGS, NASA, NOAA. REQUIRED for a real run (run.sh rejects
        an empty source).
      label: Source
      type: string?
      default: ''
    dst_crs:
      doc: 'Target CRS: native (default, no warp, preserves source projection) | EPSG:3857
        | EPSG:4326.'
      label: Target CRS
      type: string?
      default: native
    merge:
      doc: Mosaic tiles by date and product (-merge).
      label: Merge by date/product
      type: boolean?
      default: true
    mask:
      doc: Generate and apply a cloud mask (-mask).
      label: Cloud mask
      type: boolean?
      default: true
    process_date:
      doc: Only process this date, YYYYMMDD; leave blank for all dates.
      label: Process date (optional)
      type: string?
      default: ''
    process_tile:
      doc: Only process this path/row, e.g. 171035; leave blank for all tiles.
      label: Process tile (optional)
      type: string?
      default: ''
    we_nstd:
      doc: Space-separated std-dev thresholds for water extent (only used when products
        includes "we").
      label: Water-extent std devs
      type: string?
      default: 1 1.5
    compression_level:
      doc: ZSTD level 1-22. Lower = faster/larger; higher = slower/smaller. 22 = max
        (default).
      label: COG compression level
      type: int?
      default: 22
    nodata:
      doc: Override the auto-detected no-data value; leave blank to auto-detect.
      label: No-data value (optional)
      type: string?
      default: ''
    enable_s3_upload:
      doc: Upload products to s3://nasa-disasters/drcs_activations_new/<activation_event>/
        (locked destination; DPS also uploads output/ regardless).
      label: Publish to S3
      type: boolean?
      default: false
    save_png:
      doc: Also write a .png quicklook next to each product COG.
      label: Save PNG quicklook
      type: boolean?
      default: true
    png_min:
      doc: Manual lower bound for PNG scaling; blank = auto (2nd percentile, or 0
        for uint8 RGB).
      label: PNG min (optional)
      type: string?
      default: ''
    png_max:
      doc: Manual upper bound for PNG scaling; blank = auto (98th percentile, or 255
        for uint8 RGB).
      label: PNG max (optional)
      type: string?
      default: ''
    delete_cog:
      doc: Delete the COG from ~/drcs_outputs after it is copied to output/ and uploaded
        (PNG + output/ copy kept).
      label: Delete COG from home after upload
      type: boolean?
      default: true
  outputs:
    output:
      type: Directory
      outputSource: process/outputs_result
  steps:
    process:
      run: '#main'
      in:
        file_path_of_raw_data: file_path_of_raw_data
        activation_event: activation_event
        products: products
        source_label: source_label
        dst_crs: dst_crs
        merge: merge
        mask: mask
        process_date: process_date
        process_tile: process_tile
        we_nstd: we_nstd
        compression_level: compression_level
        nodata: nodata
        enable_s3_upload: enable_s3_upload
        save_png: save_png
        png_min: png_min
        png_max: png_max
        delete_cog: delete_cog
      out:
      - outputs_result
- class: CommandLineTool
  id: main
  requirements:
    DockerRequirement:
      dockerPull: ghcr.io/disasters-learning-portal/disasters-product-algorithms:deploy-algorithm
    NetworkAccess:
      networkAccess: true
    ResourceRequirement:
      ramMin: 64
      coresMin: 8
      outdirMax: 20
  baseCommand: /app/disasters-product-algorithms/dps/landsat/run.sh
  inputs:
    file_path_of_raw_data:
      type: File
      inputBinding:
        position: 1
        prefix: --file_path_of_raw_data
    activation_event:
      type: string?
      inputBinding:
        position: 2
        prefix: --activation_event
      default: YYYYMM_Hazard_Location
    products:
      type: string?
      inputBinding:
        position: 3
        prefix: --products
      default: 'true'
    source_label:
      type: string?
      inputBinding:
        position: 4
        prefix: --source_label
      default: ''
    dst_crs:
      type: string?
      inputBinding:
        position: 5
        prefix: --dst_crs
      default: native
    merge:
      type: boolean?
      inputBinding:
        position: 6
        prefix: --merge
      default: true
    mask:
      type: boolean?
      inputBinding:
        position: 7
        prefix: --mask
      default: true
    process_date:
      type: string?
      inputBinding:
        position: 8
        prefix: --process_date
      default: ''
    process_tile:
      type: string?
      inputBinding:
        position: 9
        prefix: --process_tile
      default: ''
    we_nstd:
      type: string?
      inputBinding:
        position: 10
        prefix: --we_nstd
      default: 1 1.5
    compression_level:
      type: int?
      inputBinding:
        position: 11
        prefix: --compression_level
      default: 22
    nodata:
      type: string?
      inputBinding:
        position: 12
        prefix: --nodata
      default: ''
    enable_s3_upload:
      type: boolean?
      inputBinding:
        position: 13
        prefix: --enable_s3_upload
      default: false
    save_png:
      type: boolean?
      inputBinding:
        position: 14
        prefix: --save_png
      default: true
    png_min:
      type: string?
      inputBinding:
        position: 15
        prefix: --png_min
      default: ''
    png_max:
      type: string?
      inputBinding:
        position: 16
        prefix: --png_max
      default: ''
    delete_cog:
      type: boolean?
      inputBinding:
        position: 17
        prefix: --delete_cog
      default: true
  outputs:
    outputs_result:
      outputBinding:
        glob: ./output*
      type: Directory
s:author:
- class: s:Person
  s:name: NASA Disasters
s:contributor:
- class: s:Person
  s:name: NASA Disasters
s:citation: NASA Disasters Program
s:codeRepository: https://github.com/Disasters-Learning-Portal/disasters-product-algorithms.git
s:commitHash: 17ff033265cec105b0598e864269ce701816be8f
s:dateCreated: 2026-07-21
s:license: Apache-2.0
s:softwareVersion: 1.0.0
s:version: dev
s:releaseNotes: "OGC registration test \u2014 file input required, all other inputs\
  \ optional (no \"Valid value required\") + image built in-workflow from dps/Dockerfile."
s:keywords: landsat, cog, disasters, flood, fire, ndvi, water-extent
$namespaces:
  s: https://schema.org/
$schemas:
- https://raw.githubusercontent.com/schemaorg/schemaorg/refs/heads/main/data/releases/9.0/schemaorg-current-http.rdf
