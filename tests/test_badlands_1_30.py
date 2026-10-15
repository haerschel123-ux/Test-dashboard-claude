"""DayZ 1.30 (Badlands, Vorabstand 14.10.2026): neue Klassen im Loadout-Katalog,
Fahrzeug-Builder, Motorrad-Sperre nach Datum und Types-Organizer-Kategorien.

    python3 -m pytest tests/test_badlands_1_30.py -v
"""
import base64
import datetime
import json
import os
import re
import sys
import zlib

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")


def _katalog():
    text = zlib.decompress(base64.b64decode(bot._EMBEDDED_ASSETS["loadout-catalog.js"])).decode("utf8")
    return {m.group(1): json.loads(m.group(2))
            for m in re.finditer(r"^window\.(\w+)\s*=\s*(.*);$", text, re.M)}


def _asset_namen():
    return set(bot._EMBEDDED_ASSETS)


NEU = {
    "Headgear": ["TurbanHat_Gold", "PakolHat_Beige", "OBRCap_Olive", "MilitaryBeret_TK", "GhillieHood_Desert"],
    "Mask": ["Keffiyeh_Red"],
    "Eyewear": ["DustGoggles_Black"],
    "Body": ["KameezShirt_Pink", "FNOJacket", "OBRJacket_Officer", "ShortSleeveShirt_Hawaiian_Blue",
             "GhillieSuit_Desert", "TacticalShirt_ION_Tan"],
    "Vest": ["LinenVest_Grey", "RobeVest_Navy", "ChestRigVest_Khaki", "HighCapacityVest_ION"],
    "Hips": ["LeatherBelt_Lizard", "LeatherHipPack_Lizard"],
    "Back": ["NBCPack_Yellow"],
    "Legs": ["FNOPants", "MensShalwarPants_Beige", "WomensShalwarPants_Pink", "OBRPants_Olive"],
    "Feet": ["SandalsShoes_Black", "TraditionalSandalShoes_Brown"],
    "Hands": ["Akinaka", "Jambiya", "Scimitar", "DonerKnife", "PoliceBaton", "Luger", "MP18",
              "AKM_Gold", "Magnum_Gold", "SawedoffMagnum_Gold"],
}
NEU_INHALT = {
    "complex": ["Mag_Luger_8Rnd", "Mag_MP18_Drum32Rnd", "Mag_LeeEnfield_10Rnd"],
    "simple": ["P1907_Bayonet", "GhillieAtt_Desert"],
    "food": ["Achar", "CatfishFilletMeat", "HalvaCan", "SarmaCan", "LizardMeat"],
    "openedFood": ["HalvaCan_Opened", "SarmaCan_Opened"],
    "drinks": ["SodaCan_ZumZam", "Ayran", "Lassi_Mango"],
    "misc": ["WineBottle", "Waterskin_Natural", "GunCase_Black", "GunCaseKeys_Red", "HandDrillKitPalm",
             "MortarMix", "PileOfBricks", "StorageBox_Wooden", "LizardKnifeSheath"],
}


def test_neue_items_stehen_im_richtigen_slot():
    k = _katalog()
    for slot, namen in NEU.items():
        for n in namen:
            assert n in k["LOADOUT_CATALOG"][slot], (slot, n)
    for art, namen in NEU_INHALT.items():
        for n in namen:
            assert n in k["LOADOUT_CONTENT"][art], (art, n)
    for n in ("Akinaka", "Luger", "MP18", "AKM_Gold"):
        assert n in k["LOADOUT_WEAPONS"], n


def test_katalog_bleibt_konsistent():
    k = _katalog()
    alle = k["LOADOUT_ALL"]
    assert len(alle) == len({x.lower() for x in alle}), "Doppelte Classnames"
    assert alle == sorted(alle, key=str.lower)
    assert set(k["LOADOUT_NAMES"]) == {x.lower() for x in alle}
    zugeordnet = {c for v in k["LOADOUT_CATALOG"].values() for c in v}
    zugeordnet |= {c for v in k["LOADOUT_CONTENT"].values() for c in v}
    fehlt = {c for c in zugeordnet if c not in alle}
    assert not fehlt, sorted(fehlt)[:5]
    for v in list(k["LOADOUT_CATALOG"].values()) + list(k["LOADOUT_CONTENT"].values()):
        assert v == sorted(v, key=str.lower)


