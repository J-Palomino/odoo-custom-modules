import base64
import struct

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from ..models.project_task import _mp4_duration_seconds


def _mp4_bytes(duration_s, timescale=1):
    """Minimal ISO-BMFF buffer: moov > mvhd (v0) encoding a duration. Not a
    playable video — just a parseable box tree for the duration reader."""
    # mvhd v0 payload: version+flags(4) creation(4) modification(4)
    #                  timescale(4) duration(4) ... (rest omitted, parser only
    #                  reads through duration).
    payload = (
        b"\x00\x00\x00\x00"           # version 0 + flags
        + b"\x00\x00\x00\x00"          # creation time
        + b"\x00\x00\x00\x00"          # modification time
        + struct.pack(">I", timescale)
        + struct.pack(">I", duration_s)
    )
    mvhd = struct.pack(">I", 8 + len(payload)) + b"mvhd" + payload
    moov = struct.pack(">I", 8 + len(mvhd)) + b"moov" + mvhd
    return moov


@tagged("post_install", "-at_install")
class TestMp4Duration(TransactionCase):
    def test_parses_v0_duration(self):
        self.assertEqual(_mp4_duration_seconds(_mp4_bytes(120)), 120.0)

    def test_timescale_division(self):
        self.assertEqual(_mp4_duration_seconds(_mp4_bytes(3000, timescale=600)), 5.0)

    def test_non_mp4_returns_none(self):
        self.assertIsNone(_mp4_duration_seconds(b"not a video at all"))

    def test_zero_duration_returns_none(self):
        # fragmented files report 0 in mvhd; treat as unknown, never block
        self.assertIsNone(_mp4_duration_seconds(_mp4_bytes(0)))


@tagged("post_install", "-at_install")
class TestMediaLimits(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.project = cls.env["project.project"].create({"name": "Guards Test"})
        cls.profile = cls.env["mint.social.profile"].create({
            "name": "guards",
            "connected_platforms": "instagram,facebook,x,youtube",
            "is_connected": True,
        })

    def _task(self, **vals):
        base = {
            "name": "Post",
            "project_id": self.project.id,
            "x_social_profile_id": self.profile.id,
            "date_deadline": "2099-01-01 12:00:00",
        }
        base.update(vals)
        return self.env["project.task"].create(base)

    def _attach_video(self, task, duration_s, mimetype="video/mp4"):
        return self.env["ir.attachment"].create({
            "name": "v.mp4",
            "res_model": "project.task",
            "res_id": task.id,
            "res_field": False,
            "mimetype": mimetype,
            "datas": base64.b64encode(_mp4_bytes(duration_s)),
        })

    def test_duration_limit_lookup(self):
        t = self._task()
        self.assertEqual(t._platform_duration_limit("instagram", "story"), 60)
        self.assertEqual(t._platform_duration_limit("instagram", "reel"), 900)
        # auto/feed falls back to default
        self.assertEqual(t._platform_duration_limit("x", "auto"), 140)
        # unknown platform -> no cap
        self.assertIsNone(t._platform_duration_limit("myspace", "auto"))

    def test_story_over_60s_blocks(self):
        t = self._task(x_social_platforms="instagram", x_social_post_type="story")
        self._attach_video(t, 90)  # 1:30 > 60s story cap
        with self.assertRaises(UserError) as e:
            t._social_validate()
        msg = str(e.exception)
        self.assertIn("Story", msg)
        self.assertIn("trim", msg)

    def test_story_under_limit_passes(self):
        t = self._task(x_social_platforms="instagram", x_social_post_type="story")
        self._attach_video(t, 45)  # within 60s
        t._social_validate()  # must not raise

    def test_x_video_over_140s_blocks(self):
        t = self._task(x_social_platforms="x", x_social_post_type="auto")
        self._attach_video(t, 200)  # > 140s
        problems = t._social_media_limit_problems()
        self.assertTrue(any("trim" in p for p in problems))

    def test_unparseable_video_skips_duration_guard(self):
        t = self._task(x_social_platforms="instagram", x_social_post_type="story")
        self.env["ir.attachment"].create({
            "name": "weird.webm",
            "res_model": "project.task",
            "res_id": t.id,
            "res_field": False,
            "mimetype": "video/webm",
            "datas": base64.b64encode(b"not parseable"),
        })
        # duration unknown -> no duration problem (size is tiny -> no size problem)
        self.assertEqual(t._social_media_limit_problems(), [])
