cwlVersion: v1.2
$graph:
- class: Workflow
  label: disasters-blackmarble-noaa-process
  doc: "VEDA Black Marble nighttime-lights pipeline on NOAA-20 (VJ146A2): for a WGS84\
    \ bbox + date, download VIIRS nighttime lights (Earthdata), Landsat (STAC), and\
    \ OSM roads, then fuse into an urban-focused Cloud Optimized GeoTIFF. Use this\
    \ when the target date has no Suomi-NPP coverage \u2014 Suomi-NPP product delivery\
    \ ceases 2026-11-01, NOAA-20 is unaffected. VJ146A2 exists from 2018-01-19 onward;\
    \ for earlier dates use disasters-blackmarble-process. Outputs go to their own\
    \ hdnightlightsnoaa20/ product folder, so they never overwrite a Suomi-NPP run\
    \ for the same event and date. Every input is optional in the schema so the Submit\
    \ form never blocks; run.sh enforces the real requirements (valid bbox/date, date\
    \ not before 2018-01-19, non-placeholder activation_event, readable Earthdata\
    \ secret). The Earthdata token comes from MAAP secrets, never the job inputs."
  id: disasters-blackmarble-noaa-process
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
        Must be 2018-01-19 or later -- NOAA-20 VJ146A2 does not exist before then
        (use disasters-blackmarble-process for earlier dates). Pre-filled with a known-good
        test date.
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
  baseCommand: /app/disasters-product-algorithms/dps/blackmarble_noaa/run.sh
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
    earthdata_secret_name:
      type: string?
      inputBinding:
        position: 7
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
s:commitHash: e5082ef782e252e66369d07be7971dfe7e79d482
s:dateCreated: 2026-08-13
s:license: Apache-2.0
s:softwareVersion: 1.0.0
s:version: dev
s:releaseNotes: NOAA-20 (VJ146A2) variant of the Black Marble job for dates without
  Suomi-NPP coverage; shares the Suomi-NPP engine via BM_PLATFORM, image built in-workflow
  from dps/Dockerfile.
s:keywords: black-marble, viirs, vj146a2, noaa-20, jpss-1, nighttime-lights, landsat,
  osm, cog, disasters, veda
$namespaces:
  s: https://schema.org/
$schemas:
- https://raw.githubusercontent.com/schemaorg/schemaorg/refs/heads/main/data/releases/9.0/schemaorg-current-http.rdf
