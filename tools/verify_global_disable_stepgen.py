#!/usr/bin/env python3
"""Check stock M18/M84 against a prime queued during its initial flush.

Uses real C trapq/itersolve/steppersync, EnableTracking, motion_queuing and
unchanged Toolhead methods. Time, sensors and zero feed demand are controlled
fixtures: this is a component regression, not a hardware scheduling replay.
TMC/UART and serial transport timing are not simulated.
"""
import argparse
import ast
from contextlib import nullcontext
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace


def load_toolhead(klipper_path):
    # Toolhead's full import requires pyserial; these unchanged methods do not.
    path = klipper_path / "klippy" / "toolhead.py"
    tree = ast.parse(path.read_text())
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "ToolHead")
    names = {"_advance_move_time", "_calc_print_time", "_process_lookahead",
             "_flush_lookahead", "flush_step_generation", "get_last_move_time",
             "dwell", "_check_pause", "_check_priming_state", "get_kinematics"}
    methods = [n for n in cls.body
               if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in methods} == names, "Missing stock Toolhead method"
    constants = [n for n in tree.body if isinstance(n, ast.Assign)
                 and all(isinstance(t, ast.Name) for t in n.targets)]
    namespace = {}
    exec(compile(ast.Module(body=constants + methods, type_ignores=[]),
                 str(path), "exec"), namespace)
    return type("ToolHead", (), {n: namespace[n] for n in names})


