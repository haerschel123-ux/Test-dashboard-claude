"""Offline three-way merge and transactional API regression tests."""
import asyncio
import copy
import json
import pathlib
import time

import pytest
import bot
from test_airstrike import call, servers  # noqa: F401
from test_ignorelist_weaponblueprint import tool_files  # noqa: F401

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "update"


def xml(value=None, extra=""):
    return '<types>' + (f'<type name="A"><nominal>{value}</nominal></type>' if value is not None else '') + extra + '</types>'


@pytest.mark.parametrize("base,new,own,state,expected", [
    (None, 2, None, "neu", 2), (1, 1, 9, "unveraendert", 9),
    (1, 2, 1, "update", 2), (1, 2, 2, "aktuell", 2),
    (1, 2, 9, "konflikt", 9), (1, None, 9, "entfernt", 9),
    (None, None, 9, "eigen", 9), (None, 2, 2, "aktuell", 2),
    (None, 2, 9, "konflikt", 9), (1, 2, None, "konflikt", None),
])
def test_every_decision(base, new, own, state, expected):
    after, result = bot._update_merge("types.xml", xml(base), xml(new), xml(own), "Test")
    assert result["eintraege"][0]["lage"] == state
    assert bot._update_index(after, "types.xml")["root"].findtext("type/nominal") == (str(expected) if expected else None)


def test_case_whitespace_and_untouched_bytes():
    own = '\ufeff<?xml version="1.0"?>\r\n<types>\r\n<!-- ORIGINAL <type name="Fake"/> -->\r\n\t<type name="a">\r\n\t\t<nominal>1</nominal>\r\n\t</type>\r\n<!-- TAIL -->\r\n</types>\r\n'
    after, result = bot._update_merge("types.xml", xml(1), xml(2), own, "Test")
    assert result["eintraege"][0]["lage"] == "update"
    before_block = bot._update_index(own, "types.xml")["ordered"][0]
    after_block = bot._update_index(after, "types.xml")["ordered"][0]
    assert own[:before_block["start"]].encode() == after[:after_block["start"]].encode()
    assert own[before_block["end"]:].encode() == after[after_block["end"]:].encode()
    assert '\n' not in after.replace('\r\n', '') and after.startswith('\ufeff')
    same, _ = bot._update_merge("types.xml", xml(1), '<types><type name="A"> <nominal> 1 </nominal> </type></types>', own, "Test")
    assert same == own


def test_append_comments_duplicates_and_opt_in_conflict():
    own = xml(9, '<type name="a"><nominal>7</nominal></type>')
    new = xml(2, '<type name="B"/>')
    after, result = bot._update_merge("types.xml", xml(1), new, own, "target")
    assert result["doppelte"] == ["a"] and '<nominal>9</nominal>' in after and '<nominal>7</nominal>' in after
    assert '<!-- Brigarde Killfeed Update target -->' in after
    assert after.index('<type name="B"/>') < after.index('</types>')
    after, _ = bot._update_merge("types.xml", xml(1), new, own, "target", {"entscheidungen": {"a": "uebernehmen"}, "doppelte_entfernen": True, "kommentar": False})
    assert '<nominal>2</nominal>' in after and '<nominal>7</nominal>' not in after and '<!--' not in after
    after, _ = bot._update_merge("types.xml", xml(1), xml(), own, "target", {"entscheidungen": {"a": "entfernen"}})
    assert bot._update_index(after, "types.xml")["entries"] == {}


@pytest.mark.parametrize("name", list(bot._UPDATE_DATEIEN))
def test_all_nine_schemas(name):
    _path, root, tags = bot._UPDATE_DATEIEN[name]
    tag = tags[0]
    base = f'<{root}><{tag} name="A" value="1"/></{root}>'
    new = f'<{root}><{tag} name="A" value="2"/></{root}>'
    after, result = bot._update_merge(name, base, new, base, "test")
    assert 'value="2"' in after and result["uebernommen"] == 1


def test_presets_attachments_and_anonymous_messages_and_empty_root():
    before = '<randompresets><cargo name="same"/><attachments name="same"/></randompresets>'
    assert len(bot._update_index(before, "cfgrandompresets.xml")["entries"]) == 2
    own = '<messages><message><text>Mine</text></message></messages>'
    new = '<messages><message><text>New</text></message></messages>'
    after, _ = bot._update_merge("messages.xml", '<messages/>', new, own, "test")
    assert 'Mine' in after and 'New' in after
    after, _ = bot._update_merge("types.xml", '<types/>', xml(1), '\ufeff<types/>', "test")
    assert after.startswith('\ufeff<types>') and '<nominal>1</nominal>' in after


