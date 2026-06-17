import base64

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestSocialPostType(TransactionCase):
    """Instagram/Facebook Story + Reel support via x_social_post_type.

    Covers the media_type / facebook_media_type mapping sent to posts.agency
    and the Reel/Story validation rules. No network: _social_media_type_params
    is pure, and _social_validate raises before any HTTP call.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.project = cls.env["project.project"].create({"name": "Social Test"})
        # is_connected is a plain boolean; platform_list() parses this CSV.
        cls.profile = cls.env["mint.social.profile"].create({
            "name": "teststory",
            "connected_platforms": "instagram,facebook,youtube,tiktok",
            "is_connected": True,
        })
        cls.future = "2099-01-01 12:00:00"

    def _task(self, **vals):
        base = {"name": "Post", "project_id": self.project.id}
        base.update(vals)
        return self.env["project.task"].create(base)

    def _attach(self, task, mimetype, name):
        return self.env["ir.attachment"].create({
            "name": name,
            "res_model": "project.task",
            "res_id": task.id,
            "res_field": False,
            "mimetype": mimetype,
            "datas": base64.b64encode(b"x"),
        })

    # --- media_type mapping (pure) ---------------------------------------
    def test_auto_sends_no_media_type(self):
        t = self._task(x_social_post_type="auto")
        self.assertEqual(t._social_media_type_params(["instagram", "facebook"]), [])

    def test_reel_instagram_only(self):
        t = self._task(x_social_post_type="reel")
        self.assertEqual(
            t._social_media_type_params(["instagram"]), [("media_type", "REELS")]
        )

    def test_story_maps_both_ig_and_fb(self):
        t = self._task(x_social_post_type="story")
        self.assertEqual(
            t._social_media_type_params(["instagram", "facebook"]),
            [("media_type", "STORIES"), ("facebook_media_type", "STORIES")],
        )

    def test_non_ig_fb_platforms_get_nothing(self):
        t = self._task(x_social_post_type="reel")
        self.assertEqual(t._social_media_type_params(["tiktok", "youtube"]), [])

    # --- validation rules ------------------------------------------------
    def test_reel_rejects_image(self):
        t = self._task(
            x_social_post_type="reel",
            x_social_profile_id=self.profile.id,
            x_social_platforms="instagram",
            date_deadline=self.future,
        )
        self._attach(t, "image/png", "p.png")
        with self.assertRaises(UserError) as e:
            t._social_validate()
        self.assertIn("Reel", str(e.exception))

    def test_story_requires_ig_or_fb(self):
        t = self._task(
            x_social_post_type="story",
            x_social_profile_id=self.profile.id,
            x_social_platforms="youtube",
            date_deadline=self.future,
        )
        self._attach(t, "video/mp4", "v.mp4")
        with self.assertRaises(UserError) as e:
            t._social_validate()
        self.assertIn("Instagram or Facebook", str(e.exception))

    def test_reel_video_instagram_passes(self):
        t = self._task(
            x_social_post_type="reel",
            x_social_profile_id=self.profile.id,
            x_social_platforms="instagram",
            date_deadline=self.future,
        )
        self._attach(t, "video/mp4", "v.mp4")
        # Should not raise — valid Reel.
        t._social_validate()
