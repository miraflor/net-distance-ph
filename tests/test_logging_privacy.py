"""Offline tests for compact timezone-free logs; no national data required."""
from __future__ import annotations

import io
import re
import threading

import numpy as np

from net_distance_ph.workspace import _TimestampStream
from net_distance_ph import reduce as reduction
import run

CONSOLE = r'\[\d{2}:\d{2}:\d{2}\] '
ARCHIVE = r'\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] '


def test_console_archive_are_short_unclassified_and_timezone_free():
    display, archive = io.StringIO(), io.StringIO()
    stream = _TimestampStream(display, archive, threading.RLock(), 'INFO')
    stream.write('graph done\nsecond message\n')
    assert re.fullmatch(f'{CONSOLE}graph done\\n{CONSOLE}second message\\n', display.getvalue())
    assert re.fullmatch(f'{ARCHIVE}graph done\\n{ARCHIVE}second message\\n', archive.getvalue())
    for value in (display.getvalue(), archive.getvalue()):
        assert 'INFO' not in value
        assert '+08:00' not in value
        assert 'UTC' not in value
        assert not re.search(r'\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}', value)


def test_stderr_does_not_automatically_become_error():
    display, archive = io.StringIO(), io.StringIO()
    stream = _TimestampStream(display, archive, threading.RLock(), 'ERROR')
    stream.write('ordinary warning from library\nERROR: a real error\n')
    assert re.fullmatch(f'{CONSOLE}ordinary warning from library\\n{CONSOLE}ERROR: a real error\\n', display.getvalue())
    assert re.fullmatch(f'{ARCHIVE}ordinary warning from library\\n{ARCHIVE}ERROR: a real error\\n', archive.getvalue())
    assert display.getvalue().count('ERROR:') == 1


def test_partial_multiline_stream_is_not_reprefixed():
    display, archive = io.StringIO(), io.StringIO()
    stream = _TimestampStream(display, archive, threading.RLock(), 'INFO')
    stream.write('checkpoint ')
    stream.write('saved\nnext\n')
    assert display.getvalue().count('checkpoint saved') == 1
    assert display.getvalue().count('[') == 2
    assert archive.getvalue().count('[') == 2


def test_reduction_secondary_log_has_local_date_without_timezone(tmp_path, capsys):
    target = tmp_path / 'reduce_progress.log'
    emit = run._make_reduce_logger(target)
    emit('phase 1/7 leaf pruning')
    assert capsys.readouterr().out == 'phase 1/7 leaf pruning\n'
    assert re.fullmatch(f'{ARCHIVE}phase 1/7 leaf pruning\\n', target.read_text(encoding='utf-8'))


def test_reduction_passes_have_no_embedded_time_or_timezone():
    messages = []
    u = np.array([0, 1, 2, 3], np.int64)
    v = np.array([1, 2, 3, 4], np.int64)
    w = np.ones(4, dtype=float)
    reduction.reduce_to_core(
        u, v, w, 5, np.array([0, 4]), max_degree=2,
        heartbeat_seconds=0, progress=messages.append,
    )
    phases = [m for m in messages if 'phase ' in m and (' START ' in m or ' DONE ' in m)]
    assert phases
    assert all('[' not in m and ']' not in m for m in phases)
    assert all('UTC' not in m and '+08:00' not in m for m in phases)
    assert any('leaf pruning' in m for m in phases)
    assert all(len(m) < 145 for m in phases)
    assert any('DONE ' in m for m in phases)


def test_no_nested_time_sources_in_reduction():
    from inspect import getsource
    assert '[{_stamp()}]' not in getsource(reduction.reduce_to_core)
    assert '[{_stamp()}]' not in getsource(reduction._Heartbeat)
    assert '[{_stamp()}]' not in getsource(run.stage_reduce)