@pytest.mark.parametrize("folder", ["chernarusplus", "enoch", "sakhal"])
def test_real_two_stands_all_files(folder):
    for name in bot._UPDATE_DATEIEN:
        base = (FIXTURES / folder / "basis" / name).read_text()
        new = (FIXTURES / folder / "neu" / name).read_text()
        after, result = bot._update_merge(name, base, new, base, "fixture")
        assert result["zaehler"]["konflikt"] == 0
        final = bot._update_index(after, name)["entries"]
        assert all(key in final and final[key]["form"] == row["form"] for key, row in bot._update_index(new, name)["entries"].items())


def test_full_types_performance():
    block = '<type name="Item{0}"><nominal>{1}</nominal><min>1</min><lifetime>7200</lifetime><description>{2}</description></type>\n'
    old = '<types>\n' + ''.join(block.format(i, 1, 'X' * 650) for i in range(1700)) + '</types>\n'
    new = '<types>\n' + ''.join(block.format(i, 2, 'X' * 650) for i in range(1700)) + '</types>\n'
    started = time.perf_counter(); after, result = bot._update_merge("types.xml", old, new, old, "performance"); elapsed = time.perf_counter() - started
    print(f"Chernarus-sized types.xml: {len(old.encode())} bytes, 1700 entries, {elapsed:.4f} s")
    assert elapsed < 2 and result["uebernommen"] == 1700 and after.count('<nominal>2</nominal>') == 1700


@pytest.fixture
def official(monkeypatch):
    base = {name: '<' + spec[1] + '></' + spec[1] + '>' for name, spec in bot._UPDATE_DATEIEN.items()}
    base["types.xml"] = xml(1); base["events.xml"] = '<events><event name="E"><nominal>1</nominal></event></events>'
    new = dict(base); new["types.xml"] = xml(2); new["events.xml"] = '<events><event name="E"><nominal>2</nominal></event></events>'
    calls = []
    async def load(karte, key, name):
        bot._update_stand(karte, key); calls.append((karte, key, name))
        return base[name] if key == bot._UPDATE_STAENDE[karte][2]["key"] else new[name]
    monkeypatch.setattr(bot, "_update_laden", load)
    return base, new, calls


def seed(a, official):
    base, _new, _calls = official
    a.ftp.files = {"/mission/" + bot._UPDATE_DATEIEN[name][0]: raw for name, raw in base.items()}


def payload(a, names=("types.xml",), apply=False):
    stands = bot._UPDATE_STAENDE["Livonia"]
    return {"von": stands[2]["key"], "auf": stands[1]["key"], "dateien":
            [{"name": name, "source_hash": bot._update_hash(a.ftp.files["/mission/" + bot._UPDATE_DATEIEN[name][0]])} for name in names] if apply else list(names)}


def test_api_get_compare_apply_undo_tenants(monkeypatch, servers, official):
    a, b = servers; seed(a, official); seed(b, official)
    disk = pathlib.Path('connections.json').read_text(); originals = copy.deepcopy(a.ftp.files)
    status, result = call(monkeypatch, a, bot.api_tools_updateassistant_get)
    assert status == 200 and len(result["data"]["dateien"]) == 9
    status, result = call(monkeypatch, a, bot.api_tools_updateassistant_vergleich, payload(a))
    assert status == 200, result
    assert result["data"]["dateien"][0]["zaehler"]["update"] == 1
    assert a.ftp.writes == [] and originals == a.ftp.files and disk == pathlib.Path('connections.json').read_text()
    malicious = payload(a, apply=True); malicious["dateien"][0]["xml"] = '<types><evil/></types>'
    status, result = call(monkeypatch, a, bot.api_tools_updateassistant_anwenden, malicious)
    assert status == 200, result
    entry = result["data"]["lauf"]; row = entry["dateien"][0]
    assert a.ftp.writes == ["/mission/" + row["bak"], "/mission/db/types.xml"]
    assert '<evil' not in a.ftp.files['/mission/db/types.xml']
    assert json.loads(pathlib.Path('connections.json').read_text())["1000"]["update_laeufe"][0]["id"] == entry["id"]
    assert call(monkeypatch, b, bot.api_tools_updateassistant_get)[1]["data"]["laeufe"] == []
    assert call(monkeypatch, b, bot.api_tools_updateassistant_zurueck, {"id": entry["id"]})[0] == 404
    assert call(monkeypatch, a, bot.api_tools_updateassistant_zurueck, {"id": entry["id"]})[0] == 200
    assert a.ftp.files['/mission/db/types.xml'] == originals['/mission/db/types.xml']
    assert "update_laeufe" not in bot._GUILD_SCHLUESSEL


