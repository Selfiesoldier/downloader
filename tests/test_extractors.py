import unittest
from unittest.mock import MagicMock, patch

import extractors
from extractors import (
    CookielessExtractorEngine,
    get_cookieless_ydl_opts,
    resolve_twitter,
    resolve_instagram,
    resolve_tiktok,
    extract_smart_formats,
)


class TestCookielessExtractors(unittest.TestCase):
    def test_youtube_opts_visionos_android_vr(self):
        """Verify get_cookieless_ydl_opts configures player_client with visionos and android_vr."""
        opts = get_cookieless_ydl_opts("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        
        self.assertIn("extractor_args", opts)
        self.assertIn("youtube", opts["extractor_args"])
        player_client = opts["extractor_args"]["youtube"].get("player_client", [])
        player_skip = opts["extractor_args"]["youtube"].get("player_skip", [])
        
        self.assertIn("visionos", player_client)
        self.assertIn("android_vr", player_client)
        self.assertNotIn("web", player_client)
        
        self.assertIn("js", player_skip)
        self.assertIn("configs", player_skip)
        self.assertIn("webpage", player_skip)
        
        self.assertTrue(opts.get("youtube_include_dash_manifest"))
        self.assertFalse(opts.get("check_formats"))

    @patch("extractors.yt_dlp.YoutubeDL")
    @patch("extractors._http_get")
    def test_twitter_fxtwitter_fallback(self, mock_http_get, mock_ydl_cls):
        """Mock rate-limited response from yt-dlp and verify resolve_twitter cascades to fxtwitter."""
        # 1. Native yt-dlp fails with rate limit
        mock_ydl_instance = MagicMock()
        mock_ydl_instance.__enter__.return_value = mock_ydl_instance
        mock_ydl_instance.extract_info.side_effect = RuntimeError("Twitter API rate limit reached")
        mock_ydl_cls.return_value = mock_ydl_instance

        # 2. Mock api.fxtwitter.com response
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "code": 200,
            "message": "OK",
            "tweet": {
                "id": "1891234567890123456",
                "text": "Check out this cool clip!",
                "author": {"name": "Twitter User", "screen_name": "twitteruser"},
                "media": {
                    "videos": [
                        {
                            "url": "https://video.twimg.com/ext_tw_video/1891/vid/avc1/720x1280/video.mp4",
                            "thumbnail_url": "https://pbs.twimg.com/media/thumb.jpg",
                            "duration": 18.5,
                            "width": 720,
                            "height": 1280,
                        }
                    ]
                },
            },
        }
        mock_http_get.return_value = mock_resp

        tweet_url = "https://x.com/twitteruser/status/1891234567890123456"
        info = resolve_twitter(tweet_url)

        self.assertEqual(info["id"], "1891234567890123456")
        self.assertEqual(info["url"], "https://video.twimg.com/ext_tw_video/1891/vid/avc1/720x1280/video.mp4")
        self.assertEqual(info["extractor"], "twitter:fxtwitter")
        self.assertEqual(info["title"], "Check out this cool clip!")
        self.assertEqual(len(info["formats"]), 1)
        self.assertEqual(info["formats"][0]["url"], "https://video.twimg.com/ext_tw_video/1891/vid/avc1/720x1280/video.mp4")

    @patch("extractors.yt_dlp.YoutubeDL")
    @patch("extractors._http_get")
    def test_instagram_embed_fallback(self, mock_http_get, mock_ydl_cls):
        """Mock Instagram response failure and verify embed cascade extracts direct video URL."""
        # 1. Native yt-dlp fails with login wall
        mock_ydl_instance = MagicMock()
        mock_ydl_instance.__enter__.return_value = mock_ydl_instance
        mock_ydl_instance.extract_info.side_effect = RuntimeError("Login required to view this reel")
        mock_ydl_cls.return_value = mock_ydl_instance

        # 2. Mock embed captioned page HTML
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = (
            '<!DOCTYPE html><html><body>'
            '<video class="EmbeddedVideo" src="https://scontent.cdninstagram.com/v/t50.2886-16/reel_sample.mp4?token=secret123" />'
            '</body></html>'
        )
        mock_http_get.return_value = mock_resp

        ig_url = "https://www.instagram.com/reel/C_testReel123/"
        info = resolve_instagram(ig_url)

        self.assertEqual(info["id"], "C_testReel123")
        self.assertIn("scontent.cdninstagram.com", info["url"])
        self.assertEqual(info["extractor"], "instagram:embed_fallback")
        self.assertEqual(len(info["formats"]), 1)
        self.assertEqual(info["formats"][0]["url"], "https://scontent.cdninstagram.com/v/t50.2886-16/reel_sample.mp4?token=secret123")

    @patch("extractors.yt_dlp.YoutubeDL")
    @patch("extractors._http_get")
    def test_tiktok_tikwm_fallback(self, mock_http_get, mock_ydl_cls):
        """Test tikwm response parser extracting HD play URL when native yt-dlp fails."""
        # 1. Native yt-dlp fails with bot verification
        mock_ydl_instance = MagicMock()
        mock_ydl_instance.__enter__.return_value = mock_ydl_instance
        mock_ydl_instance.extract_info.side_effect = RuntimeError("TikTok captcha required")
        mock_ydl_cls.return_value = mock_ydl_instance

        # 2. Mock TikWM API response
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "code": 0,
            "msg": "success",
            "data": {
                "id": "7401234567890123456",
                "title": "Viral TikTok Trend",
                "play": "https://www.tikwm.com/video/media/play/7401.mp4",
                "hdplay": "https://www.tikwm.com/video/media/hdplay/7401_hd.mp4",
                "duration": 22,
                "author": {"nickname": "CreatorNick", "unique_id": "creator"},
            },
        }
        mock_http_get.return_value = mock_resp

        tiktok_url = "https://www.tiktok.com/@creator/video/7401234567890123456"
        info = resolve_tiktok(tiktok_url)

        self.assertEqual(info["id"], "7401234567890123456")
        self.assertEqual(info["url"], "https://www.tikwm.com/video/media/hdplay/7401_hd.mp4")
        self.assertEqual(info["title"], "Viral TikTok Trend")
        self.assertEqual(info["extractor"], "tiktok:tikwm")
        self.assertEqual(info["uploader"], "CreatorNick")

    def test_smart_quality_badges(self):
        """Pass mock format dictionaries for portrait (9:16) and landscape (16:9) and verify badges."""
        # Vertical / Portrait 1080x1920 (Shorts/Reels)
        info_portrait = {
            "duration": 120,
            "formats": [
                {
                    "format_id": "1080p_v",
                    "width": 1080,
                    "height": 1920,
                    "vcodec": "h264",
                    "acodec": "none",
                    "filesize": 150 * 1024 * 1024,
                },
                {
                    "format_id": "720p_v",
                    "width": 720,
                    "height": 1280,
                    "vcodec": "h264",
                    "acodec": "none",
                    "filesize": 30 * 1024 * 1024,
                },
                {
                    "format_id": "480p_v",
                    "width": 480,
                    "height": 854,
                    "vcodec": "h264",
                    "acodec": "none",
                    "filesize": 15 * 1024 * 1024,
                },
                {
                    "format_id": "audio",
                    "vcodec": "none",
                    "acodec": "aac",
                    "filesize": 5 * 1024 * 1024,
                },
            ],
        }

        resolutions_portrait, audio_size = extract_smart_formats(info_portrait)
        labels_portrait = [r["label"] for r in resolutions_portrait]

        self.assertTrue(any("🎬 Full 1080p" in lbl for lbl in labels_portrait), f"Missing 1080p badge in: {labels_portrait}")
        self.assertTrue(any("⚡ Fast 720p" in lbl for lbl in labels_portrait), f"Missing 720p badge in: {labels_portrait}")
        self.assertTrue(any("💾 Data Saver 480p" in lbl for lbl in labels_portrait), f"Missing 480p badge in: {labels_portrait}")
        self.assertIn("~5MB", audio_size)

        # Landscape 1920x1080
        info_landscape = {
            "duration": 180,
            "formats": [
                {
                    "format_id": "1080p_l",
                    "width": 1920,
                    "height": 1080,
                    "vcodec": "h264",
                    "acodec": "none",
                    "filesize": 150 * 1024 * 1024,
                },
                {
                    "format_id": "720p_l",
                    "width": 1280,
                    "height": 720,
                    "vcodec": "h264",
                    "acodec": "none",
                    "filesize": 30 * 1024 * 1024,
                },
                {
                    "format_id": "480p_l",
                    "width": 854,
                    "height": 480,
                    "vcodec": "h264",
                    "acodec": "none",
                    "filesize": 15 * 1024 * 1024,
                },
            ],
        }

        resolutions_landscape, _ = extract_smart_formats(info_landscape)
        labels_landscape = [r["label"] for r in resolutions_landscape]

        self.assertTrue(any("🎬 Full 1080p" in lbl for lbl in labels_landscape), f"Missing 1080p badge in: {labels_landscape}")
        self.assertTrue(any("⚡ Fast 720p" in lbl for lbl in labels_landscape), f"Missing 720p badge in: {labels_landscape}")
        self.assertTrue(any("💾 Data Saver 480p" in lbl for lbl in labels_landscape), f"Missing 480p badge in: {labels_landscape}")


if __name__ == "__main__":
    unittest.main()
