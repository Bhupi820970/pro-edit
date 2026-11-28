from pipeline_lib import llm_fallback as L


def test_parse_json_list_handles_fences_and_wrappers():
    assert L.parse_json_list('```json\n[{"title":"A","artist":"B","why":"x"}]\n```')[0]["title"] == "A"
    assert L.parse_json_list('{"songs":[{"title":"A","artist":"B"}]}')[0]["artist"] == "B"
    assert L.parse_json_list("<think>[1]</think>no json") == []


def test_env_keys_dedupes_and_orders(monkeypatch):
    for n in ("X_KEY", "X_KEY_2", "X_KEY_3", "X_KEY_4", "X_KEY_5"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("X_KEY", "a"); monkeypatch.setenv("X_KEY_2", "a"); monkeypatch.setenv("X_KEY_3", "b")
    assert L.env_keys("X_KEY") == ["a", "b"]


def test_no_keys_means_no_backup(monkeypatch):
    for p in ("OPENROUTER_API_KEY", "GROQ_API_KEY"):
        for s in ("", "_2", "_3", "_4", "_5"):
            monkeypatch.delenv(p + s, raising=False)
    assert L.backup_song_options("p", "action", []) == []


def test_rotates_key_on_429(monkeypatch):
    calls = []

    class E(Exception):
        code = 429

    def fake(url, key, body, timeout):
        calls.append(key)
        if key == "k1":
            raise E()
        return '[{"title":"T","artist":"A","why":"w"}]'
    monkeypatch.setattr(L, "_post", fake)
    assert L.ask("u", ["k1", "k2"], ["m"], "c")[0]["title"] == "T"
    assert calls == ["k1", "k2"]
