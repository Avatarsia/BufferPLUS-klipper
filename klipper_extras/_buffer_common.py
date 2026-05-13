# Shared constants for the buffer_feeder plugin and its sub-modules.
#
# Lives in a private module (leading underscore) so Klipper's
# load_config_prefix discovery skips it — only buffer_feeder itself
# is a config entry-point.


# ---------------------------------------------------------------------------
# State constants
# ---------------------------------------------------------------------------

STATE_INIT           = "INIT"
STATE_IDLE           = "IDLE"
STATE_INITIAL_GRIP   = "INITIAL_GRIP"
STATE_AUTO           = "AUTO"
STATE_MANUAL_FEED    = "MANUAL_FEED"
STATE_MANUAL_RETRACT = "MANUAL_RETRACT"
STATE_LOADING_PULL   = "LOAD_PHASE_1"
STATE_LOADING_PUSH   = "LOAD_PHASE_3"
STATE_UNLOADING = "UNLOAD_PHASE_3"
STATE_OVERFLOW       = "OVERFLOW"
STATE_RUNOUT         = "RUNOUT"
STATE_JAM            = "JAM"

# States where LOAD/UNLOAD is active — override commands
# (BUFFER_FEED/RETRACT/AUTO_ON/FORCE_BUFFER_FILL) must refuse.
BUSY_PHASE_STATES = {STATE_INITIAL_GRIP,
                     STATE_LOADING_PULL, STATE_LOADING_PUSH,
                     STATE_UNLOADING}

# States where the main_tick continuous-feed chunk-pump is allowed
# to run. In any other state, a stale _continuous_feed must NOT
# cause new chunks to be submitted — otherwise a previously-active
# bang-bang or manual dauerfeed leaks into subsequent phases.
CONTINUOUS_FEED_STATES = {STATE_AUTO, STATE_MANUAL_FEED,
                          STATE_MANUAL_RETRACT, STATE_LOADING_PUSH,
                          STATE_INITIAL_GRIP}

# States where jam-detection watches for HALL dwell anomalies.
JAM_WATCH_STATES = {STATE_AUTO, STATE_LOADING_PUSH}


# ---------------------------------------------------------------------------
# Timing constants
# ---------------------------------------------------------------------------

# Main reactor tick interval (sensor polling, bang-bang decisions).
MAIN_TICK_INTERVAL = 0.02            # 50 Hz
JAM_TICK_INTERVAL  = 1.0             # 1 Hz

# Stable-Tracking Drop-Toleranz: kurze Sensor-Flicker waehrend
# LOAD_PHASE_3 stable-exit tracking werden bis zu dieser Dauer
# toleriert. Sobald der Sensor innerhalb der Toleranz wieder aktiv ist,
# laeuft die Stable-Uhr weiter. Erst nach N Sekunden komplett-aus
# zaehlt das als echter Reset.
STABLE_DROP_GRACE  = 0.5             # s

# Anchor-step Distanz fuer Stepcompress-Cursor-Refresh (boot anchor,
# pre-SYNC REPRIME, post-OVERFLOW prime). 0.05mm = ~250 Steps bei
# 5000 steps/mm — physisch kaum spuerbar.
ANCHOR_NUDGE_MM = 0.05

# Stepcompress-Cursor verfaellt nach CLOCK_DIFF_MAX (~16.7s) auf dem
# MCU. Wenn der Feeder laenger idle war, muss vor dem naechsten Submit
# ein Anchor-Step die Cursor-Lage aktualisieren.
REPRIME_GAP_S = 5.0

# Cap fuer forced_t0 / t0 Lookahead bei _submit_single_trapezoid.
# Submits weiter in der Zukunft als mcu_now + diesen Wert werden
# als stale verworfen.
MAX_T0_LOOKAHEAD_S = 2.0


# ---------------------------------------------------------------------------
# Click / button identifiers
# ---------------------------------------------------------------------------

CLICK_SINGLE = 1
CLICK_DOUBLE = 2
CLICK_TRIPLE = 3

BUTTON_FEED    = "feed"
BUTTON_RETRACT = "retract"