def load_feeder(path):
    spec = importlib.util.spec_from_file_location(
        "klipper_extras._global_disable_feeder_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_case(klipper_path, feeder_module, case, unguarded=False):
    import chelper
    from stepper import MCU_stepper
    from extras.stepper_enable import (
        EnableTracking, StepperEnablePin, PrinterStepperEnable)
    from extras.motion_queuing import PrinterMotionQueuing

    ToolHead = load_toolhead(klipper_path)
    ffi, lib = chelper.get_ffi()
    fd = os.open(os.devnull, os.O_WRONLY)
    sq = lib.serialqueue_alloc(fd, b'f', 0, b'diag')
    mgr = lib.steppersyncmgr_alloc()
    tq = lib.trapq_alloc()
    trapqs = [tq]
    try:
        ss = lib.steppersyncmgr_alloc_steppersync(mgr)
        lib.steppersync_setup_movequeue(ss, sq, 1024)
        se = lib.steppersync_alloc_syncemitter(ss, b'diag', 1)
        lib.steppersync_set_time(ss, 0., 48e6)
        sc = lib.syncemitter_get_stepcompress(se)
        lib.stepcompress_fill(sc, 0, 1200, 1, 2)
        sk = lib.cartesian_stepper_alloc(b'x')
        step_dist = 18.86 / (3200 * 50 / 17)
        lib.itersolve_set_trapq(sk, tq, step_dist)
        lib.syncemitter_set_stepper_kinematics(se, sk)
        clock = SimpleNamespace(now=10.)

        def unexpected_pause(until):
            raise AssertionError("Unexpected reactor pause: %r" % until)

        reactor = SimpleNamespace(
            monotonic=lambda: clock.now, assert_no_pause=nullcontext,
            NOW=0., NEVER=1e30, update_timer=lambda *args: None,
            pause=unexpected_pause)
        objects = {}

        def shutdown(*args):
            raise RuntimeError(args)

        printer = SimpleNamespace(
            lookup_object=lambda name: objects[name],
            send_event=lambda *args: None, invoke_shutdown=shutdown)
        mcu = SimpleNamespace(get_printer=lambda: printer,
                              estimated_print_time=lambda t: t)
        mq = PrinterMotionQueuing.__new__(PrinterMotionQueuing)
        mq.printer, mq.reactor, mq.mcu = printer, reactor, mcu
        mq.flush_callbacks = []
        mq.last_flush_time = mq.last_step_gen_time = 9.
        mq.need_flush_time = mq.need_step_gen_time = 9.
        mq.kin_flush_delay = .001
        mq.clear_history_time = 0.
        mq.can_pause = True
        mq.steppersyncmgr = mgr
        mq.steppersyncmgr_gen_steps = lib.steppersyncmgr_gen_steps
        mq.syncemitter_to_name = {}
        mq.trapqs = [tq]
        mq.trapq_finalize_moves = lib.trapq_finalize_moves
        mq.do_kick_flush_timer = False
        objects["motion_queuing"] = mq
        stepper = MCU_stepper.__new__(MCU_stepper)
        stepper._name = "buffer"
        stepper._mcu = mcu
        stepper._active_callbacks = []
        stepper._itersolve_check_active = lib.itersolve_check_active
        stepper._stepper_kinematics = sk
        stepper._step_dist = step_dist
        stepper._mcu_position_offset = 0.
        toggles = []
        pin = SimpleNamespace(
            set_digital=lambda t, v: toggles.append((round(t, 9), v)))
        enable = EnableTracking(stepper, StepperEnablePin(pin, 0))
        feeder = feeder_module.BufferFeeder.__new__(feeder_module.BufferFeeder)
        feeder.feed_controller = feeder_module.FeedSpeedController(
            lambda *args, **kwargs: feeder._debug_event(*args, **kwargs))
        feeder.pending_stream = feeder_module.PendingStreamLifecycle(feeder)
        feeder.motor = feeder_module.MotorLifecycle(feeder)
        feeder.diagnostics = feeder_module.BufferDiagnostics(feeder)
        feeder.print_session = feeder_module.PrintSessionCoordinator(feeder)
        feeder.legacy_workflow = feeder_module.LegacyFilamentWorkflow(feeder)
        feeder.idle_watchdog = feeder_module.IdleWatchdogCoordinator(feeder)
        feeder.streaming = feeder_module.StreamingCoordinator(feeder)
        feeder.printer, feeder.reactor = printer, reactor
        feeder.stepper, feeder.motion_queuing = stepper, mq
        feeder.trapq, feeder.trapq_append = tq, lib.trapq_append
        feeder.use_flush_callback_bang_bang = True
        feeder._bang_bang_suspended = False
        feeder._continuous_feed = False
        feeder._state = feeder_module.STATE_AUTO
        feeder._stepper_synced_to = None
        feeder._needs_overflow_prime = True
        feeder._stepcompress_primed = False
        feeder._current_move = None
        feeder._last_move_end_time = 9.
        feeder._last_enable_schedule_time = 9.
        feeder._stepper_enable = enable
        feeder._raw_motor_disable = None
        feeder._stepper_disable_in_progress = False
        feeder._global_disable_in_progress = False
        feeder._pending_disable = False
        feeder._pending_remaining_mm = 0.
        feeder._park_full_active = False
        feeder._load_endstop_move = None
        feeder._commanded_pos = 0.
        feeder._feed_distance_accumulator = 0.
        feeder._accumulated_feed_distance = 0.
        feeder._measure_load_active = False
        feeder._high_flow_carry_armed_until = 0.
        feeder._post_full_bias_clamp = False
        feeder.buffer_debug_metrics = False
        feeder._debug_event = lambda *args, **kwargs: None
        feeder._is_hall1_active = lambda context: False
        feeder.max_move_chunk_mm = 45.
        feeder.lead_time = .13
        feeder.feed_speed = 10.
        feeder.accel = 500.
        feeder.idle_anchor_mode = "silent"
        feeder._compute_target_feed_speed = lambda: 0.
        feeder.velocity_tracker = SimpleNamespace(is_ready=lambda: True)
        feeder.sensors = SimpleNamespace(
            hall_overflow=False, hall_full=False, hall_empty=True)
        toolhead = ToolHead.__new__(ToolHead)
        toolhead.printer, toolhead.reactor, toolhead.mcu = printer, reactor, mcu
        toolhead.motion_queuing = mq
        toolhead.print_time = 9.
        toolhead.special_queuing_state = "NeedPrime"
        toolhead.lookahead = SimpleNamespace(
            flush=lambda **kwargs: [], set_flush_time=lambda t: None,
            is_empty=lambda: True)
        toolhead.can_pause = True
        toolhead.kin = SimpleNamespace(clear_homing_state=lambda axes: None)
        objects["toolhead"] = toolhead
        global_enable = PrinterStepperEnable.__new__(PrinterStepperEnable)
        global_enable.printer = printer
        global_enable.enable_lines = {"buffer": enable}
        objects["stepper_enable"] = global_enable
        feeders = [feeder]
        toggle_lists = [toggles]
        if case == "multi":
            # A second real C stepper/queue checks that one module wrapper
            # suppresses both selected owners before stock's first flush.
            other_tq = lib.trapq_alloc()
            trapqs.append(other_tq)
            mq.trapqs.append(other_tq)
            other_se = lib.steppersync_alloc_syncemitter(ss, b'diag2', 1)
            other_sc = lib.syncemitter_get_stepcompress(other_se)
            lib.stepcompress_fill(other_sc, 0, 1200, 3, 4)
            other_sk = lib.cartesian_stepper_alloc(b'x')
            lib.itersolve_set_trapq(other_sk, other_tq, step_dist)
            lib.syncemitter_set_stepper_kinematics(other_se, other_sk)
            other_stepper = MCU_stepper.__new__(MCU_stepper)
            other_stepper.__dict__.update(stepper.__dict__)
            other_stepper._name = "buffer2"
            other_stepper._active_callbacks = []
            other_stepper._stepper_kinematics = other_sk
            other_toggles = []
            other_pin = SimpleNamespace(set_digital=lambda t, v:
                                        other_toggles.append((round(t, 9), v)))
            other_enable = EnableTracking(
                other_stepper, StepperEnablePin(other_pin, 0))
            other = feeder_module.BufferFeeder.__new__(feeder_module.BufferFeeder)
            other.__dict__.update(feeder.__dict__)
            other.feed_controller = feeder_module.FeedSpeedController(
                lambda *args, **kwargs: other._debug_event(*args, **kwargs))
            other.pending_stream = feeder_module.PendingStreamLifecycle(other)
            other.motor = feeder_module.MotorLifecycle(other)
            other.diagnostics = feeder_module.BufferDiagnostics(other)
            other.print_session = feeder_module.PrintSessionCoordinator(other)
            other.legacy_workflow = feeder_module.LegacyFilamentWorkflow(other)
            other.idle_watchdog = feeder_module.IdleWatchdogCoordinator(other)
            other.streaming = feeder_module.StreamingCoordinator(other)
            other.stepper, other.trapq = other_stepper, other_tq
            other._stepper_enable = other_enable
            feeders.append(other)
            toggle_lists.append(other_toggles)
            global_enable.enable_lines["buffer2"] = other_enable
        for owner in feeders:
            installer = getattr(owner, "_install_stepper_disable_guard", None)
            if installer is not None and not unguarded:
                if case == "direct":
                    installer()
                else:
                    installer(global_enable)
            mq.register_flush_callback(owner._on_mcu_flush, can_add_trapq=True)
            if case != "direct":
                # Global stop must disable already enabled motors even when
                # pre-flush submission suppression queues no new prime.
                owner._stepper_enable.motor_enable(9.)
        snapshots = []
        advance = mq._advance_flush_time

        def observe_flush(*args):
            snapshots.append(mq.need_step_gen_time)
            advance(*args)

        mq._advance_flush_time = observe_flush
        # Both stock M18 and M84 register this identical command handler.
        global_enable.cmd_M18(None)
        first_toggles = [list(values) for values in toggle_lists]
        completed_at_disable = mq.last_step_gen_time
        end_times = [owner._current_move["end_time"]
                     if owner._current_move is not None else None
                     for owner in feeders]
        assert snapshots[0] == 9., "Prime was not added after flush snapshot"
        if case == "direct":
            assert end_times[0] > snapshots[0], "Fixture did not queue a later prime"
        clock.now = 10.1
        mq._advance_flush_time(10.5)
        print("%s M18/M84: snapshot=%.3f prime_ends=%r "
              "completed_at_disable=%.3f first=%r final=%r" % (
                  case, snapshots[0], end_times, completed_at_disable,
                  first_toggles, toggle_lists))
        for owner, values, end_time in zip(feeders, toggle_lists, end_times):
            assert [v for _, v in values] == [1, 0], "Stale auto-enable fired"
            assert values == sorted(values), "Enable clock moved backwards"
            if end_time is not None:
                assert completed_at_disable >= end_time, "Global disable skipped steps"
            if case != "direct":
                assert end_time is None, "Global flush submitted a new prime"
                assert not owner._needs_overflow_prime, "Global stop left pending prime"
            assert not owner._stepper_disable_in_progress, "Handle guard leaked"
            assert not owner._global_disable_in_progress, "Module guard leaked"
            assert not owner._stepper_enable.is_motor_enabled(), "Motor reenabled"
    finally:
        # Serial messages must drain before their commandqueue owner is freed.
        lib.serialqueue_exit(sq)
        lib.serialqueue_free(sq)
        lib.steppersyncmgr_free(mgr)
        for allocated in trapqs:
            lib.trapq_free(allocated)
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--klipper-path", type=Path, required=True)
    parser.add_argument("--feeder-path", type=Path,
                        default=Path(__file__).resolve().parents[1]
                        / "klipper_extras" / "buffer_feeder.py")
    parser.add_argument("--unguarded", action="store_true",
                        help="Skip handle guard installation (expected RED)")
    parser.add_argument("--case", choices=("direct", "global", "multi", "all"),
                        default="all")
    args = parser.parse_args()
    repo_root = args.feeder_path.resolve().parents[1]
    sys.path.insert(0, str(repo_root))
    sys.path.insert(0, str(args.klipper_path.resolve() / "klippy"))
    module = load_feeder(args.feeder_path)
    cases = ("direct", "global", "multi") if args.case == "all" else (args.case,)
    for case in cases:
        run_case(args.klipper_path.resolve(), module, case, args.unguarded)
    print("PASS: %d stock-C global M18/M84 regression cases" % len(cases))


if __name__ == "__main__":
    main()
