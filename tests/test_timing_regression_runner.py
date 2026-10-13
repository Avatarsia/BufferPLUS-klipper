"""The shared runner must not accept a dirty source or an unrelated RED."""
import subprocess
from pathlib import Path

import pytest

from tools import run_timing_regressions as runner


@pytest.mark.parametrize('actual,dirty,message', [
    ('wrong', '', 'commit mismatch'),
    (runner.PINNED_KLIPPER_COMMIT, ' M klippy/stepper.py\n', 'tracked changes'),
    (runner.PINNED_KLIPPER_COMMIT, 'M  klippy/stepper.py\n', 'tracked changes'),
])
def test_source_validation_rejects_mismatch_or_dirty(monkeypatch, actual, dirty, message):
    replies = iter([actual + '\n', dirty])
    monkeypatch.setattr(runner, 'run_command', lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 0, next(replies), ''))
    with pytest.raises(RuntimeError, match=message):
        runner.check_klipper_source(Path('/path with spaces/klipper'),
                                    runner.PINNED_KLIPPER_COMMIT)


def test_source_validation_reads_head_and_tracked_status(monkeypatch):
    calls = []
    replies = iter([runner.PINNED_KLIPPER_COMMIT + '\n', ''])

    def execute(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, next(replies), '')

    monkeypatch.setattr(runner, 'run_command', execute)
    path = Path('/path with spaces/klipper')
    assert runner.check_klipper_source(path, runner.PINNED_KLIPPER_COMMIT) == runner.PINNED_KLIPPER_COMMIT
    assert calls == [
        ['git', '-C', str(path), 'rev-parse', 'HEAD'],
        ['git', '-C', str(path), 'status', '--porcelain', '--untracked-files=no'],
    ]


@pytest.mark.parametrize('code,stderr,accepted', [
    (1, 'AssertionError: Stale auto-enable fired\n', True),
    (0, 'AssertionError: Stale auto-enable fired\n', False),
    (1, 'ModuleNotFoundError: cffi\n', False),
    (-11, 'AssertionError: Stale auto-enable fired\n', False),
])
def test_negative_control_accepts_only_expected_failure(monkeypatch, code, stderr, accepted):
    monkeypatch.setattr(runner, 'run_command', lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], code, '', stderr))
    if accepted:
        runner.run_phase('negative', ['python', 'harness.py'], Path('.'), negative=True)
    else:
        with pytest.raises(RuntimeError):
            runner.run_phase('negative', ['python', 'harness.py'], Path('.'), negative=True)


def test_subprocess_uses_argument_list_and_timeout(monkeypatch):
    captured = []

    def execute(command, **kwargs):
        captured.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, '', '')

    monkeypatch.setattr(runner.subprocess, 'run', execute)
    command = ['python', '/repo with spaces/test.py']
    runner.run_command(command, cwd=Path('/repo with spaces'))
    assert captured[0][0] == command
    assert captured[0][1]['shell'] is False
    assert captured[0][1]['timeout'] == 180


def test_full_runner_starts_six_separate_test_processes(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(runner, 'check_environment', lambda path: None)
    monkeypatch.setattr(runner, 'check_klipper_source', lambda *args: runner.PINNED_KLIPPER_COMMIT)
    monkeypatch.setattr(runner, 'run_phase', lambda *args, **kwargs: calls.append((args, kwargs)))
    assert runner.main(['--klipper-path', str(tmp_path), '--check-unguarded']) == 0
    assert len(calls) == 6
    assert calls[0][0][1][1:3] == ['-m', 'pytest']
    assert 'verify_disable_stepgen.py' in calls[1][0][1][1]
    assert 'verify_global_disable_stepgen.py' in calls[2][0][1][1]
    assert [call[0][1][-1] for call in calls[3:]] == ['direct', 'global', 'multi']
    assert all(call[1]['negative'] for call in calls[3:])