def test_nicht_aufgenommenes_fehlt():
    """Basisklassen, Bunker-Sonderklasse, Tiere und Infizierte gehoeren nicht in den Loadout-Katalog."""
    k = _katalog()
    alle = {x.lower() for x in k["LOADOUT_ALL"]}
    for n in ("DigitalCodeLock_Bunker", "Animal_OvisGmelini", "Animal_VaranusGriseus", "Motorbike_01_Blue",
              "Offroad_02_Hood_Beige", "LizardSkin"):
        assert n.lower() not in alle, n


def test_einzelbilder_der_neuen_waffen_sind_eingebettet():
    namen = _asset_namen()
    for n in ("akinaka", "jambiya", "luger"):
        assert f"items/{n}.avif" in namen, n


def test_fahrzeugbuilder_motorraeder_und_humvee_beige():
    v = bot.TOOL_VEHICLES
    for farbe in ("Blue", "Red", "Yellow"):
        teile = [t[0] for t in v[f"Motorbike_01_{farbe}"]["parts"]]
        assert teile == ["Motorbike_01_Wheel_1", "Motorbike_01_Wheel_2"]
    for farbe in ("Blue", "Green", "Red", "Yellow"):
        teile = {t[0] for t in v[f"Motorbike_02_{farbe}"]["parts"]}
        assert teile == {"Motorbike_02_Wheel_1", "Motorbike_02_Wheel_2",
                         f"Motorbike_02_ShieldFront_{farbe}", f"Motorbike_02_ShieldLeft_{farbe}",
                         f"Motorbike_02_ShieldRight_{farbe}"}
    humvee = {t[0] for t in v["Offroad_02"]["parts"]}
    assert {"Offroad_02_Hood_Beige", "Offroad_02_Trunk_Beige", "Offroad_02_Door_1_1_Beige",
            "Offroad_02_Door_2_2_Beige"} <= humvee
    # Jede Motorrad-Klasse des Deployments hat auch einen Builder-Eintrag mit Bild
    for p in bot.TOOL_DEPLOY_VEHICLES.values():
        if p["klasse"].startswith("Motorbike_"):
            assert p["klasse"] in v, p["klasse"]
            assert f"vehicles/{p['klasse']}.webp" in _asset_namen()


def test_humvee_beige_in_builder_und_deployment(monkeypatch):
    assert "Beige" in bot.TOOL_VEHICLES["Offroad_02"]["farben"]
    p = bot.TOOL_DEPLOY_VEHICLES["humvee_beige"]
    assert p["klasse"] == "Offroad_02_Beige"
    # kein eigenes Beige-Bild: Rueckfall auf das Modellbild des Humvee
    assert bot._deploy_bild(p) == "/static/vehicles/Offroad_02.png"
    monkeypatch.setattr(bot, "_DEPLOY_BALD_BIS", datetime.date(2999, 1, 1))
    assert bot._deploy_bald(p) is True
    monkeypatch.setattr(bot, "_DEPLOY_BALD_BIS", datetime.date(2000, 1, 1))
    assert bot._deploy_bald(p) is False


def test_motorraeder_und_beiger_humvee_sind_freigegeben():
    """Brigarde hat die 1.30-Fahrzeuge am 14.10.2026 vorab freigegeben (Standardwert, ohne Patch)."""
    for k in ("jana_blue", "jana_red", "jana_yellow", "bitrak_blue", "bitrak_green", "bitrak_red",
              "bitrak_yellow", "humvee_beige"):
        assert bot._deploy_bald(bot.TOOL_DEPLOY_VEHICLES[k]) is False, k


