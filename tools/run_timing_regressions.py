#!/usr/bin/env python3
"""Run pytest and both real-C timing harnesses in separate Linux processes."""
import argparse
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys


PINNED_KLIPPER_COMMIT = 'ce7002bedf37e938bb483572949f3703ac6476cb'
NEGATIVE_MARKER = 'AssertionError: Stale auto-enable fired'


def run_command(command, cwd=None):
    return subprocess.run(command, cwd=cwd, shell=False, timeout=180,
                          capture_output=True, text=True, encoding='utf-8',
                          errors='replace')


def check_environment(klipper_path):
    missing = []
    if not sys.platform.startswith('linux'):
        missing.append('Linux (or WSL Linux Python)')
    for executable in ('git', 'gcc'):
        if shutil.which(executable) is None:
            missing.append(executable)
    for module in ('cffi', 'pytest'):
        if importlib.util.find_spec(module) is None:
            missing.append('%s in %s' % (module, sys.executable))
    for relative in ('klippy/chelper/__init__.py', 'klippy/chelper/itersolve.c',
                     'klippy/chelper/trapq.c', 'klippy/stepper.py',
                     'klippy/toolhead.py', 'klippy/extras/stepper_enable.py',
                     'klippy/extras/motion_queuing.py'):
        if not (klipper_path / relative).is_file():
            missing.append(str(klipper_path / relative))
    if missing:
        raise RuntimeError('Missing prerequisites: ' + ', '.join(missing))


def check_klipper_source(klipper_path, expected_commit):
    command = ['git', '-C', str(klipper_path)]
    head = run_command(command + ['rev-parse', 'HEAD'])
    if head.returncode:
        raise RuntimeError('Cannot read Klipper commit: ' + head.stderr.strip())
    actual = head.stdout.strip()
    print('Klipper HEAD: %s (expected %s)' % (actual, expected_commit), flush=True)
    if actual != expected_commit:
        raise RuntimeError('Klipper commit mismatch')
    dirty = run_command(command + ['status', '--porcelain', '--untracked-files=no'])
    if dirty.returncode:
        raise RuntimeError('Cannot check Klipper tracked source: ' + dirty.stderr.strip())
    if dirty.stdout.strip():
        raise RuntimeError('Klipper has tracked changes; commit does not identify tested source:\n'
                           + dirty.stdout.rstrip())
    print('Klipper tracked source: clean', flush=True)
    return actual


def run_phase(label, command, cwd, negative=False):
    print('\nRUN: %s' % label, flush=True)
    result = run_command(command, cwd=cwd)
    print(result.stdout, end='', flush=True)
    print(result.stderr, end='', flush=True)
    if negative:
        if result.returncode != 1 or NEGATIVE_MARKER not in result.stderr:
            raise RuntimeError('%s: expected exit 1 and %r; got exit %s'
                               % (label, NEGATIVE_MARKER, result.returncode))
        print('EXPECTED RED: %s (exit 1, stale auto-enable)' % label, flush=True)
    elif result.returncode:
        raise RuntimeError('%s failed (exit %s)' % (label, result.returncode))
    else:
        print('PASS: %s (exit 0)' % label, flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--klipper-path', type=Path, required=True)
    parser.add_argument('--expected-klipper-commit', default=PINNED_KLIPPER_COMMIT,
                        help='Exact clean Klipper HEAD; default is the verified baseline')
    parser.add_argument('--check-unguarded', action='store_true',
                        help='Also require expected RED for direct/global/multi separately')
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    klipper_path = args.klipper_path.resolve()
    try:
        check_environment(klipper_path)
        check_klipper_source(klipper_path, args.expected_klipper_commit)
        run_phase('pytest', [sys.executable, '-m', 'pytest', 'tests/', '-q',
                             '-p', 'no:cacheprovider'], root)
        local = [sys.executable, str(root / 'tools/verify_disable_stepgen.py'),
                 '--klipper-path', str(klipper_path)]
        global_command = [sys.executable, str(root / 'tools/verify_global_disable_stepgen.py'),
                          '--klipper-path', str(klipper_path)]
        run_phase('local C: completed + delayed', local, root)
        run_phase('global C: direct + global + multi', global_command, root)
        if args.check_unguarded:
            for case in ('direct', 'global', 'multi'):
                run_phase('unguarded ' + case,
                          global_command + ['--unguarded', '--case', case],
                          root, negative=True)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        print('FAIL: %s' % exc, file=sys.stderr, flush=True)
        return 1
    print('\nPASS: pytest and 5 stock-C cases%s'
          % ('; 3 expected RED controls' if args.check_unguarded else ''), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
