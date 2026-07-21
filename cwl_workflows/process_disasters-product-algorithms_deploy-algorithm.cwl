cwlVersion: v1.2
$graph:
- class: Workflow
  label: sentinel-2-ogc-test
  doc: 'Download Sentinel-2 L2A/L1C scenes from the Copernicus Data Space (CDSE) by
    MGRS tile(s) + date, then process into Cloud Optimized GeoTIFF disaster-response
    products (true color, SWIR, NDVI, water extent, etc.). OGC test build: every input
    is optional in the schema so the Submit form never blocks; run.sh enforces the
    real requirements (tile, non-placeholder activation_event, readable Copernicus
    secrets). Credentials come from MAAP secrets, never the job inputs.'
  id: sentinel-2-ogc-test
  inputs:
    tile:
      doc: 'Sentinel-2 MGRS tile ID(s) to download, e.g. T17RLN (space-separated for
        several, no quotes: T17RLN T17RLM). Pre-filled with a known-good test tile;
        change it for a real activation.'
      label: MGRS tile(s)
      type: string?
      default: T17RLN T17RLM
    activation_event:
      doc: Activation event, e.g. 202511_Flood_TX. Pre-filled with a test value so
        a bare Submit runs; set a REAL event for a real activation (the placeholder
        YYYYMM_Hazard_Location is rejected at run time).
      label: Activation event
      type: string?
      default: 202601_KyleWx_US
    download_date:
      doc: 'Acquisition date to download: one YYYYMMDD, or a start end pair (space-separated).
        Blank = the CLI''s recent-scenes default (~past 10 days). Pre-filled with
        a known-good test date.'
      label: Download date (optional)
      type: string?
      default: '20251231'
    level:
      doc: 'Sentinel-2 processing level to download: 2 = L2A (surface reflectance),
        1 = L1C (top-of-atmosphere). Default 1 for the fast test path; use 2 for atmospherically-corrected
        production.'
      label: Processing level
      type: string?
      default: '1'
    limit:
      doc: Maximum number of Copernicus search results to download for the tile/date.
      label: Search result limit
      type: int?
      default: 50
    products:
      doc: Space-separated list (true nat swir colorIR ndvi ndwi mndwi nbr we) or
        'all'.
      label: Products
      type: string?
      default: true swir
    source_label:
      doc: Data origin, e.g. Copernicus, ESA.
      label: Source
      type: string?
      default: Copernicus
    dst_crs:
      doc: 'Target CRS: native (default, no warp, preserves source projection) | EPSG:3857
        | EPSG:4326.'
      label: Target CRS
      type: string?
      default: native
    merge:
      doc: Mosaic scenes by date and product (-merge).
      label: Merge by date/product
      type: boolean?
      default: true
    mask:
      doc: Generate and apply a cloud mask (-mask, L2A only).
      label: Cloud mask
      type: boolean?
      default: false
    we_nstd:
      doc: Space-separated std-dev thresholds for water extent (no quotes), e.g. 1
        1.5.
      label: Water-extent std devs (optional)
      type: string?
      default: ''
    compression_level:
      doc: ZSTD level 1-22. Lower = faster/larger; higher = slower/smaller. Default
        1 for a fast test; raise (e.g. 22 = max) for smaller production COGs.
      label: COG compression level
      type: int?
      default: 1
    nodata:
      doc: No-data value for the output COGs (Sentinel-2 default 0).
      label: No-data value
      type: string?
      default: '0'
    enable_s3_upload:
      doc: Upload products to s3://nasa-disasters/drcs_activations_new/<activation_event>/
        (locked destination; DPS also uploads output/ regardless). ON by default.
      label: Publish to S3
      type: boolean?
      default: true
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
        tile: tile
        activation_event: activation_event
        download_date: download_date
        level: level
        limit: limit
        products: products
        source_label: source_label
        dst_crs: dst_crs
        merge: merge
        mask: mask
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
  baseCommand: /app/disasters-product-algorithms/dps/sentinel2/run.sh
  inputs:
    tile:
      type: string?
      inputBinding:
        position: 1
        prefix: --tile
      default: T17RLN T17RLM
    activation_event:
      type: string?
      inputBinding:
        position: 2
        prefix: --activation_event
      default: 202601_KyleWx_US
    download_date:
      type: string?
      inputBinding:
        position: 3
        prefix: --download_date
      default: '20251231'
    level:
      type: string?
      inputBinding:
        position: 4
        prefix: --level
      default: '1'
    limit:
      type: int?
      inputBinding:
        position: 5
        prefix: --limit
      default: 50
    products:
      type: string?
      inputBinding:
        position: 6
        prefix: --products
      default: true swir
    source_label:
      type: string?
      inputBinding:
        position: 7
        prefix: --source_label
      default: Copernicus
    dst_crs:
      type: string?
      inputBinding:
        position: 8
        prefix: --dst_crs
      default: native
    merge:
      type: boolean?
      inputBinding:
        position: 9
        prefix: --merge
      default: true
    mask:
      type: boolean?
      inputBinding:
        position: 10
        prefix: --mask
      default: false
    we_nstd:
      type: string?
      inputBinding:
        position: 11
        prefix: --we_nstd
      default: ''
    compression_level:
      type: int?
      inputBinding:
        position: 12
        prefix: --compression_level
      default: 1
    nodata:
      type: string?
      inputBinding:
        position: 13
        prefix: --nodata
      default: '0'
    enable_s3_upload:
      type: boolean?
      inputBinding:
        position: 14
        prefix: --enable_s3_upload
      default: true
    save_png:
      type: boolean?
      inputBinding:
        position: 15
        prefix: --save_png
      default: true
    png_min:
      type: string?
      inputBinding:
        position: 16
        prefix: --png_min
      default: ''
    png_max:
      type: string?
      inputBinding:
        position: 17
        prefix: --png_max
      default: ''
    delete_cog:
      type: boolean?
      inputBinding:
        position: 18
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
s:commitHash: 6ce6741812ca9ff8b9e57cfd1b57af81928a55a5
s:dateCreated: 2026-07-21
s:license: Apache-2.0
s:softwareVersion: 1.0.0
s:version: dev
s:releaseNotes: "OGC registration test \u2014 download-from-Copernicus (creds via\
  \ MAAP secrets, not job inputs); all inputs optional; image built in-workflow from\
  \ dps/Dockerfile."
s:keywords: sentinel-2, cog, disasters, flood, fire, ndvi, water-extent, copernicus
$namespaces:
  s: https://schema.org/
$schemas:
- https://raw.githubusercontent.com/schemaorg/schemaorg/refs/heads/main/data/releases/9.0/schemaorg-current-http.rdf