def test_motorrad_sperre_endet_am_erscheinungstag(monkeypatch):
    preset = bot.TOOL_DEPLOY_VEHICLES["jana_blue"]
    monkeypatch.setattr(bot, "_DEPLOY_BALD_BIS", datetime.date.today() + datetime.timedelta(days=1))
    assert bot._deploy_bald(preset) is True
    monkeypatch.setattr(bot, "_DEPLOY_BALD_BIS", datetime.date.today())
    assert bot._deploy_bald(preset) is False
    # Vorlagen ohne "bald" waren nie gesperrt
    monkeypatch.setattr(bot, "_DEPLOY_BALD_BIS", datetime.date(2999, 1, 1))
    assert bot._deploy_bald(bot.TOOL_DEPLOY_VEHICLES["ada_blue"]) is False


@pytest.mark.parametrize("name,kategorie", [
    ("Motorbike_02_Red", "Vehicles"), ("Offroad_02", "Vehicles"),
    ("Motorbike_02_ShieldLeft_Red", "Vehicle Parts"), ("Motorbike_01_Wheel_1", "Vehicle Parts"),
    ("Offroad_02_Hood_Beige", "Vehicle Parts"), ("Offroad_02_Door_1_1_Beige", "Vehicle Parts"),
    ("Animal_VaranusGriseus", "Animals"), ("Luger", "Firearms"), ("MP18", "Firearms"),
    ("Mag_Luger_8Rnd", "Magazines"), ("P1907_Bayonet", "Attachments"),
    ("Akinaka", "Melee Items"), ("Scimitar", "Melee Items"),
    ("MilitaryBeret_TK", "Hats"), ("Keffiyeh_Red", "Masks"),
    ("KameezShirt_Pink", "Clothing"), ("MensShalwarPants_Beige", "Clothing"), ("FNOPants", "Clothing"),
    ("ShortSleeveShirt_Waves", "Clothing"), ("LeatherHipPack_Lizard", "Belts"),
    ("GunCaseKeys_Red", "Tools"), ("ShippingContainerKeys_Green", "Tools"),
    ("StorageBox_Wooden", "Storage Items"), ("HalvaCan", "Food"), ("WineBottle", "Food"),
    ("BrickTrowel", "Supplies"), ("PileOfBricks", "Supplies"),
])
def test_organizer_kategorien_neuer_klassen(name, kategorie):
    assert bot._tool_organizer_kategorie_von_classname(name) == kategorie


def test_organizer_bestand_unveraendert():
    """Stichprobe: bisher erkannte Klassen behalten ihre Kategorie."""
    for name, kat in (("AK74", "Firearms"), ("Mag_AK74_30Rnd", "Magazines"), ("Hatchback_02_Blue", "Vehicles"),
                      ("HatchbackWheel", "Vehicle Parts"), ("Bandana_Blue", "Masks"), ("Machete", "Melee Items"), ("BDUJacket", "Clothing"), ("TacticalShirt_Grey", "Tacticals"),
                      ("LeatherBelt_Black", "Belts"), ("SKS_Bayonet", "Attachments")):
        got = bot._tool_organizer_kategorie_von_classname(name)
        assert got == kat, (name, got)


# Gegenprobe gegen die hochgeladene 1.30-types.xml (Brigarde, 14.10.2026)
TYPES_130_WAFFEN = ["SKS_Desert", "SKS_Winter", "SKS_Woodland", "CZ527_Desert", "CZ527_Winter", "CZ527_Woodland",
                    "Mosin9130_Desert", "Mosin9130_Winter", "Mosin9130_Woodland", "SawedoffMosin9130_Desert",
                    "SawedoffMosin9130_Winter", "SawedoffMosin9130_Woodland", "Glock19_Tan"]


def test_neue_waffen_aus_types_xml_im_loadout():
    k = _katalog()
    for n in TYPES_130_WAFFEN:
        assert n in k["LOADOUT_CATALOG"]["Hands"], n
        assert n in k["LOADOUT_WEAPONS"], n
        assert bot._tool_organizer_kategorie_von_classname(n) == "Firearms", n


