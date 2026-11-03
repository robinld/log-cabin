import json
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
    assert yolink["liteLog"] is False  # so each received state is in the journal


def test_build_lists_kasa_plugs_only_when_there_are_some():
    assert len(homebridge.build(None, "ua", "sec")["platforms"]) == 1
    kasa = homebridge.build(None, "ua", "sec", ["192.0.2.7"])["platforms"][1]
    assert kasa["platform"] == "TplinkSmarthome" and kasa["devices"] == [{"host": "192.0.2.7"}]


def test_the_flume_is_paired_as_a_non_admin_beside_home():
    home = {"pairedClients": {"HOME": "aa"}, "pairedClientsPermission": {"HOME": 1}, "signPk": "bb"}
    assert homebridge.has_admin(home)
    assert homebridge.has_admin({"pairedClients": {"OLD": "aa"}})  # saved before permissions were
    assert not homebridge.has_admin({"pairedClients": {}})
    flume = {"id": "FLUME", "ltpk": "cc"}
    paired = homebridge.with_controller(home, flume)
    assert paired["pairedClients"] == {"HOME": "aa", "FLUME": "cc"}
    assert paired["pairedClientsPermission"] == {"HOME": 1, "FLUME": 0}
    assert paired["signPk"] == "bb" and homebridge.with_controller(paired, flume) is None


def test_pair_flume_waits_for_home_then_writes_both_files(tmp_path, monkeypatch):
    monkeypatch.setattr(homebridge, "STORAGE", tmp_path)
    monkeypatch.setattr(homebridge, "CONTROLLER", tmp_path / "controller.json")
    monkeypatch.setattr(homebridge.shutil, "chown", lambda *a: None)
    info_path = homebridge.accessory_info_path("0E:11:22:33:44:55")
    assert info_path.name == "AccessoryInfo.0E1122334455.json"
    info_path.parent.mkdir()
    info_path.write_text(json.dumps({"pairedClients": {}, "signPk": "bb"}))
    assert "not paired" in homebridge.pair_flume("0E:11:22:33:44:55")
    assert not (tmp_path / "controller.json").exists()  # an unpaired bridge stays pairable

    info_path.write_text(json.dumps({"pairedClients": {"HOME": "aa"}, "pairedClientsPermission": {"HOME": 1},
                                     "signPk": "bb"}))
    assert homebridge.pair_flume("0E:11:22:33:44:55") == "flume paired"
    assert homebridge.pair_flume("0E:11:22:33:44:55") == "flume already paired"
    controller = json.loads((tmp_path / "controller.json").read_text())
    assert json.loads(info_path.read_text())["pairedClients"][controller["id"]] == controller["ltpk"]
    assert (controller["accessory_id"], controller["accessory_ltpk"], controller["port"]) == (
        "0E:11:22:33:44:55", "bb", homebridge.PORT)
    assert oct(info_path.stat().st_mode & 0o777) == "0o600"
