"""Provider-agnostic CSV input, one-command defaults, and code isolation."""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from argparse import Namespace
import pytest

from net_distance_ph import population_data as ds
from net_distance_ph import population_geometry as geo
from net_distance_ph import population_weighted as pop


def rows_fixture(folder):
    a="0100000000"; p="0128000000"; t="0128010000"
    units=[dict(psgc_code=a,name="R",level="region",region_psgc=a,province_psgc="",town_psgc=""),
           dict(psgc_code=p,name="P",level="province",region_psgc=a,province_psgc=p,town_psgc=""),
           dict(psgc_code=t,name="T",level="municipality",region_psgc=a,province_psgc=p,town_psgc=t),
           dict(psgc_code="0128010001",name="B1",level="barangay",region_psgc=a,province_psgc=p,town_psgc=t),
           dict(psgc_code="0128010002",name="B2",level="barangay",region_psgc=a,province_psgc=p,town_psgc=t)]
    ds._write_csv(folder/"units.csv",ds.UNIT_COLUMNS,units)
    ds._write_csv(folder/"barangay_population.csv",ds.POP_COLUMNS,
                  [dict(psgc_code="0128010001",population=8),dict(psgc_code="0128010002",population=12)])
    return a,p,t


def test_csv_only_requires_no_raw_provider_files(tmp_path, monkeypatch):
    rows_fixture(tmp_path)
    repo=tmp_path/"unrelated-empty-repository";repo.mkdir()
    roster,rows,meta=ds.load_plain_dataset(tmp_path)
    assert meta["population_total"]==20 and len(rows)==2
    assert not (tmp_path/"PSA.xlsx").exists()
    assert roster["0128010001"]["town_psgc"]=="0128010000"


def test_replacing_population_changes_digest_and_weights(tmp_path):
    rows_fixture(tmp_path)
    _,rows,meta=ds.load_plain_dataset(tmp_path)
    fp=meta["data_file_sha256"]
    (tmp_path/"barangay_population.csv").write_text(
        "psgc_code,population\n0128010001,9\n0128010002,11\n",encoding="utf-8")
    _,rows2,meta2=ds.load_plain_dataset(tmp_path)
    assert sum(r["population"] for r in rows2)==20
    assert fp!=meta2["data_file_sha256"]
    assert rows2[0]["population"]==9


def test_explicit_parent_overrides_code_prefix(tmp_path):
    a,p,t=rows_fixture(tmp_path)
    # Parent mapping is a field, not an inference from the numerical PSGC prefix.
    text=(tmp_path/"units.csv").read_text(encoding="utf-8")
    text=text.replace("0128010002,B2,barangay,0100000000,0128000000,0128010000",
                      "0128010002,B2,barangay,0100000000,,0128010000")
    (tmp_path/"units.csv").write_text(text,encoding="utf-8")
    with pytest.raises(ValueError,match="inconsistent"):
        ds.load_plain_dataset(tmp_path)


def test_unknown_nonbar_population_rejected(tmp_path):
    rows_fixture(tmp_path)
    (tmp_path/"barangay_population.csv").write_text(
        "psgc_code,population\n0128010001,8\n0128010000,100\n",encoding="utf-8")
    with pytest.raises(ValueError,match="not a known barangay"):
        ds.load_plain_dataset(tmp_path)


def test_malformed_numeric_population_rejected(tmp_path):
    rows_fixture(tmp_path)
    (tmp_path/"barangay_population.csv").write_text(
        "psgc_code,population\n0128010001,8.2\n0128010002,12\n",encoding="utf-8")
    with pytest.raises(ValueError,match="nonnegative integer"):
        ds.load_plain_dataset(tmp_path)


def test_one_time_default_export_and_independence_from_legacy_source(tmp_path, monkeypatch):
    repo=tmp_path/"repo"; (repo/"reference"/"psgc").mkdir(parents=True)
    raw=[
        dict(psgc_code="0100000000",name="Region",geographic_level="Reg"),
        dict(psgc_code="0128000000",name="Province",geographic_level="Prov"),
        dict(psgc_code="0128010000",name="Town",geographic_level="Mun"),
        dict(psgc_code="0128010001",name="One",geographic_level="Bgy",population_2024="8"),
        dict(psgc_code="0128010002",name="Two",geographic_level="Bgy",population_2024="12"),
    ]
    legacy=repo/"reference"/"psgc"/"psgc_q2_2026_all.jsonl.gz"
    with gzip.open(legacy,"wt",encoding="utf-8") as fd:
        fd.write("".join(json.dumps(x)+"\n" for x in raw))
    monkeypatch.setattr(ds,"_expected_default_counts",(2,1,1,1),raising=False)
    # Accept source-specific counts as a parameter only inside the one-time exporter.
    target=repo/"reference"/"population"/"default"
    ds.export_bundled_defaults(repo,target,expected_counts=(2,1,1,1))
    assert (target/"units.csv").exists() and (target/"barangay_population.csv").exists()
    legacy.unlink()
    # Everything after initial export is completely independent of XLSX/JSONL.
    _,rows,meta=ds.load_plain_dataset(target)
    assert sum(r["population"] for r in rows)==20
    ds.export_bundled_defaults(repo,target)  # safe no-op


def test_optional_runner_arguments_work_as_one_command():
    args=pop.parse_args([])
    assert args.level=="all" and args.dataset_dir is None and args.points is None
    custom=pop.parse_args(["--dataset-dir","custom","--level","region","--no-auto-geometry"])
    assert custom.level=="region" and custom.no_auto_geometry


