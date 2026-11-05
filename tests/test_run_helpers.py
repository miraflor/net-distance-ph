"""Input validation and stale-file detection in run.py."""

import json

import numpy as np
import pytest

import config
import run


def test_float_id_column_is_rejected():
    # read_dbf returns floats for a numeric field with a blank; str() of those
    # values would give codes such as '1380600000.0' and 'nan'.
    with pytest.raises(SystemExit, match="text"):
        run._validated_ids(np.array([1380600000.0, np.nan]), "PSGC")


def test_empty_and_duplicate_ids_are_rejected():
    with pytest.raises(SystemExit, match="empty"):
        run._validated_ids(np.array(["0102801000", " "], dtype=object), "PSGC")
    with pytest.raises(SystemExit, match="Duplicate"):
        run._validated_ids(np.array(["0102801000", "0102801000"], dtype=object), "PSGC")


def test_text_ids_are_kept_exactly():
    ids = run._validated_ids(np.array([" 0102801000", "PH0102801"], dtype=object), "PSGC")
    assert ids == ["0102801000", "PH0102801"]


def test_name_column_never_reuses_the_id_column():
    fields = [("MUN_CODE", "C", 10, 0), ("MUN_NAME", "C", 40, 0)]
    id_col = run._pick_column(fields, run.ID_PAT)
    assert id_col == "MUN_CODE"
    assert run._pick_column(fields, run.NAME_PAT, 1, exclude=(id_col,)) == "MUN_NAME"


def test_changed_stage1_setting_is_detected(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORK", tmp_path)
    stub = {"fingerprint": "test-input-fingerprint"}
    monkeypatch.setattr(run, "_input_manifest", lambda: stub)
    np.savez(tmp_path / "sources.npz",
             stage1_config=np.array(json.dumps(run._stage1_config(), sort_keys=True)),
             input_manifest=np.array(json.dumps(stub)))
    run._require_stage1_current()        # unchanged: passes

    monkeypatch.setattr(config, "FERRY_COST_MULTIPLIER", 3.0)
    with pytest.raises(SystemExit, match="FERRY_COST_MULTIPLIER"):
        run._require_stage1_current()


def test_sources_without_stage1_record_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORK", tmp_path)
    np.savez(tmp_path / "sources.npz", ids=np.array(["a"]))
    with pytest.raises(SystemExit, match="run.py graph"):
        run._require_stage1_current()