TYPES_130_FAHRZEUGE = frozenset(['Motorbike_01_Blue', 'Motorbike_01_Red', 'Motorbike_01_Wheel_1', 'Motorbike_01_Wheel_1_Ruined', 'Motorbike_01_Wheel_2', 'Motorbike_01_Wheel_2_Ruined', 'Motorbike_01_Yellow', 'Motorbike_02_Blue', 'Motorbike_02_Green', 'Motorbike_02_Red', 'Motorbike_02_ShieldFront_Blue', 'Motorbike_02_ShieldFront_Green', 'Motorbike_02_ShieldFront_Red', 'Motorbike_02_ShieldFront_Yellow', 'Motorbike_02_ShieldLeft_Blue', 'Motorbike_02_ShieldLeft_Green', 'Motorbike_02_ShieldLeft_Red', 'Motorbike_02_ShieldLeft_Yellow', 'Motorbike_02_ShieldRight_Blue', 'Motorbike_02_ShieldRight_Green', 'Motorbike_02_ShieldRight_Red', 'Motorbike_02_ShieldRight_Yellow', 'Motorbike_02_Wheel_1', 'Motorbike_02_Wheel_1_Ruined', 'Motorbike_02_Wheel_2', 'Motorbike_02_Wheel_2_Ruined', 'Motorbike_02_Yellow', 'Offroad_02_Beige', 'Offroad_02_Door_1_1_Beige', 'Offroad_02_Door_1_1_BeigeRust', 'Offroad_02_Door_1_2_Beige', 'Offroad_02_Door_1_2_BeigeRust', 'Offroad_02_Door_2_1_Beige', 'Offroad_02_Door_2_1_BeigeRust', 'Offroad_02_Door_2_2_Beige', 'Offroad_02_Door_2_2_BeigeRust', 'Offroad_02_Hood_Beige', 'Offroad_02_Trunk_Beige', 'Offroad_02_Trunk_BeigeRust'])


def test_fahrzeugklassen_stehen_in_der_types_xml():
    """Klassen und Teile der Motorraeder/des beigen Humvee kommen alle in der 1.30-types.xml vor."""
    teile = {t[0] for k, v in bot.TOOL_VEHICLES.items() if k.startswith("Motorbike_") for t in v["parts"]}
    teile |= {t[0] for t in bot.TOOL_VEHICLES["Offroad_02"]["parts"] if t[0].endswith("_Beige")}
    klassen = {p["klasse"] for p in bot.TOOL_DEPLOY_VEHICLES.values()
               if p["klasse"].startswith(("Motorbike_", "Offroad_02_"))}
    klassen |= {k for k in bot.TOOL_VEHICLES if k.startswith("Motorbike_")}
    assert len(klassen) == 8 and len(teile) >= 20
    assert not (klassen | teile) - TYPES_130_FAHRZEUGE, sorted((klassen | teile) - TYPES_130_FAHRZEUGE)


# ── Gegenprobe gegen die komplette Nasdara-types.xml (15.10.2026) ─────────
def test_weitere_items_aus_der_nasdara_types_xml():
    k = _katalog()
    hands = k["LOADOUT_CATALOG"]["Hands"]
    for n in ("LeeEnfield", "SCARH", "SCARL_Black", "Glock_Tan", "SawedoffMagnum", "OrientalMachete", "Sickle"):
        assert n in hands and n in k["LOADOUT_WEAPONS"], n
    inhalt = k["LOADOUT_CONTENT"]
    assert {"Mag_SCARH_20Rnd", "Mag_AKM_Drum75Rnd_Gold"} <= set(inhalt["complex"])
    assert {"SCAR_StockBttstck", "AK_WoodHndgrd_Gold"} <= set(inhalt["simple"])
    assert {"Mackerel", "SpicyCrackers", "SaltyMix", "CaninaBerry"} <= set(inhalt["food"])
    slot = k["LOADOUT_CATALOG"]
    assert "TurbanHat_Black" in slot["Headgear"] and "Ushanka_Green" in slot["Headgear"]
    assert "HunterPants_Desert" in slot["Legs"] and "HuntingJacket_Desert" in slot["Body"]
    assert "UKAssVest_Desert" in slot["Vest"] and "CoyoteBag_Desert" in slot["Back"]
    assert "LeatherShoes_Natural" in slot["Feet"]
    # types.xml schreibt CZ527_Desert/Winter gross - keine kleingeschriebene Altform mehr
    assert "CZ527_desert" not in hands and "CZ527_Desert" in hands


