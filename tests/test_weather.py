"""Weather endpoints against official Vanilla XML and flat tenant registry."""
import copy
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from test_airstrike import bot, call, servers  # noqa: F401; shared real registry fixture

FIXTURES = Path(__file__).parent / "fixtures" / "weather"
MAPS = (("ChernarusPlus", "chernarusplus"), ("Livonia", "enoch"), ("Sakhal", "sakhal"))


def post(monkeypatch, conn, values=None, commit=False, session=None):
    return call(monkeypatch, conn, bot.api_tools_weather_post,
                {"werte": values if values is not None else bot._weather_vanilla(conn.data["map_name"]),
                 "commit": commit}, session)


@pytest.mark.parametrize("karte,filename", MAPS)
def test_vanilla_complete_original_and_missing_file(monkeypatch, servers, karte, filename):
    a, _ = servers
    a.data["map_name"] = karte
    status, response = call(monkeypatch, a, bot.api_tools_weather_get)
    assert status == 200
    assert response["data"]["quelle"] == "vanilla"
    assert response["data"]["werte"] == bot._weather_vanilla(karte)
    raw = (FIXTURES / f"cfgweather-{filename}.xml").read_text()
    a.ftp.files["/mission/cfgweather.xml"] = raw
    status, response = call(monkeypatch, a, bot.api_tools_weather_get)
    result = response["data"]
    assert status == 200 and result["quelle"] == "server"
    root = ET.fromstring(raw)
    values = result["werte"]
    assert values["enable"] == int(root.get("enable"))
    assert values["reset"] == int(root.get("reset"))
    for block in root:
        if block.tag == "storm":
            assert values[block.tag] == {k: float(v) for k, v in block.attrib.items()}
        else:
            assert values[block.tag] == {child.tag: {k: float(v) for k, v in child.attrib.items()} for child in block}
    assert a.ftp.writes == []


def test_corrupt_and_missing_blocks(monkeypatch, servers):
    a, _ = servers
    a.ftp.files["/mission/cfgweather.xml"] = "<weather><rain>"
    status, response = call(monkeypatch, a, bot.api_tools_weather_get)
    assert status == 200 and response["data"]["quelle"] == "vanilla"
    assert response["data"]["warnung"]
    a.ftp.files["/mission/cfgweather.xml"] = '<weather reset="yes" enable="false"><overcast><current actual="0.33"/></overcast></weather>'
    status, response = call(monkeypatch, a, bot.api_tools_weather_get)
    values = response["data"]["werte"]
    expected = bot._weather_vanilla("Livonia")
    expected["reset"], expected["overcast"]["current"]["actual"] = 1, .33
    assert status == 200 and values == expected and response["data"]["quelle"] == "server"


def test_bohemia_child_element_syntax():
    raw = '<weather><overcast><current><actual>0.33</actual><time>150</time><duration>360</duration></current></overcast><storm><timeout>75</timeout></storm></weather>'
    values = bot._weather_parse(raw, "Livonia")
    assert values["overcast"]["current"] == {"actual": .33, "time": 150, "duration": 360}
    assert values["storm"]["timeout"] == 75


@pytest.mark.parametrize("karte,filename", MAPS)
def test_preview_and_single_commit(monkeypatch, servers, karte, filename):
    a, b = servers
    a.data["map_name"] = karte
    original_b = copy.deepcopy(b.ftp.files)
    values = bot._weather_vanilla(karte)
    values["reset"] = 1
    values["windDirection"]["current"]["actual"] = 1.57
    status, response = post(monkeypatch, a, values)
    assert status == 200 and a.ftp.writes == []
    xml = response["data"]["generated"][0]["content"]
    assert ET.fromstring(xml).get("enable") == "1"
    assert 'duration="32768"' in xml
    status, _ = post(monkeypatch, a, values, True)
    assert status == 200 and a.ftp.writes == ["/mission/cfgweather.xml"]
    expected = copy.deepcopy(values)
    expected["enable"] = 1
    assert bot._weather_parse(a.ftp.files["/mission/cfgweather.xml"], karte) == expected
    assert b.ftp.files == original_b and b.ftp.writes == []
    root = ET.fromstring(xml)
    assert root.find("rain/thresholds") is not None and root.find("snowfall/thresholds") is not None
    assert not any("backup" in name for name in a.ftp.files)
    assert bot._weather_xml(values, "fixed") == bot._weather_xml(values, "fixed")


