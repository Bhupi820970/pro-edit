import pipeline as P


def test_twin_for_other_platform(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "home", lambda: tmp_path)
    (tmp_path / "ready" / "youtube").mkdir(parents=True)
    out = tmp_path / "ready" / "youtube" / "yt-movie-1.mp4"
    out.write_bytes(b"v")
    prev = tmp_path / "prev.mp4"
    prev.write_bytes(b"p")
    e = {"id": "yt-movie-1", "platform": "youtube", "format": "shorts", "preview": str(prev), "state": "ready"}
    t = P.cross_post_twin(e, out, {})
    assert t["platform"] == "instagram" and t["id"] == "in-movie-1" and t["twin_of"] == "yt-movie-1"
    assert (tmp_path / "ready" / "instagram" / "in-movie-1.mp4").exists()
    assert P.cross_post_twin(e, out, {"cross_post": False}) is None
    assert P.cross_post_twin({**e, "format": "wide"}, out, {}) is None


def test_junk_titles_blocked():
    from pipeline_lib.titles import trailer_blocked
    assert trailer_blocked("Bloopers | Percy Jackson and the Olympians | Disney+", "series")
    assert trailer_blocked("Season 3 Watch Party | Disney+", "movie")
    assert not trailer_blocked("The Wedding Dress | Percy Jackson and the Olympians", "series")
    assert not trailer_blocked("LeBron James postgame interview", "sports")


def test_work_title_skips_channel_names():
    from pipeline_lib.titles import work_title
    assert work_title("GOAT (2026) 4K - Goat Tears Diss Track (4/12) | Movieclips", "movie") == "GOAT"
    assert work_title("GOAT - Film Clip | Chillin' in the Penthouse", "movie") == "GOAT"
    assert work_title("The Batman - Ending Monologue from \u201cThe Batman\u201d | Super Scenes | DC", "dc") == "The Batman"
