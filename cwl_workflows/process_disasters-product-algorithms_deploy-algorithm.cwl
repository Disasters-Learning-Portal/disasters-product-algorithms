cwlVersion: v1.2
$graph:
- class: Workflow
  label: disasters-umbra-process
  doc: Process Umbra SAR scenes into Cloud Optimized GeoTIFF disaster-response products
    (sigma/beta/gamma calibration, always-on Lee speckle filter with a selectable
    3/5/7 window). Fetches the source GEC raster from the CSDA Umbra vendor S3 bucket
    keyed by date. Every input is optional in the schema so the Submit form never
    blocks; run.sh enforces the real requirements.
  id: disasters-umbra-process
  inputs:
    date:
      doc: Target acquisition date 'YYYY-MM-DD HH:MM:SS'. REQUIRED for a real run
        (run.sh rejects an empty date). Discover valid dates with the list-dates algorithm
        (sensor=umbra).
      label: Target date
      type: string?
      default: ''
    product:
      doc: 'Calibration product to generate: sigma, beta, or gamma.'
      label: Calibration product
      type: string?
      default: sigma
    bucket:
      doc: CSDA Umbra vendor S3 bucket (DPS worker needs read access).
      label: Vendor S3 bucket
      type: string?
      default: csda-data-vendor-umbra
    prefix:
      doc: Key prefix within the vendor bucket to search for scenes.
      label: S3 prefix
      type: string?
      default: disasters
    filter_size:
      doc: Lee speckle-filter kernel. Filtering is always applied to the backscatter
        (no opt-out); only the kernel is tunable. Must be 3, 5, or 7.
      label: Lee filter window size
      type: int?
      default: 5
    dst_crs:
      doc: 'Target CRS: native (default) | EPSG:3857 | EPSG:4326.'
      label: Target CRS
      type: string?
      default: native
    activation_event:
      doc: Activation event, e.g. 202511_Flood_TX. The placeholder YYYYMM_Hazard_Location
        is REJECTED at run time.
      label: Activation event
      type: string?
      default: YYYYMM_Hazard_Location
    source_label:
      doc: Data origin, e.g. USGS, NASA, NOAA, Umbra. REQUIRED for a real run.
      label: Source
      type: string?
      default: ''
    compression_level:
      doc: ZSTD level 1-22 (22 = max, default).
      label: COG compression level
      type: int?
      default: 22
    nodata:
      doc: Override the no-data value; leave blank to use the default (-9999.0). SAR
        dB backscatter uses -9999.0, never 0 (0 dB is a legitimate value).
      label: No-data value (optional)
      type: string?
      default: ''
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
        filter_size: filter_size
        dst_crs: dst_crs
        activation_event: activation_event
        source_label: source_label
        compression_level: compression_level
        nodata: nodata
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
  baseCommand: /app/disasters-product-algorithms/dps/umbra/run.sh
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
      default: csda-data-vendor-umbra
    prefix:
      type: string?
      inputBinding:
        position: 4
        prefix: --prefix
      default: disasters
    filter_size:
      type: int?
      inputBinding:
        position: 5
        prefix: --filter_size
      default: 5
    dst_crs:
      type: string?
      inputBinding:
        position: 6
        prefix: --dst_crs
      default: native
    activation_event:
      type: string?
      inputBinding:
        position: 7
        prefix: --activation_event
      default: YYYYMM_Hazard_Location
    source_label:
      type: string?
      inputBinding:
        position: 8
        prefix: --source_label
      default: ''
    compression_level:
      type: int?
      inputBinding:
        position: 9
        prefix: --compression_level
      default: 22
    nodata:
      type: string?
      inputBinding:
        position: 10
        prefix: --nodata
      default: ''
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
s:commitHash: aaca51a90bea651677b9a32debf9a59918f9e2f7
s:dateCreated: 2026-08-13
s:license: Apache-2.0
s:softwareVersion: 1.0.0
s:version: dev
s:releaseNotes: "OGC registration \u2014 all inputs optional; image built in-workflow\
  \ from dps/Dockerfile."
s:keywords: umbra, sar, cog, disasters, flood, calibration
$namespaces:
  s: https://schema.org/
$schemas:
- https://raw.githubusercontent.com/schemaorg/schemaorg/refs/heads/main/data/releases/9.0/schemaorg-current-http.rdf