@pytest.mark.parametrize("key", ["../master", "https://evil.invalid/", "missing", 1, None])
def test_unknown_ref_rejected_without_write(monkeypatch, servers, official, key):
    a, _ = servers; seed(a, official); value = payload(a, apply=True); value['auf'] = key
    assert call(monkeypatch, a, bot.api_tools_updateassistant_anwenden, value)[0] == 400
    assert a.ftp.writes == []


def test_hash_conflict_xml_source_error_no_partial_write(monkeypatch, servers, official):
    a, _ = servers; seed(a, official)
    value = payload(a, apply=True); value['dateien'][0]['source_hash'] = 'outdated'
    assert call(monkeypatch, a, bot.api_tools_updateassistant_anwenden, value)[0] == 409
    a.ftp.files['/mission/db/types.xml'] = '<types><broken>'
    assert call(monkeypatch, a, bot.api_tools_updateassistant_anwenden, payload(a, apply=True))[0] == 409
    seed(a, official)
    async def fail(*_args): raise OSError('Bohemia-Quelle nicht erreichbar oder ungültig.')
    monkeypatch.setattr(bot, '_update_laden', fail)
    assert call(monkeypatch, a, bot.api_tools_updateassistant_anwenden, payload(a, apply=True))[0] == 502
    assert a.ftp.writes == []


def test_rollback_partial_second_file_and_manifest_failure(monkeypatch, servers, official):
    a, _ = servers; seed(a, official); original = copy.deepcopy(a.ftp.files)
    async def delete(conn, path, _loop): conn.ftp.files.pop('/mission/' + path, None); return True
    monkeypatch.setattr(bot, '_tools_datei_loeschen', delete)
    async def write(conn, path, raw, _loop):
        conn.ftp.files['/mission/' + path] = raw; conn.ftp.writes.append(path)
        return len(conn.ftp.writes) != 4
    monkeypatch.setattr(bot, '_tools_datei_schreiben', write)
    status, result = call(monkeypatch, a, bot.api_tools_updateassistant_anwenden, payload(a, ('types.xml','events.xml'), True))
    assert status == 502, result
    assert a.ftp.files == original and 'update_laeufe' not in a.data
    a.ftp.writes.clear()
    save = bot._conn_store
    def fail_store(*args, **kwargs):
        if args[1] == 'update_laeufe': raise OSError('manifest failure')
        return save(*args, **kwargs)
    monkeypatch.setattr(bot, '_conn_store', fail_store)
    status, _ = call(monkeypatch, a, bot.api_tools_updateassistant_anwenden, payload(a, apply=True))
    assert status == 502 and a.ftp.files == original


def test_undo_external_change_and_bad_backup(monkeypatch, servers, official):
    a, _ = servers; seed(a, official)
    status, result = call(monkeypatch, a, bot.api_tools_updateassistant_anwenden, payload(a, apply=True)); assert status == 200
    entry = result['data']['lauf']; current = a.ftp.files['/mission/db/types.xml']; writes = len(a.ftp.writes)
    a.ftp.files['/mission/db/types.xml'] += ' '
    assert call(monkeypatch, a, bot.api_tools_updateassistant_zurueck, {'id':entry['id']})[0] == 409 and len(a.ftp.writes) == writes
    a.ftp.files['/mission/db/types.xml'] = current; a.ftp.files['/mission/'+entry['dateien'][0]['bak']] = '<types/>'
    assert call(monkeypatch, a, bot.api_tools_updateassistant_zurueck, {'id':entry['id']})[0] == 409


