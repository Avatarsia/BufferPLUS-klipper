class FakeReactor:
    NOW = 0.0
    NEVER = float("inf")
    MONOTONIC_STEP = 0.001

    def __init__(self):
        self.now = 0.0
        self.timers = []
        self.callback_registrations = []
        self.update_timer_calls = []

    def register_timer(self, callback, when=None):
        timer = {"callback": callback, "when": when}
        self.timers.append(timer)
        return timer

    def register_callback(self, callback, when=None):
        registration = {"callback": callback, "when": when}
        self.callback_registrations.append(registration)
        return registration

    def unregister_timer(self, timer):
        if timer in self.timers:
            self.timers.remove(timer)

    def update_timer(self, timer, when):
        timer["when"] = when
        self.update_timer_calls.append((timer, when))

    def monotonic(self):
        current = self.now
        self.now = round(self.now + self.MONOTONIC_STEP, 6)
        return current

    def pause(self, when):
        self.now = max(self.now, when)
        return self.now


class FakeGCode:
    def __init__(self):
        self.commands = {}
        self.info_messages = []
        self.script_invocations = []
        self.scripts = self.script_invocations

    def register_command(self, name, handler, desc=None):
        self.commands[name] = {
            "handler": handler,
            "desc": desc,
        }

    def respond_info(self, message):
        self.info_messages.append(message)

    def run_script(self, script):
        self.script_invocations.append(("run_script", script))

    def run_script_from_command(self, script):
        self.script_invocations.append(("run_script_from_command", script))

    def error(self, message):
        return RuntimeError(message)


class FakeButtons:
    def __init__(self):
        self.registrations = []
        self.callbacks_by_pin = {}
        self.trigger_calls = []
        self._next_eventtime = 0.0

    def register_buttons(self, pins, callback):
        self.registrations.append((tuple(pins), callback))
        for pin in pins:
            self.callbacks_by_pin.setdefault(pin, []).append(callback)

    def trigger_pin(self, pin_name, raw_state):
        callbacks = self.callbacks_by_pin.get(pin_name)
        if not callbacks:
            raise KeyError(pin_name)
        eventtime = self._next_eventtime
        self._next_eventtime = round(self._next_eventtime + 0.001, 6)
        self.trigger_calls.append((pin_name, raw_state, eventtime))
        for callback in callbacks:
            callback(eventtime, raw_state)
        return eventtime


class FakeMotionQueuing:
    def __init__(self):
        self.trapqs = []
        self.append_calls = []
        self.scan_window_checks = 0
        self.note_mcu_movequeue_activity_calls = []
        self.activity = self.note_mcu_movequeue_activity_calls

    def allocate_trapq(self):
        trapq = object()
        self.trapqs.append(trapq)
        return trapq

    def lookup_trapq_append(self):
        def _append(*args):
            self.append_calls.append(args)
        return _append

    def check_step_generation_scan_windows(self):
        self.scan_window_checks += 1

    def note_mcu_movequeue_activity(self, end_time):
        self.note_mcu_movequeue_activity_calls.append(end_time)


class FakeMCU:
    def estimated_print_time(self, eventtime):
        return eventtime

    def print_time_to_clock(self, print_time):
        return int(print_time * 1000000)


class FakeHeater:
    def __init__(self, temperature=0.0, target=0.0):
        self.temperature = temperature
        self.target = target

    def get_temp(self, eventtime):
        return self.temperature, self.target


class FakeExtruder:
    def __init__(self):
        self.trapq = object()
        self.heater = FakeHeater()

    def get_trapq(self):
        return self.trapq

    def get_heater(self):
        return self.heater


class FakeToolhead:
    def __init__(self):
        self.last_move_time = 0.0
        self.flush_calls = 0

    def get_last_move_time(self):
        return self.last_move_time

    def flush_step_generation(self):
        self.flush_calls += 1


class FakePrintStats:
    def __init__(self, state="standby", filament_used=0.0):
        self.state = state
        self.filament_used = filament_used

    def get_status(self, eventtime):
        return {
            "state": self.state,
            "filament_used": self.filament_used,
        }


class FakeStepperEnableHandle:
    def __init__(self):
        self.enables = []
        self.disables = []

    def motor_enable(self, print_time):
        self.enables.append(print_time)

    def motor_disable(self, print_time):
        self.disables.append(print_time)


class FakeStepperEnable:
    def __init__(self):
        self.handles = {}

    def lookup_enable(self, name):
        return self.handles.setdefault(name, FakeStepperEnableHandle())


class FakePrinterStepper:
    def __init__(self, config, units_in_radians=False):
        self.config = config
        self.units_in_radians = units_in_radians
        self.name = config.get_name()
        self.trapq = None
        self.trapq_sets = []
        self.position = (0.0, 0.0, 0.0)
        self.itersolve = None
        self.mcu = FakeMCU()

    def setup_itersolve(self, alloc_name, axis):
        self.itersolve = (alloc_name, axis)

    def set_trapq(self, trapq):
        self.trapq = trapq
        self.trapq_sets.append(trapq)

    @property
    def last_trapq_set(self):
        if not self.trapq_sets:
            return None
        return self.trapq_sets[-1]

    def set_position(self, position):
        self.position = position

    def get_name(self):
        return self.name

    def get_mcu(self):
        return self.mcu


class FakePrinter:
    def __init__(self):
        self.reactor = FakeReactor()
        self.objects = {
            "gcode": FakeGCode(),
            "buttons": FakeButtons(),
            "motion_queuing": FakeMotionQueuing(),
            "toolhead": FakeToolhead(),
            "extruder": FakeExtruder(),
            "print_stats": FakePrintStats(),
            "stepper_enable": FakeStepperEnable(),
        }
        self.event_handlers = {}

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default=None):
        if name in self.objects:
            return self.objects[name]
        if default is not None:
            return default
        raise KeyError(name)

    def load_object(self, config, name):
        return self.lookup_object(name)

    def register_event_handler(self, event, handler):
        self.event_handlers.setdefault(event, []).append(handler)


class FakeConfig:
    DEFAULT_VALUES = {
        "hall_empty_pin": "fake:hall_empty",
        "hall_full_pin": "fake:hall_full",
        "hall_overflow_pin": "fake:hall_overflow",
        "entrance_pin": "fake:entrance",
        "feed_button_pin": "fake:feed_button",
        "retract_button_pin": "fake:retract_button",
    }

    def __init__(self, printer=None, values=None, name="buffer_feeder mellow"):
        self.printer = printer or FakePrinter()
        self.values = dict(self.DEFAULT_VALUES)
        if values:
            self.values.update(values)
        self.name = name

    def get_printer(self):
        return self.printer

    def get_name(self):
        return self.name

    def get(self, key, default=None):
        return self.values.get(key, default)

    def getfloat(self, key, default=None, **kwargs):
        return float(self.values.get(key, default))

    def getint(self, key, default=None, **kwargs):
        return int(self.values.get(key, default))

    def getboolean(self, key, default=None, **kwargs):
        # Klipper-konform: "0"/"false"/"no" → False, "1"/"true"/"yes" → True.
        # Naive bool("0") wuerde True liefern (truthy) — review-finding fix.
        v = self.values.get(key, default)
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ('0', 'false', 'no', 'off', ''):
                return False
            if s in ('1', 'true', 'yes', 'on'):
                return True
            raise ValueError("FakeConfig.getboolean: invalid '%s'" % v)
        return bool(v)
