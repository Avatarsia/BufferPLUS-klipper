class FakeGCmd:
    """Canonical gcmd stub for cmd_* dispatchers."""

    def __init__(self, values=None):
        self.values = {key.upper(): value for key, value in (values or {}).items()}

    def get(self, key, default=None):
        return self.values.get(key.upper(), default)

    def _get_numeric(self, key, default, parser, minval=None, maxval=None,
                     above=None, below=None):
        key = key.upper()
        if key not in self.values:
            return default
        value = parser(self.values[key])
        if ((minval is not None and value < minval)
                or (maxval is not None and value > maxval)
                or (above is not None and value <= above)
                or (below is not None and value >= below)):
            raise ValueError("%s value %s outside permitted bounds" % (key, value))
        return value

    def get_int(self, key, default=None, **kwargs):
        return self._get_numeric(key, default, int, **kwargs)

    def get_float(self, key, default=None, **kwargs):
        return self._get_numeric(key, default, float, **kwargs)


def set_sensor_active(feeder, sensor_name, active):
    """Seed a sensor's stable + raw state in polarity-aware fashion."""
    polarity_flip = feeder._pin_polarity_flip[sensor_name]
    raw = (not active) if polarity_flip else active
    feeder._pin_stable_state[sensor_name] = raw
    feeder._pin_raw_state[sensor_name] = raw


def build_feeder(values=None, *, printer=None, name="buffer_feeder mellow",
                 state=None, grace_done=True, fire_connect=False,
                 print_state=None, config=None):
    """Construct first, then optionally connect; preserve explicit setup policy.

    Sensor states, tracker history and specialized printer objects stay with
    the calling test. Supplied printer/config objects retain their overrides.
    """
    from fakes_klipper import FakeConfig, FakePrinter, FakePrintStats
    from klipper_extras import buffer_feeder

    if config is not None:
        printer = config.get_printer()
    elif printer is None:
        printer = FakePrinter()
    if print_state is not None:
        printer.objects["print_stats"] = FakePrintStats(state=print_state)
    if config is None:
        config = FakeConfig(printer=printer, values=values, name=name)
    feeder = buffer_feeder.BufferFeeder(config)
    if fire_connect:
        printer.fire_event('klippy:connect')
    if grace_done:
        feeder._startup_grace_done = True
    if state is not None:
        feeder._state = state
    return printer, feeder
