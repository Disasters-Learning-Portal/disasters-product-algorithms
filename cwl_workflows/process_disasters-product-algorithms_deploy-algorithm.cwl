cwlVersion: v1.2
$graph:
- class: Workflow
  label: list-dates-ogc-test
  doc: Discovery tool. Pick a SENSOR (capella | umbra | satellogic) and Submit to
    print the scenes currently available in that sensor's CSDA vendor S3 bucket, newest
    first by S3 delivery time (report to the job log + CSV in output/). No processing,
    no COG. Sentinel-2/Landsat are file-input and not options here.
  id: list-dates-ogc-test
  inputs:
    sensor:
      doc: 'Which vendor bucket to list scene dates for: capella | umbra | satellogic
        (case-insensitive, spaces trimmed). Any other value aborts before any S3 listing.
        (Sentinel-2 and Landsat are file-input and have no vendor-bucket discovery.)'
      label: Sensor
      type: string?
      default: capella
    level:
      doc: "Satellogic processing level to report \u2014 L1D | L1B (case-insensitive;\
        \ an invalid value aborts before listing). ONLY used when sensor=satellogic;\
        \ ignored (with a NOTE in the log) for capella and umbra."
      label: Processing level (Satellogic only)
      type: string?
      default: L1D
  outputs:
    output:
      type: Directory
      outputSource: process/outputs_result
  steps:
    process:
      run: '#main'
      in:
        sensor: sensor
        level: level
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
      ramMin: 2
      coresMin: 1
      outdirMax: 1
  baseCommand: /app/disasters-product-algorithms/dps/list_dates/run.sh
  inputs:
    sensor:
      type: string?
      inputBinding:
        position: 1
        prefix: --sensor
      default: capella
    level:
      type: string?
      inputBinding:
        position: 2
        prefix: --level
      default: L1D
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
s:commitHash: e5bbd341dad3d93b3548c51a6d12d6c2d4cd0e3e
s:dateCreated: 2026-07-20
s:license: Apache-2.0
s:softwareVersion: 1.0.0
s:version: dev
s:releaseNotes: "OGC registration \u2014 sensor-selector discovery tool; image built\
  \ in-workflow from dps/Dockerfile."
s:keywords: discovery, list-dates, capella, umbra, satellogic, disasters
$namespaces:
  s: https://schema.org/
$schemas:
- https://raw.githubusercontent.com/schemaorg/schemaorg/refs/heads/main/data/releases/9.0/schemaorg-current-http.rdf
