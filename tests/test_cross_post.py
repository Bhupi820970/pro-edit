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
