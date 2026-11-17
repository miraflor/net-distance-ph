"""No-network source priority, lineage and old-to-new code tests."""
from __future__ import annotations
import csv
import json
import pytest
from net_distance_ph import population_geometry as geo


def test_ocha_pcode_normalization():
    assert geo._normalized_ocha_pcode('ph133910004')=='PH133910004'
    assert geo._normalized_ocha_pcode(' 133910004 ')=='PH133910004'
    assert geo._normalized_ocha_pcode('malformed') is None


def test_explicit_correspondence_without_fuzzy_name_matching(tmp_path):
    (tmp_path/'geometry_crosswalk.csv').write_text('psgc_code,ocha_pcode\n1380610004,PH133910004\n',encoding='utf8')
    forward,checksum=geo._crosswalk(tmp_path,{'1380610004','1380610005'})
    assert 'PH133910004' in forward['1380610004']
    assert 'PH133910004' not in forward['1380610005']
    assert len(checksum)==64


def test_ambiguous_historical_code_does_not_split_old_polygon(tmp_path):
    (tmp_path/'geometry_crosswalk.csv').write_text('psgc_code,ocha_pcode\n1380610004,PH133910004\n1380610005,PH133910004\n')
    forward,_=geo._crosswalk(tmp_path,{'1380610004','1380610005'})
    assert all('PH133910004' not in x for x in forward.values())


def test_source_priority_and_audited_fallback(tmp_path,monkeypatch):
    workspace=tmp_path/'workspace'; dataset=tmp_path/'dataset';dataset.mkdir()
    one='1380610004';two='1380610005'
    roster={one:{'level':'barangay'},two:{'level':'barangay'}}
    calls=[]
    def ocha(ws,ds,codes):
        calls.append('OCHA')
        return {one:(120.1,14.2)},{'source':'OCHA','matched':1}
    def faeldon(ws,codes):
        calls.append('faeldon')
        assert codes=={two}
        return {two:(120.2,14.3)},{'source':'faeldon','matched':1}
    monkeypatch.setattr(geo,'_source_ocha',ocha)
    monkeypatch.setattr(geo,'_source_faeldon',faeldon)
    out=geo.discover_points(workspace,dataset,roster,source='auto')
    assert calls==['OCHA','faeldon']
    with out.open(newline='',encoding='utf8') as f:rows=list(csv.DictReader(f))
    assert {r['psgc_code'] for r in rows}=={one,two}
    audit=out.with_name(out.stem+'_source_audit.csv')
    with audit.open(newline='',encoding='utf8') as f:a=list(csv.DictReader(f))
    assert {r['psgc_code']:r['geometry_source'] for r in a}=={one:'OCHA',two:'faeldon'}
    meta=json.loads(out.with_suffix('.json').read_text())
    assert meta['geometry_source_policy']=='auto'
    calls.clear()
    assert geo.discover_points(workspace,dataset,roster,source='auto')==out
    assert calls==[]


def test_faeldon_source_does_not_call_ocha(tmp_path,monkeypatch):
    called=[]
    def fallback(ws,codes):
        called.append('faeldon')
        return {'1380610004':(120,14)},{'source':'faeldon'}
    monkeypatch.setattr(geo,'_source_faeldon',fallback)
    monkeypatch.setattr(geo,'_source_ocha',lambda *a:pytest.fail('OCHA must not be used'))
    out=geo.discover_points(tmp_path/'ws',tmp_path/'data',{'1380610004':{'level':'barangay'}},source='faeldon')
    assert out.exists() and called==['faeldon']


def test_network_or_hdx_error_uses_visible_fallback(tmp_path,monkeypatch,capsys):
    monkeypatch.setattr(geo,'_source_ocha',lambda *a: (_ for _ in ()).throw(OSError('HDX offline')))
    monkeypatch.setattr(geo,'_source_faeldon',lambda ws,codes:({'1380610004':(120,14)},{'source':'faeldon'}))
    out=geo.discover_points(tmp_path/'ws',tmp_path/'data',{'1380610004':{'level':'barangay'}},source='auto')
    assert 'FALLBACK' in capsys.readouterr().out
    assert 'HDX offline' in out.with_suffix('.json').read_text()


def test_missing_geometry_never_replaced_by_town_centroid(tmp_path,monkeypatch):
    monkeypatch.setattr(geo,'_source_ocha',lambda *a:({},{}))
    monkeypatch.setattr(geo,'_source_faeldon',lambda *a:({},{}))
    # A *partial* geometry result is valid and never replaced by a town centroid.
    # All-missing remains an error because no representative model is possible.
    with pytest.raises(ValueError,match='No barangay geometry was matched'):
        geo.discover_points(tmp_path/'ws',tmp_path/'data',{'1380610004':{'level':'barangay'}},source='auto')


def test_points_explicit_override_has_precedence(tmp_path,monkeypatch):
    user=tmp_path/'custom.csv';user.write_text('psgc_code,lon,lat\n1380610004,120,14\n')
    monkeypatch.setattr(geo,'_source_ocha',lambda *a:pytest.fail('Should not fetch'))
    assert geo.discover_points(tmp_path/'ws',tmp_path/'data',{},requested=user)==user


