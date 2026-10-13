#!/usr/bin/env python3
"""Exercise delayed step generation with stock Klipper C and enable callbacks.

Run on Linux with Klipper's Python dependencies installed. No hardware is used.
The delayed flush is injected; this does not reproduce host/MCU scheduling.
"""
import argparse
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace


def load_feeder(path):
    spec = importlib.util.spec_from_file_location(
        "klipper_extras._disable_feeder_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MotionQueue:
    def __init__(self):
        self.last_step_gen_time = 0.
        self.callbacks = []

    def register_flush_callback(self, callback):
        self.callbacks.append(callback)

    def unregister_flush_callback(self, callback):
        self.callbacks.remove(callback)


def run_case(feeder_module, delayed):
    import chelper
    from stepper import MCU_stepper
    from extras.stepper_enable import EnableTracking, StepperEnablePin

    ffi, lib = chelper.get_ffi()
    fd = os.open(os.devnull, os.O_WRONLY)
    sq = lib.serialqueue_alloc(fd, b'f', 0, b'diag')
    mgr = lib.steppersyncmgr_alloc()
    tq = lib.trapq_alloc()
    try:
        ss = lib.steppersyncmgr_alloc_steppersync(mgr)
        lib.steppersync_setup_movequeue(ss, sq, 1024)
        se = lib.steppersync_alloc_syncemitter(ss, b'diag', 1)
        lib.steppersync_set_time(ss, 0., 48e6)
        sc = lib.syncemitter_get_stepcompress(se)
        lib.stepcompress_fill(sc, 0, 1200, 1, 2)
        sk = lib.cartesian_stepper_alloc(b'x')
        lib.itersolve_set_trapq(sk, tq, 18.86 / (3200 * 50 / 17))
        lib.syncemitter_set_stepper_kinematics(se, sk)
        lib.trapq_append(tq, 10., 0., .025, 0., 0., 0., 0.,
                        1., 0., 0., 2., 2., 0.)
        mq = MotionQueue()
        printer = SimpleNamespace(lookup_object=lambda name: mq)
        mcu = SimpleNamespace(get_printer=lambda: printer,
                              estimated_print_time=lambda eventtime: eventtime)
        stepper = MCU_stepper.__new__(MCU_stepper)
        stepper._mcu = mcu
        stepper._active_callbacks = []
        stepper._itersolve_check_active = lib.itersolve_check_active
        stepper._stepper_kinematics = sk
        toggles = []
        pin = SimpleNamespace(set_digital=lambda t, value: toggles.append((t, value)))
        enable = EnableTracking(stepper, StepperEnablePin(pin, 0))
        feeder = feeder_module.BufferFeeder.__new__(feeder_module.BufferFeeder)
        feeder.motor = feeder_module.MotorLifecycle(feeder)
        feeder.diagnostics = feeder_module.BufferDiagnostics(feeder)
        feeder.print_session = feeder_module.PrintSessionCoordinator(feeder)
        feeder.legacy_workflow = feeder_module.LegacyFilamentWorkflow(feeder)
        feeder.idle_watchdog = feeder_module.IdleWatchdogCoordinator(feeder)
        feeder.streaming = feeder_module.StreamingCoordinator(feeder)
        feeder.stepper = stepper
        feeder.printer = printer
        clock = SimpleNamespace(now=10.1)
        feeder.reactor = SimpleNamespace(monotonic=lambda: clock.now)
        feeder.motion_queuing = mq
        feeder._stepper_synced_to = None
        feeder._stepper_enable = enable
        feeder._stepcompress_primed = True
        feeder._pending_disable = False
        feeder._current_move = {"end_time": 10.025}
        feeder._last_move_end_time = 10.025
        feeder._last_enable_schedule_time = 10.
        feeder.lead_time = .05

        def generate(until):
            # Klipper checks activity before advancing the C step generator.
            for callback in tuple(mq.callbacks):
                callback(until, until)
            error = lib.steppersyncmgr_gen_steps(mgr, until, until, 0)
            assert error == ffi.NULL, "C step generation failed"
            mq.last_step_gen_time = until

        generate(10.005 if delayed else 10.3)
        assert toggles == [(10., 1)], toggles
        feeder._schedule_stepper_disable()
        deferred = feeder._pending_disable
        primed = feeder._stepcompress_primed
        first_toggles = list(toggles)
        generate(10.3)
        clock.now = 10.3
        feeder._schedule_stepper_disable()
        generate(10.4)
        label = "delayed" if delayed else "completed"
        print(f"{label}: first={first_toggles}, deferred={deferred}, "
              f"primed={primed}, final={toggles}")
        if delayed:
            assert deferred, "Disable was not deferred behind C step generation"
            assert primed, "Deferred disable invalidated stepcompress priming"
            assert first_toggles == [(10., 1)], "Premature motor disable"
        assert [v for _, v in toggles] == [1, 0], "Stale auto-enable fired"
        assert toggles == sorted(toggles), "Enable clock moved backwards"
        assert not feeder._stepcompress_primed, "Completed disable kept priming"
    finally:
        # Drain serial messages before freeing the manager's commandqueue.
        lib.serialqueue_exit(sq)
        lib.serialqueue_free(sq)
        lib.steppersyncmgr_free(mgr)
        lib.trapq_free(tq)
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--klipper-path", type=Path, required=True)
    parser.add_argument("--feeder-path", type=Path,
                        default=Path(__file__).resolve().parents[1]
                        / "klipper_extras" / "buffer_feeder.py")
    args = parser.parse_args()
    sys.path.insert(0, str(args.feeder_path.resolve().parents[1]))
    sys.path.insert(0, str(args.klipper_path.resolve() / "klippy"))
    module = load_feeder(args.feeder_path)
    run_case(module, delayed=False)
    run_case(module, delayed=True)
    print("PASS: 2 stock-C disable/enable regression cases")


if __name__ == "__main__":
    main()
