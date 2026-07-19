cwlVersion: v1.2
$graph:
- class: Workflow
  label: capella-ogc-test
  doc: 'Process Capella SAR scenes into Cloud Optimized GeoTIFF disaster-response
    products (sigma-naught, optional Lee filter). Fetches source rasters from the
    CSDA Capella vendor S3 bucket keyed by date. OGC test build: every input is optional
    in the schema so the Submit form never blocks; run.sh enforces the real requirements
    (date, source, non-placeholder activation_event).'
  id: capella-ogc-test
  inputs:
    date:
      doc: Target acquisition date, YYYYMMDDHHMMSS. Closest matching Capella scene
        is selected. Discover valid dates via the 'list-dates' algorithm (sensor=capella).
        REQUIRED for a real run (run.sh rejects an empty date).
      label: Target date
      type: string?
      default: ''
    product:
      doc: Calibration product to generate. Only 'sigma' (sigma-naught) is supported.
      label: Calibration product
      type: string?
      default: sigma
    bucket:
      doc: CSDA Capella vendor S3 bucket to fetch source rasters from (DPS worker
        needs read access).
      label: Vendor S3 bucket
      type: string?
      default: csdap-capellaspace-delivery
    prefix:
      doc: Key prefix within the vendor bucket to search for scenes.
      label: Vendor S3 prefix
      type: string?
      default: disasters
    apply_filter:
      doc: Apply a Lee speckle filter to the sigma0 product.
      label: Apply Lee filter
      type: boolean?
      default: false
    filter_size:
      doc: Lee filter window size in pixels (only used when apply_filter is true).
      label: Lee filter window size
      type: int?
      default: 5
    dst_crs:
      doc: 'Target CRS: native (default, no warp, preserves source UTM) | EPSG:3857
        | EPSG:4326.'
      label: Target CRS
      type: string?
      default: native
    activation_event:
      doc: Activation event, e.g. 202511_Flood_TX. The placeholder YYYYMM_Hazard_Location
        is REJECTED at run time -- set a real value for a real run.
      label: Activation event
      type: string?
      default: YYYYMM_Hazard_Location
    source_label:
      doc: Data origin, e.g. USGS, NASA, NOAA, Capella Space. REQUIRED for a real
        run (run.sh rejects an empty source).
      label: Source
      type: string?
      default: ''
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
        date: date
        product: product
        bucket: bucket
        prefix: prefix
        apply_filter: apply_filter
        filter_size: filter_size
        dst_crs: dst_crs
        activation_event: activation_event
        source_label: source_label
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
      ramMin: 32
      coresMin: 4
      outdirMax: 20
  baseCommand: /app/disasters-product-algorithms/dps/capella/run.sh
  inputs:
    date:
      type: string?
      inputBinding:
        position: 1
        prefix: --date
      default: ''
    product:
      type: string?
      inputBinding:
        position: 2
        prefix: --product
      default: sigma
    bucket:
      type: string?
      inputBinding:
        position: 3
        prefix: --bucket
      default: csdap-capellaspace-delivery
    prefix:
      type: string?
      inputBinding:
        position: 4
        prefix: --prefix
      default: disasters
    apply_filter:
      type: boolean?
      inputBinding:
        position: 5
        prefix: --apply_filter
      default: false
    filter_size:
      type: int?
      inputBinding:
        position: 6
        prefix: --filter_size
      default: 5
    dst_crs:
      type: string?
      inputBinding:
        position: 7
        prefix: --dst_crs
      default: native
    activation_event:
      type: string?
      inputBinding:
        position: 8
        prefix: --activation_event
      default: YYYYMM_Hazard_Location
    source_label:
      type: string?
      inputBinding:
        position: 9
        prefix: --source_label
      default: ''
    compression_level:
      type: int?
      inputBinding:
        position: 10
        prefix: --compression_level
      default: 22
    nodata:
      type: string?
      inputBinding:
        position: 11
        prefix: --nodata
      default: ''
    enable_s3_upload:
      type: boolean?
      inputBinding:
        position: 12
        prefix: --enable_s3_upload
      default: false
    save_png:
      type: boolean?
      inputBinding:
        position: 13
        prefix: --save_png
      default: true
    png_min:
      type: string?
      inputBinding:
        position: 14
        prefix: --png_min
      default: ''
    png_max:
      type: string?
      inputBinding:
        position: 15
        prefix: --png_max
      default: ''
    delete_cog:
      type: boolean?
      inputBinding:
        position: 16
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
s:commitHash: a3d34e579f5156d9149cf26725abaaeb1b97b7b4
s:dateCreated: 2026-07-19
s:license: Apache-2.0
s:softwareVersion: 1.0.0
s:version: dev
s:releaseNotes: "OGC registration test \u2014 all inputs optional (no \"Valid value\
  \ required\") + image built in-workflow from dps/Dockerfile."
s:keywords: capella, sar, sigma0, cog, disasters, flood
$namespaces:
  s: https://schema.org/
$schemas:
- https://raw.githubusercontent.com/schemaorg/schemaorg/refs/heads/main/data/releases/9.0/schemaorg-current-http.rdf
