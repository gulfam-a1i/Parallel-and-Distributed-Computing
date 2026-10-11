"""FFmpeg command building for each encoder family (no FFmpeg needed)."""

import unittest

from common import ffmpeg as ff


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.cfg = ff.validate_job({"codec": "h264", "resolution": "1080p", "bitrate_kbps": 8000,
                                    "preset": "p4", "container": "mp4"})

    def cmd(self, encoder, hw_decode=True):
        return ff.build_command(self.cfg, "in.mp4", "out.mp4", encoder, hw_decode=hw_decode)

    def value(self, cmd, flag):
        return cmd[cmd.index(flag) + 1]

    def test_nvenc(self):
        c = self.cmd("h264_nvenc")
        self.assertEqual(self.value(c, "-hwaccel"), "cuda")
        self.assertEqual(self.value(c, "-preset"), "p4")
        self.assertEqual(self.value(c, "-pix_fmt"), "yuv420p")

    def test_quick_sync(self):
        c = self.cmd("h264_qsv")
        self.assertEqual(self.value(c, "-preset"), "medium")
        self.assertEqual(self.value(c, "-pix_fmt"), "nv12")      # QSV rejects yuv420p

    def test_amf(self):
        c = self.cmd("hevc_amf")
        self.assertEqual(self.value(c, "-quality"), "balanced")
        self.assertEqual(self.value(c, "-pix_fmt"), "nv12")

    def test_cpu(self):
        c = self.cmd("libx264", hw_decode=False)
        self.assertNotIn("-hwaccel", c)
        self.assertEqual(self.value(c, "-preset"), "faster")

    def test_family_detection(self):
        self.assertEqual(ff.encoder_family("h264_qsv"), "qsv")
        self.assertEqual(ff.encoder_family("hevc_nvenc"), "nvenc")
        self.assertEqual(ff.encoder_family("libx265"), "cpu")

    def test_bad_settings_rejected(self):
        for bad in ({"codec": "vp9"}, {"resolution": "999p"}, {"preset": "p9"}, {"bitrate_kbps": 5}):
            with self.assertRaises(ValueError):
                ff.validate_job(dict(self.cfg, **bad))


if __name__ == "__main__":
    unittest.main()
