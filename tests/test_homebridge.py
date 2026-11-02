import re

from home_events import homebridge


def test_build_keeps_the_bridge_identity_so_home_stays_paired():
    old = {"bridge": {"name": "x", "username": "0E:11:22:33:44:55", "pin": "123-45-321", "port": 1}}
    cfg = homebridge.build(old, "ua", "sec")
    assert cfg["bridge"]["username"] == "0E:11:22:33:44:55" and cfg["bridge"]["pin"] == "123-45-321"
    assert cfg["bridge"]["port"] == homebridge.PORT


def test_build_makes_a_new_identity_for_a_new_bridge():
    b = homebridge.build(None, "ua", "sec")["bridge"]
    assert re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", b["username"])
    assert re.fullmatch(r"\d{3}-\d{2}-\d{3}", b["pin"])


def test_build_sends_only_sensors_to_homekit():
    yolink = homebridge.build(None, "ua", "sec")["platforms"][0]
    assert yolink["allDevices"] is False
    assert "Switch" not in yolink["includeTypes"] and "LeakSensor" in yolink["includeTypes"]
    assert (yolink["userAccessId"], yolink["secretKey"]) == ("ua", "sec")


def test_build_lists_kasa_plugs_only_when_there_are_some():
    assert len(homebridge.build(None, "ua", "sec")["platforms"]) == 1
    kasa = homebridge.build(None, "ua", "sec", ["192.0.2.7"])["platforms"][1]
    assert kasa["platform"] == "TplinkSmarthome" and kasa["devices"] == [{"host": "192.0.2.7"}]
