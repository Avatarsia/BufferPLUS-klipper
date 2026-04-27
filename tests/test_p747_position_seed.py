"""P7-47 — Position seeding in sync_to_extruder (Issue #16 root cause).

Hardware-Test #4 (klippy.log.test4.txt 2026-04-26) showed that even
with the P7-46 anchor-step in place, BUFFER_SYNC_TO_EXTRUDER for the
UNLOAD-tip-forming sequence crashed with:

  stepcompress o=0 i=0 c=14 a=0: Invalid sequence

3rd-party root-cause analysis (verified against Klipper-mainline
klippy/kinematics/extruder.py:75) identified the real bug:

  Mainline ExtruderStepper.sync_to_extruder:
    self.stepper.set_position([extruder.last_position, 0., 0.])

  Our SyncCoordinator.sync_to_extruder (pre-P7-47):
    self.stepper.set_position((0., 0., 0.))    ← BUG

When the extruder is at e.g. 180mm cumulative (after LOAD Phase 3/3)
and we sync mellow-stepper to its trapq with commanded_pos=0,
itersolve searches the next move's step times relative to a
position-mismatch of 180mm. Every step lands at progress=0 of the
extruder's current move → step_time=0 for all 14 steps → rel_sc=0
→ 'Invalid sequence' on the first flush.

P7-47 fixes this by passing extruder.last_position through to
set_position, matching Klipper-mainline semantics.
"""

from fakes_klipper import FakeConfig, FakePrinter
from klipper_extras import buffer_feeder


def make_feeder():
    printer = FakePrinter()
    config = FakeConfig(printer=printer)
    feeder = buffer_feeder.BufferFeeder(config)
    feeder._startup_grace_done = True
    return printer, feeder


def test_sync_seeds_stepper_with_extruder_last_position():
    """The set_position call must carry extruder.last_position, not
    (0, 0, 0). Verifies the central P7-47 fix."""
    printer, feeder = make_feeder()
    extruder = printer.lookup_object('extruder')
    extruder.last_position = 180.0

    feeder._sync_to_extruder('extruder')

    # FakePrinterStepper records every set_position call with its
    # argument; the most recent should match extruder.last_position.
    assert feeder.stepper.position[0] == 180.0, (
        "P7-47 broken: stepper position seeded with %r, expected "
        "extruder.last_position=180.0" % (feeder.stepper.position,))


def test_sync_position_seed_zero_when_extruder_fresh():
    """At extruder.last_position=0.0 (fresh print, no extrude yet),
    the seed is also 0.0 — no regression for the cold-start case."""
    printer, feeder = make_feeder()
    extruder = printer.lookup_object('extruder')
    extruder.last_position = 0.0

    feeder._sync_to_extruder('extruder')

    assert feeder.stepper.position[0] == 0.0


def test_sync_position_seed_propagates_through_unsync_cycle():
    """LOAD-UNLOAD cycle: sync seeds with last_position, unsync resets
    to 0 (own-trapq has its own zero). The next sync seeds again with
    whatever last_position is at that moment."""
    printer, feeder = make_feeder()
    extruder = printer.lookup_object('extruder')

    extruder.last_position = 180.0
    feeder._sync_to_extruder('extruder')
    assert feeder.stepper.position[0] == 180.0

    feeder._unsync_if_synced()
    # unsync_if_synced resets stepper to (0,0,0) on its own trapq.
    assert feeder.stepper.position == (0., 0., 0.)

    extruder.last_position = 250.0
    feeder._sync_to_extruder('extruder')
    assert feeder.stepper.position[0] == 250.0
