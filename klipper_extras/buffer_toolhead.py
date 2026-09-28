"""Sensor-controlled toolhead loading and unloading with scoped cleanup."""
from contextlib import contextmanager

from ._buffer_common import (
    STATE_AUTO, STATE_IDLE, STATE_INIT, STATE_JAM, STATE_LOADING_PULL,
    STATE_OVERFLOW, STATE_RUNOUT, STATE_UNLOADING,
)


class ToolheadSensorWorkflow:
    def __init__(self, owner):
        self.owner = owner
        self.active = False

    def _error(self, message):
        name = self.owner.load_endstop_sensor or '(nicht konfiguriert)'
        return self.owner._cmd_error(
            "Druckkopfsensor '%s' (load_endstop_sensor): %s" % (name, message))

    def _present(self):
        try:
            status = self.owner._load_endstop_sensor.get_status(
                self.owner.reactor.monotonic())
            if not status['enabled']:
                raise ValueError('Sensor wurde waehrend des Ablaufs deaktiviert')
            return bool(status['filament_detected'])
        except Exception as exc:
            raise self._error('nicht auswertbar; Sensor-Konfiguration und '
                              'Verkabelung pruefen: %s' % exc)

    def _run(self, script):
        self.owner._gcode_run_script_checked(script, from_command=True)

    @contextmanager
    def _operation(self, gcmd, unload=False):
        o = self.owner
        if self.active:
            raise self._error('LOAD/UNLOAD laeuft bereits')
        if o._stepper_synced_to is not None:
            raise self._error('Buffer bereits synchronisiert; zuerst BUFFER_UNSYNC')
        sensor = o._load_endstop_sensor
        helper = getattr(sensor, 'runout_helper', None)
        if (sensor is None or not callable(getattr(sensor, 'get_status', None))
                or helper is None or not hasattr(helper, 'sensor_enabled')
                or not hasattr(helper, 'min_event_systime')):
            raise self._error('kein kompatibler filament_switch_sensor vorhanden; '
                              'Klipper-Sensor-Section und Namen pruefen. Ohne '
                              'Druckkopfsensor load_endstop_sensor weglassen '
                              'und LOAD_FILAMENT/UNLOAD_FILAMENT verwenden.')
        if not o._startup_grace_done:
            raise self._error('startup grace nicht abgeschlossen; erneut versuchen')
        allowed = {STATE_IDLE, STATE_AUTO, STATE_RUNOUT}
        if unload:
            allowed.update((STATE_INIT, STATE_UNLOADING, STATE_OVERFLOW, STATE_JAM))
        o._check_phase_entry('SENSOR_UNLOAD' if unload else 'SENSOR_LOAD', allowed)
        o._raise_if_locked_out(gcmd, direction=-1 if unload else 1)
        saved = (helper.sensor_enabled, helper.min_event_systime)
        self.active = True
        state_saved = False
        try:
            helper.min_event_systime = o.reactor.NEVER
            helper.sensor_enabled = True
            self._present()
            if o._state not in (STATE_JAM, STATE_OVERFLOW):
                o._set_state(STATE_UNLOADING if unload else STATE_LOADING_PULL)
            # Stop inherited AUTO/pending work before handing over either trapq.
            o._halt_motion()
            o._overflow_interrupted_state = None
            o._overflow_resume_mm = 0.
            self._wait(gcmd, -1 if unload else 1)
            self._run('SAVE_GCODE_STATE NAME=buffer_feeder_op')
            o._macro_state_saved = True
            state_saved = True
            self._run('M83')
            yield
        except Exception:
            o._halt_motion()
            o._overflow_interrupted_state = None
            o._overflow_resume_mm = 0.
            raise
        finally:
            try:
                o._unsync_if_synced()
            finally:
                try:
                    if state_saved and o._macro_state_saved:
                        self._run('RESTORE_GCODE_STATE NAME=buffer_feeder_op MOVE=0')
                        o._macro_state_saved = False
                finally:
                    helper.sensor_enabled, helper.min_event_systime = saved
                    self.active = False
                    if o._state in (STATE_LOADING_PULL, STATE_UNLOADING):
                        o._set_state(STATE_IDLE)

    def _wait(self, gcmd, direction=1):
        o = self.owner
        deadline = o.reactor.monotonic() + o.max_feed_time
        while o._move_in_flight() or o._pending_remaining_mm > 0:
            o._raise_if_locked_out(gcmd, direction=direction)
            if o.reactor.monotonic() >= deadline:
                raise self._error('max_feed_time beim Foerdern erreicht; '
                                  'Filamentweg und Verkabelung pruefen')
            o.reactor.pause(o.reactor.monotonic() + .01)
        o._raise_if_locked_out(gcmd, direction=direction)

    def _heat(self, gcmd):
        o = self.owner
        name = gcmd.get('EXTRUDER', 'extruder')
        active = o.printer.lookup_object('toolhead').get_extruder().get_name()
        if name != active:
            raise self._error('EXTRUDER=%s stimmt nicht mit dem aktiven Extruder '
                              '%s ueberein; gewuenschten Extruder zuerst '
                              'aktivieren' % (name, active))
        extruder = o.printer.lookup_object(name)
        need = max(o.min_temp, extruder.get_heater().min_extrude_temp)
        target = max(need, gcmd.get_float('AUTO_HEAT_TARGET', 250., above=0.))
        window = gcmd.get_float('TEMP_WINDOW', 5., minval=0.)
        if extruder.get_status(o.reactor.monotonic())['temperature'] < need:
            self._run('SET_HEATER_TEMPERATURE HEATER=%s TARGET=%g\n'
                      'TEMPERATURE_WAIT SENSOR=%s MINIMUM=%g'
                      % (name, target, name, max(need, target - window)))
        return name, need

    def load(self, gcmd):
        o = self.owner
        with self._operation(gcmd):
            extruder, _ = self._heat(gcmd)
            if not self._present():
                try:
                    overrun = o._load_to_endstop(
                        gcmd, o.load_fast_distance, o.load_fast_speed,
                        minimum_ratio=0.)
                except Exception as exc:
                    raise self._error('Ankunft nicht bestaetigt: %s; '
                                      'Filamentweg, switch_pin und '
                                      'load_fast_distance pruefen' % exc)
                offset = max(0., o.load_sensor_to_extruder - overrun)
                if overrun > o.load_sensor_to_extruder:
                    o._respond('LOAD: nominaler Sensor-Nachlauf %.2f mm '
                               'uebersteigt load_sensor_to_extruder %.2f mm'
                               % (overrun, o.load_sensor_to_extruder))
                if offset:
                    o._raise_if_locked_out(gcmd)
                    o._set_state(STATE_LOADING_PULL)
                    o._enable_stepper()
                    o._submit_move(offset, o.load_slow_speed,
                                   submit_chunk_cap=min(1., o.interrupt_chunk_mm,
                                                        o.max_move_chunk_mm))
                    self._wait(gcmd)
            if not self._present():
                raise self._error('Signal vor Extruder-Sync verloren; '
                                  'Filamentweg und Verkabelung pruefen')
            o._raise_if_locked_out(gcmd)
            o._set_state(STATE_LOADING_PULL)
            o.sync.sync_to_extruder(extruder)
            # Completed short moves keep HALT/HALL/JAM checks responsive.
            remaining = o.load_slow_distance
            deadline = o.reactor.monotonic() + o.max_feed_time
            while remaining > 1.e-8:
                o._raise_if_locked_out(gcmd)
                if not self._present():
                    raise self._error('Signal beim synchronen Laden verloren; '
                                      'Filamentweg und Verkabelung pruefen')
                if o.reactor.monotonic() >= deadline:
                    raise self._error('max_feed_time beim synchronen Laden erreicht')
                chunk = min(1., remaining)
                self._run('G1 E%g F%g\nM400' % (chunk, o.load_slow_speed * 60.))
                remaining -= chunk
            o._raise_if_locked_out(gcmd)
            if not self._present():
                raise self._error('Signal nach synchronem Laden verloren; '
                                  'Filamentweg und Verkabelung pruefen')
        self._run('BUFFER_AUTO_ON_IF_READY BUFFER=%s' % o.name)

    def unload(self, gcmd):
        o = self.owner
        recovery = o._state in (STATE_JAM, STATE_OVERFLOW)
        with self._operation(gcmd, unload=True):
            if self._present():
                extruder, need = self._heat(gcmd)
                o._raise_if_locked_out(gcmd, direction=-1)
                o.sync.sync_to_extruder(extruder)
                limit = gcmd.get_float('SYNC_DIST', o.unload_sync_distance, above=0.)
                retracted = 0.
                elapsed = 0.

                def move(distance, speed):
                    nonlocal retracted, elapsed
                    while abs(distance) > 1.e-8:
                        o._raise_if_locked_out(gcmd, direction=-1)
                        if not self._present():
                            return False
                        if elapsed >= o.max_feed_time:
                            raise self._error('max_feed_time beim Entladen erreicht; '
                                              'Filamentweg und switch_pin pruefen')
                        chunk = min(1., abs(distance))
                        if distance < 0:
                            chunk = min(chunk, limit - retracted)
                            if chunk <= 1.e-8:
                                raise self._error('Sensor nach SYNC_DIST=%g mm '
                                                  'Rueckzug noch belegt; '
                                                  'Filamentweg, switch_pin/Polaritaet '
                                                  'und Verkabelung pruefen' % limit)
                            chunk = -chunk
                            retracted -= chunk
                        else:
                            o._raise_if_locked_out(gcmd)
                        started = o.reactor.monotonic()
                        self._run('G1 E%g F%g\nM400' % (chunk, speed * 60.))
                        elapsed += o.reactor.monotonic() - started
                        distance -= chunk
                    o._raise_if_locked_out(gcmd, direction=-1)
                    return self._present()

                cycles = gcmd.get_int('TIP_CYCLES', 6, minval=0)
                if recovery:
                    cycles = 0
                speed = gcmd.get_float('TIP_SPEED', 20., above=0.)
                for _ in range(cycles):
                    if not move(gcmd.get_float('TIP_PUSH', 8., above=0.), speed):
                        break
                    if not move(-gcmd.get_float('TIP_PULL', 14., above=0.), speed):
                        break
                if self._present() and gcmd.get_int('USE_COOLING_MOVE', 1, minval=0, maxval=1):
                    cool = max(need, gcmd.get_float('COOL_TEMP', 170., above=0.))
                    maximum = max(cool + 1., gcmd.get_float('COOL_TEMP_MAX', cool + 10., above=0.))
                    self._run('SET_HEATER_TEMPERATURE HEATER=%s TARGET=%g\n'
                              'TEMPERATURE_WAIT SENSOR=%s MINIMUM=%g MAXIMUM=%g'
                              % (extruder, cool, extruder, need, maximum))
                if self._present():
                    move(-gcmd.get_float('TIP_FINAL_RETRACT', 50., above=0.),
                         gcmd.get_float('TIP_FINAL_SPEED', 50., above=0.))
                if self._present():
                    move(-limit, gcmd.get_float('FAST_SPD', o.unload_fast_speed, above=0.))
                o._unsync_if_synced()
            o._raise_if_locked_out(gcmd, direction=-1)
            self._run('BUFFER_UNLOAD_PHASE3 BUFFER=%s MAX_DISTANCE=%g SPEED=%g'
                      % (o.name, gcmd.get_float('MAX_DISTANCE', o.unload_fast_max, above=0.),
                         o.unload_phase3_speed))
        o._respond('UNLOAD abgeschlossen (Druckkopfsensor frei)')
