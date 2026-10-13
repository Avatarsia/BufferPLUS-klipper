"""Pending-stream operations; live state remains on BufferFeeder."""

from enum import Enum


class SubmitResult(Enum):
    QUEUED = 'queued'
    SKIPPED = 'skipped'


class PendingStreamLifecycle:
    def __init__(self, owner):
        self.owner = owner

    def start(self, remaining_mm, direction, speed, chunk_cap):
        self.finish()
        if remaining_mm > 0.:
            owner = self.owner
            owner._pending_remaining_mm = remaining_mm
            owner._pending_direction = direction
            owner._pending_speed = speed
            owner._pending_submit_chunk_cap = chunk_cap

    def advance(self, queued_mm, speed):
        owner = self.owner
        owner._pending_speed = speed
        owner._pending_remaining_mm = max(
            0., owner._pending_remaining_mm - queued_mm)
        if owner._pending_remaining_mm <= 0.:
            self.finish()

    def finish(self):
        # Queued moves drain; motor, continuous-feed and fault flags are
        # caller-owned. Retain direction/speed for overflow snapshots.
        owner = self.owner
        owner._pending_remaining_mm = 0.
        owner._pending_submit_chunk_cap = None
        owner._park_full_active = False

    def discard_overflow_resume(self, *, clear_interrupted_state=False,
                                clear_motion_details=False, clear_follow=False):
        # Snapshot lifetime is independent of the active pending stream.
        owner = self.owner
        owner._overflow_resume_mm = 0.
        if clear_interrupted_state:
            owner._overflow_interrupted_state = None
        if clear_motion_details:
            owner._overflow_resume_dir = 0
            owner._overflow_resume_spd = 0.
        if clear_follow:
            owner._overflow_interrupted_follow = False
