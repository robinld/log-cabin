"""HomeKit bridge for the alarm panel: arm, disarm, chime, and every zone.

Presents the panel as a HomeKit Security System (Off, Home = Stay, Away), the
chime as a switch, the entry delay as a contact sensor that is open while the
keypad counts down to the alarm (HomeKit has no such state, and a sensor can
notify), and each zone as a contact, motion or smoke sensor, with
low battery and trouble as its status flags. State comes from the EVL flume's
action server (GET /status) and zones from the log: names
from the keypad, open zones from the latest zone dump, and troubles from the
panel's Contact ID reports (plus the keypad's CHECK and LOBAT screens).

HomeKit never asks for an alarm code, so changes made in the Home app are sent
with the user code stored in SSM (evl/user_code). Without it the bridge is
read-only and the Home app's controls spring back. A change that the panel
hasn't shown within PENDING_S springs back too. HomeKit has no "not ready to
arm" state, so while the panel is disarmed and not ready the Alarm shows Off
with its fault flag raised, and arming springs back without being sent. Home
doesn't display fault flags, so a door sensor the panel has lost contact with
(RF supervision, which also blocks arming) is shown as open: that Home does
show. Motion sensors aren't, since motion would set off automations.

HomeKit finds accessories by Bonjour on the home LAN. The bridge advertises on
a dummy interface (mdns0, set up by install.sh), and home_events.mdns_relay
carries those packets to and from the LAN.
"""

import json
import logging
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request

from pyhap.accessory import Accessory, Bridge
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import CATEGORY_ALARM_SYSTEM, CATEGORY_BRIDGE, CATEGORY_SENSOR, CATEGORY_SWITCH

from . import config
from .flumes.evl import ACTION_PORT
from .mdns_relay import MDNS_IP, wg_address

log = logging.getLogger("homekit")

ACTIONS_URL = f"http://127.0.0.1:{ACTION_PORT}"
PORT = 51827
POLL_S = 2
PENDING_S = 20  # how long the panel gets to show a change sent from HomeKit
TROUBLE_WINDOW_S = 120  # the keypad cycles through its trouble screens well within this

# HomeKit SecuritySystemCurrentState / TargetState values.
STAY, AWAY, NIGHT, DISARMED, TRIGGERED = 0, 1, 2, 3, 4
CURRENT = {"stay": STAY, "away": AWAY, "disarmed": DISARMED, "alarm": TRIGGERED}
TARGET_ACTIONS = {STAY: "stay", AWAY: "away", DISARMED: "disarm"}

# Contact ID event codes (the panel's %03 reports) that set a zone's flags.
FIRE_ALARMS = {f"11{d}" for d in range(10)}
SUPERVISION_LOST = "381"
TROUBLES = {"373", SUPERVISION_LOST, "383"}  # fire trouble, RF supervision lost, tamper
LOW_BATTERY = "384"


def zone_title(name):
    """'1ST UPSTAIRS GARAGE' -> '1st Upstairs Garage'."""
    return re.sub(r"(\d)(St|Nd|Rd|Th)\b", lambda m: m[1] + m[2].lower(), name.title())