def test_plain_points_take_precedence_and_no_download(tmp_path,monkeypatch):
    data=tmp_path/"dataset";data.mkdir()
    (data/"barangay_points.csv").write_text(
        "psgc_code,lon,lat\n0128010001,121,14\n0128010002,121.1,14.1\n",encoding="utf-8")
    a=geo.discover_points(tmp_path,data,{},auto_geometry=False)
    assert a==data/"barangay_points.csv"


def test_no_geometry_refuses_to_invent_coordinates(tmp_path):
    data=tmp_path/"data";data.mkdir()
    with pytest.raises(FileNotFoundError,match="No barangay coordinates"):
        geo.discover_points(tmp_path,data,{},auto_geometry=False)


def test_actual_ten_digit_psgc_hierarchy_abra_style(tmp_path):
    """Ten-digit PSGC: region 2; province 5; town 7; barangay 10 digits.

    The previous v1 overlay used six digits for municipality/city parents.
    Actual codes such as Boliney 1400102000 would then be mapped to the
    province rather than the municipality, invalidating population weights.
    """
    r="1400000000";p="1400100000";t="1400102000";b="1400102001"
    rows=[dict(psgc_code=r,name="Cordillera",geographic_level="Reg"),
          dict(psgc_code=p,name="Abra",geographic_level="Prov"),
          dict(psgc_code=t,name="Boliney",geographic_level="Mun"),
          dict(psgc_code=b,name="Barangay",geographic_level="Bgy",population_2024="100")]
    root=tmp_path/"repo";src=root/"reference"/"psgc"
    src.mkdir(parents=True)
    with gzip.open(src/"psgc_q2_2026_all.jsonl.gz","wt",encoding="utf-8") as f:
        for row in rows:f.write(json.dumps(row)+"\n")
    output=tmp_path/"normalized"
    ds.export_bundled_defaults(root,output,expected_counts=(1,1,1,1))
    roster,prepared,_=ds.load_plain_dataset(output)
    assert prepared[0]["town_psgc"]==t
    assert prepared[0]["province_psgc"]==p
    assert prepared[0]["region_psgc"]==r


def test_manila_style_submunicipal_district_parent_falls_back_to_city(tmp_path):
    # Actual Q2-2026 PSGC patterns: 1380610004 (Barangay 689) belongs to
    # City of Manila 1380600000, despite a SubMun at 1380610000.
    region="1300000000";city="1380600000";submun="1380610000";bgy="1380610004"
    src=tmp_path/"repo"/"reference"/"psgc"
    src.mkdir(parents=True)
    data=[dict(psgc_code=region,name="NCR",geographic_level="Reg"),
          dict(psgc_code=city,name="City of Manila",geographic_level="City"),
          dict(psgc_code=submun,name="District I",geographic_level="SubMun"),
          dict(psgc_code=bgy,name="Bgy 1",geographic_level="Bgy",population_2024="42")]
    with gzip.open(src/"psgc_q2_2026_all.jsonl.gz","wt",encoding="utf-8") as f:
        for r in data:f.write(json.dumps(r)+"\n")
    dest=tmp_path/"plain"
    ds.export_bundled_defaults(src.parents[1],dest,expected_counts=(1,1,0,1))
    _,rows,_=ds.load_plain_dataset(dest)
    assert rows[0]["town_psgc"]==city
    assert rows[0]["province_psgc"]==""
    assert rows[0]["town_psgc"] == city


def test_stale_provenance_does_not_claim_default_source_after_edit(tmp_path):
    rows_fixture(tmp_path)
    src=tmp_path/"provenance.json"
    src.write_text(json.dumps({"source":"Official original",
        "units_sha256":ds.sha256(tmp_path/"units.csv"),
        "population_sha256":ds.sha256(tmp_path/"barangay_population.csv")}),encoding="utf-8")
    _,_,meta=ds.load_plain_dataset(tmp_path)
    assert meta["source"]["source_note"]=="Official original"
    (tmp_path/"barangay_population.csv").write_text(
        "psgc_code,population\n0128010001,9\n0128010002,11\n",encoding="utf-8")
    _,_,after=ds.load_plain_dataset(tmp_path)
    assert "Modified normalized tables" in after["source"]["source_note"]
    assert after["source"]["source_note_matches_input_hashes"] is False


def test_flat_ocha_crosswalk_export_unique_only(tmp_path):
    src=tmp_path/'repo'/'reference'/'psgc'
    src.mkdir(parents=True)
    records=[
        {'psgc_code':'1380610004','geographic_level':'Bgy','correspondence_code':'133910004'},
        {'psgc_code':'1380610005','geographic_level':'Bgy','correspondence_code':'133910005'},
        {'psgc_code':'1380610006','geographic_level':'Bgy','correspondence_code':'133910005'},
    ]
    with gzip.open(src/'psgc_q2_2026_all.jsonl.gz','wt',encoding='utf-8') as fd:
        for r in records:fd.write(json.dumps(r)+'\n')
    out=tmp_path/'plain'
    target=ds.export_ocha_crosswalk(tmp_path/'repo',out)
    assert target.exists()
    assert target.read_text().splitlines()==[
        'psgc_code,ocha_pcode','1380610004,PH133910004'
    ]
    assert ds.export_ocha_crosswalk(tmp_path/'repo',out)==target