def test_detection_and_only_new_cross_errors(monkeypatch, servers, official):
    a, _ = servers; seed(a, official)
    status, result = call(monkeypatch, a, bot.api_tools_updateassistant_erkennen, {'datei':'types.xml'})
    assert status == 200 and result['data']['vorschlag']['key'] == bot._UPDATE_STAENDE['Livonia'][2]['key']
    a.ftp.files['/mission/db/types.xml'] = xml(1,'<type name="Custom"/>')
    status, result = call(monkeypatch, a, bot.api_tools_updateassistant_erkennen, {'datei':'types.xml'})
    assert status == 200 and result['data']['vorschlag']['prozent'] == 50
    before = {'db/types.xml':xml(1),'db/events.xml':'<events><event name="A"><children><child type="OldMissing"/></children></event></events>'}
    after = dict(before); after['db/events.xml'] = '<events><event name="A"><children><child type="OldMissing"/><child type="NewMissing"/></children></event></events>'
    issues = bot._update_neue_probleme(before, after, 'Livonia')
    assert len(issues) == 1 and 'NewMissing' in issues[0]['message']


def test_new_errors_require_explicit_confirm(monkeypatch, servers, official):
    a, _ = servers; seed(a, official)
    official[1]['events.xml'] = '<events><event name="E"><children><child type="Missing"/></children></event></events>'
    value = payload(a, ('events.xml',), True)
    status, result = call(monkeypatch, a, bot.api_tools_updateassistant_anwenden, value)
    assert status == 409 and result['probleme'] and not a.ftp.writes
    value['bestaetigt_fehler'] = True
    assert call(monkeypatch, a, bot.api_tools_updateassistant_anwenden, value)[0] == 200


def test_scalar_diff_utf8_limit_and_missing_name(monkeypatch, servers, official):
    result, _ = bot._update_vergleichen('types.xml', xml(1), xml(2), xml(1))
    assert result['eintraege'][0]['kurzdiff'] == [{'feld':'nominal', 'alt':'1', 'neu':'2'}]
    raw = xml(1, '<type name="Long"><description>' + 'ü' * 5000 + '</description></type>')
    result, _ = bot._update_vergleichen('types.xml', xml(1), raw, xml(1))
    assert all(len(row['neu'].encode()) <= 4096 for row in result['eintraege'])
    a, _ = servers; seed(a, official)
    a.ftp.files['/mission/db/types.xml'] = '<types><type/></types>'
    assert call(monkeypatch, a, bot.api_tools_updateassistant_anwenden, payload(a, apply=True))[0] == 409
    assert not a.ftp.writes


def test_source_changes_during_download_checked_before_backups(monkeypatch, servers, official):
    a, _ = servers; seed(a, official)
    load = bot._update_laden
    async def changes_during_download(*args):
        a.ftp.files['/mission/db/types.xml'] = xml(999)
        return await load(*args)
    monkeypatch.setattr(bot, '_update_laden', changes_during_download)
    assert call(monkeypatch, a, bot.api_tools_updateassistant_anwenden, payload(a, apply=True))[0] == 409
    assert not a.ftp.writes and a.ftp.files['/mission/db/types.xml'] == xml(999)


def test_all_routes_use_module_and_action_gate(monkeypatch, servers, official):
    a, _ = servers; seed(a, official); calls = []
    async def denied(_request, module, action):
        calls.append((module, action)); return a, bot.err('Denied', 403)
    monkeypatch.setattr(bot, '_brlc_prepare', denied)
    handlers = [(bot.api_tools_updateassistant_get, 'view'), (bot.api_tools_updateassistant_erkennen, 'view'),
                (bot.api_tools_updateassistant_vergleich, 'view'), (bot.api_tools_updateassistant_anwenden, 'edit'),
                (bot.api_tools_updateassistant_zurueck, 'edit')]
    for handler, action in handlers:
        assert call(monkeypatch, a, handler, {})[0] == 403
        assert calls[-1] == ('tools.updateassistant', action)
    assert not a.ftp.writes and not official[2]


def test_rate_limit_and_twenty_history_entries(monkeypatch, servers, official):
    a, _ = servers; seed(a, official)
    a.data['update_laeufe'] = [{'id': str(i)} for i in range(20)]
    status, result = call(monkeypatch, a, bot.api_tools_updateassistant_anwenden, payload(a, apply=True), session='update-rate-test')
    assert status == 200 and len(a.data['update_laeufe']) == 20 and a.data['update_laeufe'][0]['id'] == '1'
    assert a.data['update_laeufe'][-1]['id'] == result['data']['lauf']['id']
    count = len(a.ftp.writes)
    assert call(monkeypatch, a, bot.api_tools_updateassistant_anwenden, payload(a, apply=True), session='update-rate-test')[0] == 429
    assert len(a.ftp.writes) == count


