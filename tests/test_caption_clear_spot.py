from PIL import Image
import vicestyle as vs


def test_clear_spot_avoids_faces():
    W, H = 2560, 1440
    bar = int(round((H - W * 9 / 21) / 2))
    faces = [(0.30, 0.30, 0.45, 0.60), (0.55, 0.30, 0.70, 0.60)]   # two big faces mid-frame
    img = Image.new("RGBA", (900, 300))
    got = vs._clear_spot(img, faces, W, H, bar + 8, H - bar - 8, int(W * 0.05), (W // 2, H // 2))
    assert got is not None
    im, x, y, ov = got
    box = (x + 12, y + 12, x + im.width - 12, y + im.height - 12)
    assert ov == 0
    assert not any(vs._inter(box, pb) for pb in vs.person_boxes(faces, W, H, tight=True))