def test_neue_ruecksaecke_im_rucksack_builder():
    for k, slots in (("CoyoteBag_Desert", 56), ("AssaultBag_Desert", 30), ("TortillaBag_Desert", 56),
                     ("TortillaBag_Winter", 56), ("ArmyPouch_Green", 20), ("DryBag_Orange", 20),
                     ("DryBag_Red", 20), ("DryBag_Yellow", 20)):
        assert bot.TOOL_BAGS[k]["slots"] == slots, k


def test_neue_infizierte_im_horden_generator():
    z = bot.TOOL_ZOMBIE_DATA
    assert "ZmbM_takiImam" in z["InfectedTakiSolitude"]
    assert "ZmbM_takiPoliceman_Heavy" in z["InfectedTakiPolice"]
    assert "ZmbM_IonSoldier_Normal_3" in z["InfectedIon"]
    assert "Zmbf_IonSoldier_Normal_1" in z["InfectedIon"]       # Schreibweise laut types.xml
    assert all(isinstance(g, list) and g for g in z.values())


@pytest.mark.parametrize("name,kategorie", [
    ("Offroad_02_Beige", "Vehicles"), ("Water_Well_N1", "Static Objects"), ("Wreck_UH1Y", "Static Objects"),
    ("LeeEnfield", "Firearms"), ("SCARL_Black", "Firearms"), ("Glock_Tan", "Firearms"), ("SKS_Green", "Firearms"),
    ("AK_WoodBttstck_Gold", "Attachments"), ("SCAR_StockBttstck", "Attachments"),
    ("CeramicPot_Gray", "Storage Items"), ("SpicyCrackers", "Food"), ("SaltyMix", "Food"),
])
def test_organizer_weitere_klassen(name, kategorie):
    assert bot._tool_organizer_kategorie_von_classname(name) == kategorie


def test_jedes_katalog_item_ist_einem_slot_oder_inhalt_zugeordnet():
    """Nur ueber die Suche erreichbare Items (z. B. Rucksaecke, Balaclavas) darf es nicht geben."""
    k = _katalog()
    platziert = {c.lower() for v in k["LOADOUT_CATALOG"].values() for c in v}
    platziert |= {c.lower() for v in k["LOADOUT_CONTENT"].values() for c in v}
    ohne = [x for x in k["LOADOUT_ALL"] if x.lower() not in platziert]
    erlaubt = {"1911", "drybag_green.png", "fnx45_arrow", "placeholder", "Boat_01_Black", "CivilianSedan",
               "Hatchback_02", "Offroad_02", "OffroadHatchback", "Sedan_02", "Truck_01_Covered"}
    assert set(ohne) <= erlaubt, sorted(set(ohne) - erlaubt)


def test_bisher_nur_per_suche_erreichbare_items_haben_jetzt_einen_slot():
    k = _katalog()
    c = k["LOADOUT_CATALOG"]
    assert "Balaclava3Holes_Black" in c["Mask"] and "GhillieBushrag_Tan" in c["Body"]
    assert {"PlateCarrierPouches", "PlateCarrierHolster_Camo"} <= set(c["Vest"])
    assert {"ArmyPouch_Beige", "FurCourierBag", "FurImprovisedBag"} <= set(c["Back"])
    assert "NBCBootsYellow" in c["Feet"] and "Armband_Zenit" in c["Armband"] and "Iceaxe" in c["Hands"]
    assert "Mag_PM73_25Rnd" in k["LOADOUT_CONTENT"]["complex"]
    assert "Ammo_762x54" in k["LOADOUT_CONTENT"]["looseAmmo"]
    assert "Chemlight_Red" in k["LOADOUT_CONTENT"]["misc"]


def test_neue_item_icons_sind_eingebettet():
    namen = _asset_namen()
    for n in ("scarh", "ushanka_black", "waterproofbag_green", "tortillabag_desert", "mag_scarh_20rnd", "wrench"):
        assert f"items/{n}.avif" in namen, n