def test_source_specific_caches_dont_mix(tmp_path,monkeypatch):
    roster={'1380610004':{'level':'barangay'}}
    monkeypatch.setattr(geo,'_source_ocha',lambda *a:({'1380610004':(120,14)},{'source':'OCHA'}))
    monkeypatch.setattr(geo,'_source_faeldon',lambda *a:({'1380610004':(121,15)},{'source':'faeldon'}))
    a=geo.discover_points(tmp_path/'ws',tmp_path/'data',roster,source='auto')
    b=geo.discover_points(tmp_path/'ws',tmp_path/'data',roster,source='faeldon')
    assert a!=b and a.read_bytes()!=b.read_bytes()


def test_crosswalk_change_invalidates_cached_geometry(tmp_path,monkeypatch):
    ws=tmp_path/'ws';ds=tmp_path/'ds';ds.mkdir()
    roster={'1380610004':{'level':'barangay'}}
    monkeypatch.setattr(geo,'_source_ocha',lambda *a:({'1380610004':(120,14)},{'source':'OCHA'}))
    a=geo.discover_points(ws,ds,roster,source='ocha')
    (ds/'geometry_crosswalk.csv').write_text('psgc_code,ocha_pcode\n1380610004,PH133910004\n')
    b=geo.discover_points(ws,ds,roster,source='ocha')
    assert a != b


def test_default_source_strictly_ocha_and_cli_agree(tmp_path,monkeypatch):
    from net_distance_ph.population_weighted import parse_args
    assert parse_args([]).geometry_source == 'ocha'
    calls=[]
    def ocha(ws,ds,codes):
        calls.append('OCHA')
        return {'1380610004':(120,14)},{'source':'OCHA'}
    monkeypatch.setattr(geo,'_source_ocha',ocha)
    monkeypatch.setattr(geo,'_source_faeldon',lambda *a:pytest.fail('No implicit faeldon fallback'))
    point=geo.discover_points(tmp_path/'ws',tmp_path/'data',{'1380610004':{'level':'barangay'}})
    assert calls==['OCHA']
    manifest=json.loads(point.with_suffix('.json').read_text())
    assert manifest['geometry_source_policy']=='ocha'


def test_default_ocha_does_not_fall_back_on_unavailable(tmp_path,monkeypatch):
    monkeypatch.setattr(geo,'_source_ocha',lambda *a: (_ for _ in ()).throw(OSError('HDX offline')))
    monkeypatch.setattr(geo,'_source_faeldon',lambda *a:pytest.fail('No implicit faeldon fallback'))
    with pytest.raises(OSError,match='HDX offline'):
        geo.discover_points(tmp_path/'ws',tmp_path/'data',{'1380610004':{'level':'barangay'}})


def test_default_ocha_does_not_fall_back_on_unmatched_codes(tmp_path,monkeypatch):
    monkeypatch.setattr(geo,'_source_ocha',lambda *a:({},{}))
    monkeypatch.setattr(geo,'_source_faeldon',lambda *a:pytest.fail('No implicit faeldon fallback'))
    with pytest.raises(ValueError,match='No barangay geometry was matched'):
        geo.discover_points(tmp_path/'ws',tmp_path/'data',{'1380610004':{'level':'barangay'}})


def test_default_ocha_skips_partial_missing_with_audit(tmp_path, monkeypatch, capsys):
    one, two = '1380610004', '1380610005'
    called=[]
    def upstream(ws, ds, needed):
        called.append('OCHA')
        assert needed == {one, two}
        return {one: (120.99, 14.59)}, {'source': 'OCHA'}
    monkeypatch.setattr(geo, '_source_ocha', upstream)
    monkeypatch.setattr(geo, '_source_faeldon',
        lambda *a: pytest.fail('OCHA-only default must not fetch faeldon'))
    ws=tmp_path/'ws'
    ds=tmp_path/'ds'
    out=geo.discover_points(ws,ds,{one:{'level':'barangay'},two:{'level':'barangay'}})
    assert called == ['OCHA']
    with out.open(newline='',encoding='utf8') as f:
        rows=list(csv.DictReader(f))
    assert [r['psgc_code'] for r in rows] == [one]
    audit=out.with_name(out.stem+'_source_audit.csv')
    with audit.open(newline='',encoding='utf8') as f:
        audit_rows={r['psgc_code']:r for r in csv.DictReader(f)}
    assert audit_rows[one]['geometry_source']=='OCHA'
    assert audit_rows[two]['geometry_status']=='missing_geometry'
    meta=json.loads(out.with_suffix('.json').read_text())
    assert meta['n_unmatched']==1
    assert meta['unmatched_codes']==[two]
    assert 'GEOMETRY COVERAGE WARNING' in capsys.readouterr().out
    assert geo.discover_points(ws,ds,{one:{'level':'barangay'},two:{'level':'barangay'}})==out
    assert called == ['OCHA']


def test_missing_geometry_with_explicit_auto_also_audited(tmp_path,monkeypatch):
    one,two,three='1380610004','1380610005','1380610006'
    monkeypatch.setattr(geo,'_source_ocha',
        lambda *a: ({one:(120,14)}, {'source':'OCHA'}))
    monkeypatch.setattr(geo,'_source_faeldon',
        lambda ws,codes: ({two:(121,15)}, {'source':'faeldon'}))
    out=geo.discover_points(tmp_path/'ws',tmp_path/'ds',
         {k:{'level':'barangay'} for k in (one,two,three)},source='auto')
    with out.open(newline='',encoding='utf8') as f: rows=list(csv.DictReader(f))
    assert {r['psgc_code'] for r in rows}=={one,two}
    meta=json.loads(out.with_suffix('.json').read_text())
    assert meta['unmatched_codes']==[three]