def test_undo_second_write_failure_restores_current_files(monkeypatch, servers, official):
    a, _ = servers; seed(a, official)
    status, result = call(monkeypatch, a, bot.api_tools_updateassistant_anwenden, payload(a, ('types.xml','events.xml'), True)); assert status == 200
    current = copy.deepcopy(a.ftp.files); history = copy.deepcopy(a.data['update_laeufe']); writes = []
    async def partial(conn, path, raw, _loop):
        conn.ftp.files['/mission/' + path] = raw; writes.append(path)
        return len(writes) != 2
    monkeypatch.setattr(bot, '_tools_datei_schreiben', partial)
    assert call(monkeypatch, a, bot.api_tools_updateassistant_zurueck, {'id':result['data']['lauf']['id']})[0] == 502
    assert a.ftp.files == current and a.data['update_laeufe'] == history


@pytest.mark.parametrize('mode', ['ok', 'large', 'redirect', 'broken', 'missing'])
def test_http_loader_fixed_source_limits_timeout_cache(monkeypatch, mode):
    calls = []; bot._UPDATE_CACHE.clear()
    class Response:
        status = 404 if mode == 'missing' else 302 if mode == 'redirect' else 200
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return False
        @property
        def content(self): return self
        async def iter_chunked(self, size):
            assert size == 65536
            if mode == 'large':
                for _ in range(321): yield b'x' * 65536
            else: yield b'<broken>' if mode == 'broken' else xml(1).encode()
    class Session:
        def __init__(self, **kwargs):
            assert kwargs['trust_env'] is True and kwargs['timeout'].total == 15
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return False
        def get(self, url, **kwargs):
            calls.append(url); assert kwargs == {'allow_redirects':False}; return Response()
    monkeypatch.setattr(bot.aiohttp, 'ClientSession', Session)
    stand = bot._UPDATE_STAENDE['ChernarusPlus'][1]
    if mode in ('large','redirect','broken'):
        with pytest.raises(OSError, match='Bohemia-Quelle'):
            asyncio.run(bot._update_laden('ChernarusPlus', stand['key'], 'types.xml'))
    else:
        expected = '<types></types>' if mode == 'missing' else xml(1)
        assert asyncio.run(bot._update_laden('ChernarusPlus', stand['key'], 'types.xml')) == expected
        assert asyncio.run(bot._update_laden('ChernarusPlus', stand['key'], 'types.xml')) == expected
        assert len(calls) == 1
    assert calls[0] == ('https://raw.githubusercontent.com/BohemiaInteractive/DayZ-Central-Economy/' + stand['ref'] + '/dayzOffline.chernarusplus/db/types.xml')
    bot._UPDATE_CACHE.clear()


def test_readable_diff_and_silent_identical_rows():
    """Kurzdiff zeigt XML statt Python-Tupel; „unverändert“ überträgt weder Blöcke noch Diff."""
    base = '<types><type name="A"><nominal>1</nominal><flags count_in_map="1"/></type><type name="B"><nominal>5</nominal></type></types>'
    new = '<types><type name="A"><nominal>2</nominal><flags count_in_map="0"/></type><type name="B"><nominal>5</nominal></type></types>'
    result, _ = bot._update_vergleichen("types.xml", base, new, base)
    rows = {row["name"]: row for row in result["eintraege"]}
    diff = {item["feld"]: item for item in rows["A"]["kurzdiff"]}
    assert diff["flags"]["alt"] == '<flags count_in_map="1" />' and diff["flags"]["neu"] == '<flags count_in_map="0" />'
    assert diff["nominal"] == {"feld": "nominal", "alt": "1", "neu": "2"}
    assert rows["A"]["alt"] and rows["A"]["neu"]
    assert rows["B"]["lage"] == "unveraendert" and rows["B"]["kurzdiff"] == []
    assert not (rows["B"]["alt"] or rows["B"]["neu"] or rows["B"]["basis"])


