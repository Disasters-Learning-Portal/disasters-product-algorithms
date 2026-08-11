cwlVersion: v1.2
$graph:
- class: Workflow
  label: disasters-blackmarble-process
  doc: 'VEDA Black Marble nighttime-lights pipeline: for a WGS84 bbox + date, download
    VIIRS VNP46A2 (Earthdata), Landsat (STAC), and OSM roads, then fuse into an urban-focused
    Cloud Optimized GeoTIFF. Every input is optional in the schema so the Submit form
    never blocks; run.sh enforces the real requirements (valid bbox/date, non-placeholder
    activation_event, readable Earthdata secret). The Earthdata token comes from MAAP
    secrets, never the job inputs.'
  id: disasters-blackmarble-process
  inputs:
    bbox:
      doc: min_lon,min_lat,max_lon,max_lat in WGS84, e.g. -122.55,37.69,-122.32,37.81.
        Latitude span must be >= 0.05 deg. Pre-filled with a known-good San Francisco
        test box; change it for a real activation.
      label: Bounding box (WGS84)
      type: string?
      default: -122.55,37.69,-122.32,37.81
    activation_event:
      doc: Activation event, e.g. 202511_Flood_TX -- used for the S3 output path (dps_output/<event>/).
        Pre-filled with a test value so a bare Submit runs; set a REAL event for a
        real activation (the placeholder YYYYMM_Hazard_Location is rejected at run
        time).
      label: Activation event
      type: string?
      default: 202601_KyleWx_US
    date:
      doc: Target date YYYY-MM-DD. Needs VIIRS + Landsat coverage near this date.
        Pre-filled with a known-good test date.
      label: Target date
      type: string?
      default: '2023-06-15'
    config:
      doc: 'Processing preset: fast (quick smoke, minimal enhancements) | default
        | high_quality. Default fast.'
      label: Quality preset
      type: string?
      default: fast
    osm_source:
      doc: 'OpenStreetMap road backend: overpass (Overpass API via OSMnx) | layercake
        (OSM-US parquet, faster on large/dense areas, experimental). Default overpass.'
      label: OSM road source
      type: string?
      default: overpass
    wgs84:
      doc: Also write an EPSG:4326 (WGS84) COG next to the native output (for web
        mapping).
      label: Also export EPSG:4326
      type: boolean?
      default: false
    basename:
      doc: Output COG filename stem -> <basename>.tif (letters, digits, . _ - only).
      label: Output filename stem
      type: string?
      default: black_marble_output
    earthdata_secret_name:
      doc: NAME of the MAAP secret holding your NASA Earthdata token (not the token
        value). Default EARTHDATA_TOKEN. Store it once with maap.secrets.add_secret('EARTHDATA_TOKEN',
        '<token>').
      label: Earthdata secret name
      type: string?
      default: EARTHDATA_TOKEN
  outputs:
    output:
      type: Directory
      outputSource: process/outputs_result
  steps:
    process:
      run: '#main'
      in:
        bbox: bbox
        activation_event: activation_event
        date: date
        config: config
        osm_source: osm_source
        wgs84: wgs84
        basename: basename
        earthdata_secret_name: earthdata_secret_name
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
      ramMin: 16
      coresMin: 4
      outdirMax: 20
  baseCommand: /app/disasters-product-algorithms/dps/blackmarble/run.sh
  inputs:
    bbox:
      type: string?
      inputBinding:
        position: 1
        prefix: --bbox
      default: -122.55,37.69,-122.32,37.81
    activation_event:
      type: string?
      inputBinding:
        position: 2
        prefix: --activation_event
      default: 202601_KyleWx_US
    date:
      type: string?
      inputBinding:
        position: 3
        prefix: --date
      default: '2023-06-15'
    config:
      type: string?
      inputBinding:
        position: 4
        prefix: --config
      default: fast
    osm_source:
      type: string?
      inputBinding:
        position: 5
        prefix: --osm_source
      default: overpass
    wgs84:
      type: boolean?
      inputBinding:
        position: 6
        prefix: --wgs84
      default: false
    basename:
      type: string?
      inputBinding:
        position: 7
        prefix: --basename
      default: black_marble_output
    earthdata_secret_name:
      type: string?
      inputBinding:
        position: 8
        prefix: --earthdata_secret_name
      default: EARTHDATA_TOKEN
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
s:commitHash: 55a3169b2b509ba441615170143d207dbcbb2793
s:dateCreated: 2026-08-11
s:license: Apache-2.0
s:softwareVersion: 1.0.0
s:version: dev
s:releaseNotes: "OGC registration test \u2014 download-from-Earthdata/STAC/OSM (token\
  \ via MAAP secrets, not job inputs); all inputs optional; image built in-workflow\
  \ from dps/Dockerfile."
s:keywords: black-marble, viirs, nighttime-lights, landsat, osm, cog, disasters, veda
$namespaces:
  s: https://schema.org/
$schemas:
- https://raw.githubusercontent.com/schemaorg/schemaorg/refs/heads/main/data/releases/9.0/schemaorg-current-http.rdf
