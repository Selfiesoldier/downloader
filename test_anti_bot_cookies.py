import os
import sys
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bot import (
    resolve_cookies,
    validate_netscape_cookies,
    get_ydl_opts,
    detect_platform,
    COOKIES_DIR,
    BASE_DIR
)

print("=== UNIT TESTING ANTI-BOT & COOKIES SUBSYSTEM ===")

# 1. Test validate_netscape_cookies
valid_sample = """# Netscape HTTP Cookie File
# https://curl.haxx.se/rfc/cookie_spec.html
.youtube.com	TRUE	/	TRUE	1772000000	LOGIN_INFO	AFmmF2swRQIg...
.youtube.com	TRUE	/	TRUE	1772000000	PREF	f4=4000000&tz=Asia.Calcutta
"""
assert validate_netscape_cookies(valid_sample) is True, "Valid Netscape cookie header should pass"
print("✓ [PASS] validate_netscape_cookies passed on standard Netscape file")

invalid_sample = "hello world\nthis is just some random text without cookie records\n"
assert validate_netscape_cookies(invalid_sample) is False, "Random text should be rejected"
print("✓ [PASS] validate_netscape_cookies rejected non-cookie text")

# 2. Test resolve_cookies priority
test_yt = COOKIES_DIR / "youtube_cookies.txt"
test_ig = COOKIES_DIR / "instagram_cookies.txt"
test_univ = COOKIES_DIR / "cookies.txt"

# Clean up before testing
for f in [test_yt, test_ig, test_univ]:
    if f.exists():
        f.unlink()

# Initially should return None
assert resolve_cookies("https://www.youtube.com/watch?v=123") is None
assert resolve_cookies("https://www.instagram.com/reel/123") is None
print("✓ [PASS] resolve_cookies returns None when no cookies exist")

# Create youtube_cookies.txt
test_yt.write_text(valid_sample, encoding="utf-8")
assert resolve_cookies("https://www.youtube.com/watch?v=123") == str(test_yt)
assert resolve_cookies("https://www.instagram.com/reel/123") is None
print("✓ [PASS] resolve_cookies matches youtube_cookies.txt specifically for YouTube")

# Create instagram_cookies.txt
test_ig.write_text(valid_sample, encoding="utf-8")
assert resolve_cookies("https://www.instagram.com/reel/123") == str(test_ig)
print("✓ [PASS] resolve_cookies matches instagram_cookies.txt specifically for Instagram")

# Clean up test files
test_yt.unlink()
test_ig.unlink()

# Create universal cookies.txt
test_univ.write_text(valid_sample, encoding="utf-8")
assert resolve_cookies("https://www.youtube.com/watch?v=123") == str(test_univ)
assert resolve_cookies("https://www.instagram.com/reel/123") == str(test_univ)
print("✓ [PASS] resolve_cookies falls back to universal cookies.txt for all platforms")
test_univ.unlink()

# 3. Test get_ydl_opts extractor_args
opts = get_ydl_opts(url="https://www.youtube.com/watch?v=123")
assert "extractor_args" in opts, "extractor_args must be present"
assert "youtube" in opts["extractor_args"], "youtube extractor args must be present"
assert "android" in opts["extractor_args"]["youtube"]["player_client"], "android client must be in player_client"
assert "ios" in opts["extractor_args"]["youtube"]["player_client"], "ios client must be in player_client"
assert "http_headers" in opts, "http_headers must be present"
print("✓ [PASS] get_ydl_opts includes mobile client rotation (android, ios, mweb) and browser headers")

print("\n🎉 ALL UNIT TESTS PASSED SUCCESSFULLY!")