def test_get_ist_leicht_und_blockiert_nie(monkeypatch, servers, official):
    """Oeffnen des Tools: kein Laden der Dateien, kein Abbruch bei Zusatzordnern/Lesefehlern."""
    a, b = servers; seed(a, official)
    # Ohne Mission-Ordner / FTP: 200 mit Hinweis statt Fehler
    b.data["ftp_mission_dir"] = ""
    status, result = call(monkeypatch, b, bot.api_tools_updateassistant_get)
    assert status == 200 and result["data"]["kein_mission_ordner"] is True
    assert not any(row["vorhanden"] for row in result["data"]["dateien"])
    # Lesefehler einer Datei, kaputter Verzeichnis-Durchlauf: Tool oeffnet trotzdem
    read = a.ftp.read_file_ex
    a.ftp.read_file_ex = lambda path: (_ for _ in ()).throw(RuntimeError("FTP weg")) if path.endswith("events.xml") else read(path)
    a.ftp.walk = lambda *args, **kw: ([], "zu_gross")
    status, result = call(monkeypatch, a, bot.api_tools_updateassistant_get)
    assert status == 200
    rows = {row["name"]: row for row in result["data"]["dateien"]}
    assert rows["types.xml"]["vorhanden"] and not rows["events.xml"]["vorhanden"]
    assert "hash" not in rows["types.xml"] and a.ftp.writes == []


def test_vergleich_toleriert_kaputten_durchlauf_und_meldet_fehlende_auswahl(monkeypatch, servers, official):
    a, _ = servers; seed(a, official)
    seen = {}
    def walk(root, max_dateien=0, max_bytes=0, ueberspringen=None):
        seen["skip"] = ueberspringen
        return [], "zu_gross"     # z. B. Persistenz-Ordner sprengt die Mengengrenze
    a.ftp.walk = walk
    status, result = call(monkeypatch, a, bot.api_tools_updateassistant_vergleich, payload(a))
    assert status == 200, result
    assert "storage_1" in seen["skip"]                      # Persistenz wird gar nicht erst durchlaufen
    del a.ftp.files["/mission/db/types.xml"]
    status, result = call(monkeypatch, a, bot.api_tools_updateassistant_vergleich, payload(a))
    assert status == 404 and "nicht vorhanden" in result["error"]
    a.data["ftp_mission_dir"] = ""
    status, result = call(monkeypatch, a, bot.api_tools_updateassistant_vergleich, payload(a))
    assert status == 404 and "Mission-Ordner" in result["error"]


def test_import_vergleicht_ohne_server_und_schreibt_nie(monkeypatch, servers, official):
    """Manuell importierte Datei: Vergleich + Ergebnis auch ohne FTP/Mission-Ordner, nichts wird geschrieben."""
    a, b = servers; base, new, _calls = official
    b.data["ftp_mission_dir"] = ""                       # Server ohne Dateizugriff (z. B. Konsole)
    p = payload(a)
    p["dateien"] = ["types.xml"]; p["importiert"] = {"types.xml": base["types.xml"]}
    status, result = call(monkeypatch, b, bot.api_tools_updateassistant_vergleich, p)
    assert status == 200, result
    row = result["data"]["dateien"][0]
    assert row["quelle"] == "import" and row["zaehler"]["update"] == 1
    status, result = call(monkeypatch, b, bot.api_tools_updateassistant_ergebnis, p)
    assert status == 200, result
    gen = result["data"]["generated"][0]
    assert gen["filename"] == "types.xml" and gen["content"] != base["types.xml"]
    assert gen["content"] == bot._update_merge("types.xml", base["types.xml"], new["types.xml"], base["types.xml"], "x")[0]   # genau die Server-Logik
    assert b.ftp.writes == [] and "update_laeufe" not in b.data
    # Ohne Import fehlt der Zugang weiterhin mit klarer Meldung
    p2 = payload(a); p2["dateien"] = ["types.xml"]
    status, result = call(monkeypatch, b, bot.api_tools_updateassistant_vergleich, p2)
    assert status == 404 and "Mission-Ordner" in result["error"]


def test_import_ersetzt_serverdatei_und_gemischte_auswahl(monkeypatch, servers, official):
    a, _ = servers; base, new, _calls = official; seed(a, official)
    a.ftp.files["/mission/db/types.xml"] = new["types.xml"]            # Server ist schon aktuell …
    p = payload(a); p["dateien"] = ["types.xml", "events.xml"]
    p["importiert"] = {"types.xml": base["types.xml"]}                  # … importiert wird aber der alte Stand
    status, result = call(monkeypatch, a, bot.api_tools_updateassistant_vergleich, p)
    assert status == 200, result
    rows = {row["name"]: row for row in result["data"]["dateien"]}
    assert rows["types.xml"]["quelle"] == "import" and rows["types.xml"]["zaehler"]["update"] == 1
    assert rows["events.xml"]["quelle"] == "server"
    assert a.ftp.writes == []


