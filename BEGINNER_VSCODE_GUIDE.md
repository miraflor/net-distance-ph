# Run ph-net-distance in VS Code on Windows

This guide is for a **local** computation. The repository does not include the
national GIS files or computed matrices.

## 1. Open the project

Unzip or clone the repository. In VS Code choose **File → Open Folder** and
select the `ph-net-distance` directory containing `run.py`, `config.py` and
the `ph_net_distance/` Python package directory.

## 2. Activate a Python environment

Open **Terminal → New Terminal**, then use Conda if available:

```powershell
conda create -n phdistance python=3.12 -y
conda activate phdistance
python -m pip install -e ".[test]"
python --version
```

Select the same interpreter in VS Code via **Python: Select Interpreter**.
Conda is optional; a Python virtual environment is also supported.

## 3. Put your data in a separate folder

The program expects WGS84 (EPSG:4326) road and ferry **polyline** shapefiles
plus a point shapefile with locality IDs and names. The required basenames:

- `primsectertrunk_road.shp`
- `ferry_route_shape.shp`
- `muncity_centroids.shp` (+ `.dbf` with text IDs)

Leave related shapefile sidecars beside each `.shp`. For example, you might put
them in `D:\GIS\Philippines\DistanceMatrixInputs`; **do not commit input
shapefiles or private data to GitHub**.

## 4. Run the tests and setup check

```powershell
python -m pytest -q
python check_setup.py --data "D:\GIS\Philippines\DistanceMatrixInputs"
```

Resolve any `FAIL` diagnostics first. An unexpected ID or duplicated source
filename is now rejected rather than silently used.

## 5. Compute the matrix

```powershell
python run.py all --data "D:\GIS\Philippines\DistanceMatrixInputs" --workers 4
```

The four stages are `graph`, `reduce`, `matrix`, `verify`. Keep the terminal
open so you can monitor logs and resumable checkpoints. Use individual stages
if you need control over a long computation:

```powershell
python run.py graph --data "D:\GIS\Philippines\DistanceMatrixInputs"
python run.py reduce
python check_reduce_status.py
python run.py matrix --workers 4
python run.py verify
```

You give the data folder only to `graph`: it records the folder, and the later
stages read the same folder again to confirm that the shapefiles have not
changed. If you move the shapefiles, add `--data` with their new location.

Intermediate files are in `work/`. The matrix, labels, partial-result radius
and verification reports are in `out/`. Both directories are ignored by git.

## 6. Working with a different source release

Do **not** mix old graph or matrix intermediates with new shapefiles. Run
against a new, empty `work/` and `out/` location (using `--work` and `--out`
on every stage or environment variables), or intentionally clear both
folders before rerunning. The program stops with a message if it finds stage
files from different runs, or if any input shapefile changed after `graph`.

For full model interpretation, partial-result rules and attribution, see the
[documentation](docs/index.md).
