# MAAP DPS integration

This folder wires the sensor pipelines into the MAAP
[Data Processing System (DPS)](https://docs.maap-project.org/en/latest/technical_tutorials/dps_tutorial/dps_tutorial_demo.html).

DPS clones this (public) git repo, runs a **build script** once to create a conda
env, then runs a **run script** per job. DPS downloads declared *file* inputs
into a relative `input/` dir, passes *positional* inputs as `$1 $2 …`, and
uploads anything the run script writes to a relative `output/` dir to S3.

## Layout

```
dps/
├── environment.yml          # SHARED lean conda env (name: disasters_dps)
├── register_algorithms.py   # registers each algorithm_config.yaml with MAAP
├── landsat/
│   ├── build-env.sh         # conda env update + pip install repo
│   ├── run.sh               # file-input -> process_landsat89 -> output/
│   └── algorithm_config.yaml
└── sentinel2/               # same shape (process_sentinel2)
    ├── build-env.sh
    ├── run.sh
    └── algorithm_config.yaml
```

Scope is **Landsat + Sentinel-2** (the file-input optical sensors). The SAR /
vendor sensors (capella, umbra, satellogic) read CSDA vendor S3 buckets and need
DPS-worker AWS access to those buckets — deferred until that platform question is
resolved. See [docs/DPS.md](../docs/DPS.md).

## Test locally before registering

Reproduce the DPS working dir, then run the build + run scripts exactly as DPS
will (positional args in registration order):

```bash
WORK=$(mktemp -d); cd "$WORK"; mkdir -p input
cp /path/to/LC09_..._02_T1.tar input/                 # a Landsat C2 L2 granule

bash /path/to/repo/dps/landsat/build-env.sh           # creates disasters_dps env
bash /path/to/repo/dps/landsat/run.sh \
  "202512_Flood_WA" "true ndvi" "EPSG:3857" "Landsat 8/9 Collection 2 Level-2"

ls -la output/                                         # must be NON-EMPTY
conda run -n disasters_dps which process_landsat89     # console script on PATH
gdalinfo output/<one>.tif | grep -E 'ACTIVATION_EVENT|SOURCE|PROCESSOR'
```

Pass criteria: non-empty `output/` COGs, GeoTIFF activation tags present, and a
real `PROCESSOR` version (proves setuptools-scm resolved). Repeat for Sentinel-2
with a `.zip` granule.

## Register + submit

From a MAAP ADE workspace (maap-py installed):

```bash
python dps/register_algorithms.py            # registers landsat + sentinel2
```

```python
from maap.maap import MAAP
maap = MAAP()
job = maap.submitJob(
    identifier="landsat-202512_Flood_WA-test",
    algo_id="disasters_process_landsat89", version="dev",
    queue="maap-dps-worker-16gb",
    granule_archive="s3://<staging-bucket>/LC09_..._02_T1.tar",
    activation_event="202512_Flood_WA", products="true ndvi",
    dst_crs="EPSG:3857", source_label="Landsat 8/9 Collection 2 Level-2",
)
print(job.id)
print(maap.getJobStatus(job.id))   # then maap.getJobResult(job.id) when Succeeded
```

## Before registering: confirm

- **`base_container_url`** in each `algorithm_config.yaml` points at the MAAP OPS
  base image (`custom_images/maap_base`). Confirm the exact URL/tag in the
  registration UI's "Container URL" dropdown.
- **`algorithm_version`** points at a git ref DPS can clone. It is set to `dev`
  for active development; pin a tag (e.g. `v0.10.0`) for reproducible production
  runs. Either way the clone must include git tags so setuptools-scm can resolve
  a version (`build-env.sh` has a fallback if it can't).