def test_import_fehler_und_anwenden_verweigert(monkeypatch, servers, official):
    a, _ = servers; base, _new, _calls = official; seed(a, official)
    p = payload(a); p["dateien"] = ["types.xml"]
    for bad, teil in (({"types.xml": "<types><type name='A'>"}, "keine gültige Update-XML"),
                      ({"types.xml": "<events/>"}, "keine gültige Update-XML"),
                      ({"types.xml": "<!DOCTYPE x><types/>"}, "keine gültige Update-XML"),
                      ({"passwd": "x"}, "Unbekannte"), ({"types.xml": 5}, "Unbekannte"), ({"types.xml": "  "}, "Unbekannte"),
                      ({"types.xml": "<types>" + "<type name='A'><nominal>1</nominal></type>" * 180000 + "</types>"}, "zu groß"),
                      (["types.xml"], "Ungültige")):
        status, result = call(monkeypatch, a, bot.api_tools_updateassistant_vergleich, {**p, "importiert": bad})
        assert status == 400 and teil in result["error"], (bad if len(str(bad)) < 80 else "gross", result)
    # Anwenden mit Import: nie auf den Server schreiben
    q = payload(a, apply=True); q["importiert"] = {"types.xml": base["types.xml"]}
    status, result = call(monkeypatch, a, bot.api_tools_updateassistant_anwenden, q)
    assert status == 400 and "nicht auf den Server geschrieben" in result["error"] and a.ftp.writes == []


def test_erkennen_mit_importiertem_text_und_mandanten(monkeypatch, servers, official):
    a, b = servers; base, _new, _calls = official; b.data["ftp_mission_dir"] = ""
    status, result = call(monkeypatch, b, bot.api_tools_updateassistant_erkennen, {"datei": "types.xml", "text": base["types.xml"]})
    assert status == 200, result
    assert result["data"]["vorschlag"]["prozent"] >= 0 and result["data"]["kandidaten"]
    status, result = call(monkeypatch, b, bot.api_tools_updateassistant_erkennen, {"datei": "types.xml", "text": "<types><kaputt"})
    assert status == 400 and "keine gültige Update-XML" in result["error"]


def test_import_findet_fehlende_eintraege_und_haengt_sie_an(monkeypatch, servers, official):
    """Kernfall des Imports: Eintraege fehlen in der eigenen Datei → werden gemeldet und im Ergebnis ergaenzt."""
    a, b = servers; base, new, _calls = official
    b.data["ftp_mission_dir"] = ""
    new_types = '<types><type name="A"><nominal>1</nominal></type><type name="Fehlt1"><nominal>3</nominal></type><type name="Fehlt2"><nominal>4</nominal></type></types>'
    official_new = dict(new); official_new["types.xml"] = new_types
    async def load(karte, key, name):
        return base[name] if key == bot._UPDATE_STAENDE[karte][2]["key"] else official_new[name]
    monkeypatch.setattr(bot, "_update_laden", load)
    p = payload(a); p["dateien"] = ["types.xml"]
    p["importiert"] = {"types.xml": '<types><type name="A"><nominal>1</nominal></type></types>'}   # eigene Datei: Fehlt1/Fehlt2 fehlen
    status, result = call(monkeypatch, b, bot.api_tools_updateassistant_vergleich, p)
    assert status == 200, result
    row = result["data"]["dateien"][0]
    assert row["zaehler"]["neu"] == 2 and {e["name"] for e in row["eintraege"] if e["lage"] == "neu"} == {"Fehlt1", "Fehlt2"}
    status, result = call(monkeypatch, b, bot.api_tools_updateassistant_ergebnis, p)
    assert status == 200, result
    content = result["data"]["generated"][0]["content"]
    assert 'name="Fehlt1"' in content and 'name="Fehlt2"' in content and "Brigarde Killfeed Update" in content
    assert content.rstrip().endswith("</types>") and b.ftp.writes == []
    # Abgewaehlter Eintrag bleibt draussen
    p["optionen"] = {"types.xml": {"entscheidungen": {"fehlt2": "behalten"}}}
    status, result = call(monkeypatch, b, bot.api_tools_updateassistant_ergebnis, p)
    assert 'name="Fehlt1"' in result["data"]["generated"][0]["content"] and 'name="Fehlt2"' not in result["data"]["generated"][0]["content"]
