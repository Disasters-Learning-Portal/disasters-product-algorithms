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
    filter_size:
      doc: Lee speckle-filter window size in pixels. Filtering is always applied;
        this only tunes the kernel. Must be 3, 5 or 7.
      label: Lee filter window size
      type: int?
      default: 5
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
  outputs:
    output:
      type: Directory
      outputSource: process/outputs_result
  steps:
    process:
      run: '#main'
      in:
        date: date
        filter_size: filter_size
        activation_event: activation_event
        source_label: source_label
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
    filter_size:
      type: int?
      inputBinding:
        position: 2
        prefix: --filter_size
      default: 5
    activation_event:
      type: string?
      inputBinding:
        position: 3
        prefix: --activation_event
      default: YYYYMM_Hazard_Location
    source_label:
      type: string?
      inputBinding:
        position: 4
        prefix: --source_label
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
s:commitHash: 9123113ac1649eaa4e1dbd1487052aeb54c39a04
s:dateCreated: 2026-08-12
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