@pytest.mark.parametrize("path,value", [
    (("overcast", "current", "actual"), 1.1), (("fog", "current", "actual"), -.1),
    (("windMagnitude", "limits", "max"), 51), (("windDirection", "current", "actual"), 3.16),
    (("rain", "current", "time"), 86401), (("rain", "thresholds", "end"), 86401),
    (("storm", "density"), 2), (("storm", "threshold"), -1), (("storm", "timeout"), 86401),
    (("rain", "current", "actual"), "0.5"), (("rain", "current", "actual"), float("nan")),
    (("rain", "current", "actual"), float("inf")), (("rain", "current", "actual"), True),
    (("rain", "current", "actual"), 10 ** 1000), (("enable",), True),
    (("snowfall", "current", "actual"), .1), (("snowfall", "limits", "max"), .1),
    (("overcast", "limits", "min"), 1.01), (("overcast", "timelimits", "min"), 901),
    (("rain", "thresholds", "min"), 1.1), (("windDirection", "changelimits", "min"), 2),
])
def test_invalid_400(monkeypatch, servers, path, value):
    a, _ = servers
    values = bot._weather_vanilla("Livonia")
    target = values
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    status, _ = post(monkeypatch, a, values, True)
    assert status == 400 and a.ftp.writes == []


def test_rain_on_sakhal_and_inverted_minmax(monkeypatch, servers):
    a, _ = servers
    a.data["map_name"] = "Sakhal"
    values = bot._weather_vanilla("Sakhal")
    values["rain"]["current"]["actual"] = .1
    assert post(monkeypatch, a, values)[0] == 400
    values = bot._weather_vanilla("Sakhal")
    values["overcast"]["limits"] = {"min": .8, "max": .2}
    assert post(monkeypatch, a, values)[0] == 400


@pytest.mark.parametrize("karte,filename", MAPS)
def test_all_presets(monkeypatch, servers, karte, filename):
    a, _ = servers
    a.data["map_name"] = karte
    for preset in bot._weather_presets(karte):
        assert post(monkeypatch, a, preset["werte"])[0] == 200
        root = ET.fromstring(bot._weather_xml(preset["werte"], "fixed"))
        forbidden = "rain" if karte == "Sakhal" else "snowfall"
        assert float(root.find(f"{forbidden}/limits").get("max")) == 0
        assert preset["disabled"] == (preset["key"] == "snowstorm" and karte != "Sakhal" or preset["key"] == "rain" and karte == "Sakhal")


def test_flat_tenant_get_commit_and_rate(monkeypatch, servers, tmp_path):
    a, b = servers
    before = (tmp_path / "connections.json").read_bytes()
    a.ftp.files["/mission/cfgweather.xml"] = bot._weather_xml(bot._weather_presets("Livonia")[-1]["werte"], "A")
    assert call(monkeypatch, b, bot.api_tools_weather_get)[1]["data"]["quelle"] == "vanilla"
    original_a = copy.deepcopy(a.ftp.files)
    assert post(monkeypatch, b, commit=True, session="same")[0] == 200
    assert post(monkeypatch, b, commit=True, session="same")[0] == 429
    assert b.ftp.writes == ["/mission/cfgweather.xml"] and a.ftp.files == original_a and a.ftp.writes == []
    assert (tmp_path / "connections.json").read_bytes() == before


def test_no_mission_and_unknown_map(monkeypatch, servers):
    a, _ = servers
    a.data["ftp_mission_dir"] = ""
    assert call(monkeypatch, a, bot.api_tools_weather_get)[1]["data"]["kein_mission_ordner"]
    assert post(monkeypatch, a)[0] == 200
    assert post(monkeypatch, a, commit=True)[0] == 409
    a.data["map_name"] = "Unknown"
    assert call(monkeypatch, a, bot.api_tools_weather_get)[0] == 400
    assert a.ftp.writes == []


@pytest.mark.parametrize("raises", [False, True])
def test_ftp_failure_is_502(monkeypatch, servers, raises):
    a, _ = servers
    if raises:
        a.ftp.raise_at = 1
    else:
        a.ftp.fail_at = 1
    assert post(monkeypatch, a, commit=True)[0] == 502
    assert a.ftp.writes == ["/mission/cfgweather.xml"]