def open_zones(body):
    """'%01,<hex>$' -> set of open zone numbers (lowest bit of byte 0 is zone 1)."""
    hexs, out = body[4:-1], set()
    for i in range(len(hexs) // 2):
        b = int(hexs[i * 2:i * 2 + 2], 16)
        out.update(i * 8 + bit + 1 for bit in range(8) if b >> bit & 1)
    return out


def user_code():
    """The stored alarm code, or None if it isn't set (the bridge is then read-only)."""
    try:
        code = config.param("evl/user_code")
    except SystemExit:  # still CHANGE_ME
        return None
    except Exception as e:  # not created yet
        if "ParameterNotFound" not in type(e).__name__ + str(e):
            raise
        return None
    return code if re.fullmatch(r"\d{4}", code) else None


def named_service(acc, kind, chars=()):
    """A service carrying the accessory's name, which Home shows on the tile
    (without it, a bridged sensor is just "Contact Sensor")."""
    s = acc.add_preload_service(kind, chars=[*chars, "Name"])
    s.configure_char("Name", value=acc.display_name)
    return s


class Pending:
    """A change sent from HomeKit, waiting for the panel to show it."""

    def __init__(self):
        self.want = self.until = None

    def start(self, want):
        self.want, self.until = want, time.time() + PENDING_S

    def fail(self):
        self.want = None  # the next update springs the control back

    def waiting(self, have):
        """True while the control should be left alone."""
        if self.want is None:
            return False
        if have == self.want:
            self.want = None
            return False
        if time.time() < self.until:
            return True
        log.warning("the panel didn't show %s; springing back", self.want)
        self.want = None
        return False


class Alarm(Accessory):
    category = CATEGORY_ALARM_SYSTEM

    def __init__(self, driver, name, send):
        super().__init__(driver, name)
        self.send = send
        self.pending = Pending()
        self.ready = True
        s = named_service(self, "SecuritySystem", ["StatusFault"])
        self.current = s.configure_char("SecuritySystemCurrentState", value=DISARMED)
        self.not_ready = s.configure_char("StatusFault", value=0)
        self.target = s.configure_char(
            "SecuritySystemTargetState", value=DISARMED,
            valid_values={"StayArm": STAY, "AwayArm": AWAY, "Disarm": DISARMED},
            setter_callback=self.set_target)

    def set_target(self, value):
        self.pending.start(value)
        if value != DISARMED and self.current.value == DISARMED and not self.ready:
            log.info("refused HomeKit %s: the panel isn't ready to arm", TARGET_ACTIONS[value])
            self.pending.fail()
            return
        self.send(TARGET_ACTIONS[value], self.pending)

    def update(self, state, ready):
        cur = CURRENT.get(state)
        if cur is None:
            return
        self.ready = ready
        if self.current.value != cur:
            self.current.set_value(cur)
        not_ready = int(cur == DISARMED and not ready)
        if self.not_ready.value != not_ready:
            self.not_ready.set_value(not_ready)
        # While triggered, the target stays at the armed mode, so Off disarms.
        if self.pending.waiting(cur) or cur == TRIGGERED:
            return
        if self.target.value != cur:
            self.target.set_value(cur)


class Chime(Accessory):
    category = CATEGORY_SWITCH

    def __init__(self, driver, name, send):
        super().__init__(driver, name)
        self.send = send
        self.pending = Pending()
        self.panel = None  # the chime as the panel last showed it
        self.on = named_service(self, "Switch").configure_char(
            "On", value=False, setter_callback=self.set_on)

    def set_on(self, value):
        self.pending.start(bool(value))
        if self.panel is None:  # the key toggles, so it's only safe once the state is known
            log.info("refused HomeKit chime: the panel's state isn't known yet")
            self.pending.fail()
        elif bool(value) != self.panel:
            self.send("chime", self.pending)  # the panel's chime key toggles it

    def update(self, chime):
        self.panel = chime
        if not self.pending.waiting(chime) and self.on.value != chime:
            self.on.set_value(chime)


class EntryDelay(Accessory):
    category = CATEGORY_SENSOR

    def __init__(self, driver, name):
        super().__init__(driver, name, aid=50)  # fixed, clear of the zones' 100 + zone
        self.state = named_service(self, "ContactSensor").configure_char("ContactSensorState", value=0)

    def update(self, counting_down):
        if self.state.value != int(counting_down):
            self.state.set_value(int(counting_down))


class Zone(Accessory):
    category = CATEGORY_SENSOR

    def __init__(self, driver, name, kind, aid):
        super().__init__(driver, name, aid=aid)
        self.kind = kind
        char = {"MotionSensor": "MotionDetected", "ContactSensor": "ContactSensorState",
                "SmokeSensor": "SmokeDetected"}[kind]
        s = named_service(self, kind, ["StatusLowBattery", "StatusFault"])
        self.state = s.configure_char(char, value=False if kind == "MotionSensor" else 0)
        self.low_battery = s.configure_char("StatusLowBattery", value=0)
        self.fault = s.configure_char("StatusFault", value=0)

    def update(self, active, low_battery, trouble):
        # ContactSensorState: 0 = contact (closed), 1 = no contact (open).
        value = bool(active) if self.kind == "MotionSensor" else int(bool(active))
        for char, v in ((self.state, value), (self.low_battery, int(low_battery)),
                        (self.fault, int(trouble))):
            if char.value != v:
                char.set_value(v)


class AlarmBridge(Bridge):
    category = CATEGORY_BRIDGE

    def __init__(self, driver, name, db_path, code):
        super().__init__(driver, name)
        self.db_path, self.code = db_path, code
        self.alarm = Alarm(driver, "Alarm", self.send)
        self.chime = Chime(driver, "Chime", self.send)
        self.entry_delay = EntryDelay(driver, "Entry Delay")
        self.add_accessory(self.alarm)
        self.add_accessory(self.chime)
        self.add_accessory(self.entry_delay)
        self.zones = {}
        self.add_zones(self.known_zones())

    def db(self):
        return sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5)

    def known_zones(self):
        """Every zone the log has ever named, read once at start."""
        with self.db() as c:
            self.seen_id = c.execute("select coalesce(max(id), 0) from log").fetchone()[0]
            names = dict(c.execute("select zone, name from alarm_zones"))
            # Fire zones show up in Contact ID reports, even when the keypad never names them.
            for (z,) in c.execute("""select distinct cast(substr(body, 11, 3) as integer) from log
                                     where source = 'evl' and event_type = 'cid'
                                     and (substr(body, 6, 2) = '11' or substr(body, 6, 3) = '373')"""):
                names.setdefault(z, f"Fire Zone {z}")
        return names

    def new_zones(self, c):
        """Zones named in the log since the last poll: a keypad screen naming a
        zone ("FAULT 19 BEDROOM3 DOOR"), or a fire report for one."""
        names = {}
        for rid, kind, zone, body in c.execute(
                """select id, event_type, device_id, body from log where id > ? and source = 'evl'
                   and ((event_type = 'keypad' and device_id is not null) or event_type = 'cid')
                   order by id""", (self.seen_id,)):
            self.seen_id = rid
            if kind == "keypad":
                words = body[:-1].split(",", 5)[-1].split()
                if len(words) > 2:
                    names.setdefault(int(zone), " ".join(words[2:]))
            elif body[5:7] == "11" or body[5:8] == "373":
                names.setdefault(int(body[10:13]), f"Fire Zone {int(body[10:13])}")
        return names

    def add_zones(self, names):
        """Add a sensor for each zone not seen before; True if any were added."""
        added = False
        for zone, name in sorted(names.items()):
            if zone in self.zones or not zone:
                continue
            kind = ("MotionSensor" if "MOTION" in name.upper()
                    else "SmokeSensor" if re.search("SMOKE|FIRE", name.upper())
                    else "ContactSensor")
            # aid = 100 + zone keeps each zone's HomeKit identity stable.
            self.zones[zone] = Zone(self.driver, zone_title(name), kind, aid=100 + zone)
            self.add_accessory(self.zones[zone])
            log.info("zone %d: %s (%s)", zone, name, kind)
            added = True
        return added

    def send(self, action, pending):
        """Send an action from the Home app, off the event loop."""
        if not self.code:
            log.info("refused HomeKit %s: no user code stored (evl/user_code)", action)
            pending.fail()
            return
        self.driver.add_job(self.post, action, pending)

    def post(self, action, pending):
        body = json.dumps({"action": action, "code": self.code}).encode()
        req = urllib.request.Request(ACTIONS_URL + "/action", body,
                                     {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15):
                log.info("sent %s", action)  # never the code
        except urllib.error.HTTPError as e:
            log.warning("%s not sent: %s", action, json.load(e).get("error"))
            pending.fail()
        except OSError as e:
            log.warning("%s not sent: %s", action, e)
            pending.fail()

    @Accessory.run_at_interval(POLL_S)
    async def run(self):
        st = {}
        try:
            with urllib.request.urlopen(ACTIONS_URL + "/status", timeout=2) as r:
                st = json.load(r)
            if "state" in st:
                self.alarm.update(st["state"], bool(st["ready"]))
                self.chime.update(bool(st["chime"]))
                self.entry_delay.update(bool(st.get("entry_delay")))
        except OSError as e:
            log.warning("status unavailable: %s", e)

        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - TROUBLE_WINDOW_S))
        with self.db() as c:
            if self.add_zones(self.new_zones(c)):
                self.driver.config_changed()
            row = c.execute("""select body from log where source = 'evl'
                               and event_type = 'zone_state' order by id desc limit 1""").fetchone()
            screens = c.execute("""select distinct event_name, device_id from log
                                   where source = 'evl' and event_type = 'keypad'
                                   and event_name in ('CHECK', 'LOBAT') and received_at > ?""",
                                (since,)).fetchall()
            # The latest report of each event code for each zone: 1 = new, 3 = restored.
            reports = c.execute("""select substr(body, 6, 3), cast(substr(body, 11, 3) as integer),
                                          substr(body, 5, 1), max(id)
                                   from log where source = 'evl' and event_type = 'cid'
                                   group by 1, 2""").fetchall()
            # The panel doesn't always report a sensor restored (leaving programming
            # clears troubles silently), so a sensor heard from since counts as back.
            first = min((i for code, _, q, i in reports if code == SUPERVISION_LOST and q == "1"),
                        default=None)
            heard = dict(c.execute("""select cast(device_id as integer), max(id) from log
                                      where id > ? and source = 'evl' and event_type = 'keypad'
                                      and event_name = 'FAULT' group by 1""",
                                   (first,)).fetchall()) if first else {}
        is_open = open_zones(row[0]) if row else set()
        # The EVL reports a zone closed about a minute after the keypad stops
        # showing it; the panel's Ready light comes back at once. Disarmed and
        # ready means nothing is open, unless a zone is bypassed.
        if st.get("state") == "disarmed" and st.get("ready") and not st.get("bypass"):
            is_open = set()
        low = {int(z) for k, z in screens if k == "LOBAT" and z}
        trouble = {int(z) for k, z in screens if k == "CHECK" and z}
        lost = set(trouble)
        fire = set()
        for code, zone, q, rid in reports:
            if q != "1" or (code == SUPERVISION_LOST and heard.get(zone, 0) > rid):
                continue
            if code == LOW_BATTERY:
                low.add(zone)
            elif code in TROUBLES:
                trouble.add(zone)
                if code == SUPERVISION_LOST:
                    lost.add(zone)
            elif code in FIRE_ALARMS:
                fire.add(zone)
        for zone, acc in self.zones.items():
            active = (zone in fire if acc.kind == "SmokeSensor"
                      else zone in is_open or zone in lost if acc.kind == "ContactSensor"
                      else zone in is_open)
            acc.update(active, zone in low, zone in trouble)


def main():
    config.setup_logging()
    code = user_code()
    log.info("user code %s", "stored: Home app controls work" if code else "not set: read-only")
    driver = AccessoryDriver(
        address=wg_address(), port=PORT,
        persist_file=os.environ.get("HOMEKIT_STATE", "/data/homekit.state"),
        interface_choice=[MDNS_IP])
    driver.add_accessory(AlarmBridge(driver, "Log Cabin", os.environ["DB_PATH"], code))
    driver.start()


if __name__ == "__main__":
    main()
