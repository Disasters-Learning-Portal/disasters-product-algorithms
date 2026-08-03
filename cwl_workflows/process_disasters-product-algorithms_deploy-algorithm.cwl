cwlVersion: v1.2
$graph:
- class: Workflow
  label: satellogic-ogc-test
  doc: 'Generate a Satellogic disaster-response product (true color, color IR, NDVI,
    NDWI, or EVI) from CSDA vendor L1B/L1D rasters and write a Cloud Optimized GeoTIFF.
    OGC build: every input optional in the schema; run.sh enforces the real requirements
    (date, source, non-placeholder activation_event, level).'
  id: satellogic-ogc-test
  inputs:
    date:
      doc: Target acquisition datetime 'YYYY-MM-DD HH:MM:SS'. REQUIRED for a real
        run. Discover valid dates with the list-dates algorithm (sensor=satellogic,
        level=<L1D|L1B>).
      label: Target datetime
      type: string?
      default: ''
    product:
      doc: 'One of: truecolor, colorir, ndvi, ndwi, evi.'
      label: Product
      type: string?
      default: truecolor
    level:
      doc: 'Satellogic processing level: L1D or L1B.'
      label: Processing level
      type: string?
      default: L1D
    bucket:
      doc: CSDA vendor bucket the rasters are read from. The CLI hardcodes this; changing
        it has no effect (documented for the AWS read-access requirement).
      label: Vendor S3 bucket (informational)
      type: string?
      default: csda-data-vendor-satellogic
    prefix:
      doc: Key prefix under the vendor bucket. The CLI hardcodes this; changing it
        has no effect.
      label: Vendor S3 prefix (informational)
      type: string?
      default: disasters
    use_mask:
      doc: Apply the cloud mask (index/water products only).
      label: Apply cloud mask
      type: boolean?
      default: false
    visualize:
      doc: Apply normalization + gamma correction for RGB products.
      label: Visualize (RGB only)
      type: boolean?
      default: false
    gamma:
      doc: Gamma correction for RGB products (only used when visualize is true).
      label: Gamma correction
      type: float?
      default: 0.7
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
      doc: Data origin, e.g. USGS, NASA, NOAA, Satellogic. REQUIRED for a real run.
      label: Source
      type: string?
      default: ''
    compression_level:
      doc: ZSTD level 1-22 (22 = max, default).
      label: COG compression level
      type: int?
      default: 22
    nodata:
      doc: Override the auto-detected no-data value; leave blank to auto-detect.
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
        level: level
        bucket: bucket
        prefix: prefix
        use_mask: use_mask
        visualize: visualize
        gamma: gamma
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
  baseCommand: /app/disasters-product-algorithms/dps/satellogic/run.sh
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
      default: truecolor
    level:
      type: string?
      inputBinding:
        position: 3
        prefix: --level
      default: L1D
    bucket:
      type: string?
      inputBinding:
        position: 4
        prefix: --bucket
      default: csda-data-vendor-satellogic
    prefix:
      type: string?
      inputBinding:
        position: 5
        prefix: --prefix
      default: disasters
    use_mask:
      type: boolean?
      inputBinding:
        position: 6
        prefix: --use_mask
      default: false
    visualize:
      type: boolean?
      inputBinding:
        position: 7
        prefix: --visualize
      default: false
    gamma:
      type: float?
      inputBinding:
        position: 8
        prefix: --gamma
      default: 0.7
    dst_crs:
      type: string?
      inputBinding:
        position: 9
        prefix: --dst_crs
      default: native
    activation_event:
      type: string?
      inputBinding:
        position: 10
        prefix: --activation_event
      default: YYYYMM_Hazard_Location
    source_label:
      type: string?
      inputBinding:
        position: 11
        prefix: --source_label
      default: ''
    compression_level:
      type: int?
      inputBinding:
        position: 12
        prefix: --compression_level
      default: 22
    nodata:
      type: string?
      inputBinding:
        position: 13
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
s:commitHash: c282e9a90b3f2ee047062e9039632ea57012e74f
s:dateCreated: 2026-08-03
s:license: Apache-2.0
s:softwareVersion: 1.0.0
s:version: dev
s:releaseNotes: "OGC registration \u2014 all inputs optional; image built in-workflow\
  \ from dps/Dockerfile."
s:keywords: satellogic, cog, disasters, flood, fire, ndvi, ndwi, evi
$namespaces:
  s: https://schema.org/
$schemas:
- https://raw.githubusercontent.com/schemaorg/schemaorg/refs/heads/main/data/releases/9.0/schemaorg-current-http.rdf
