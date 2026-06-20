"""Канонические ключи Recipe Vault (один video_id → одна запись)."""

from __future__ import annotations


def canonical_cache_key(url: str, source_type: str) -> str:
    """Стабильный ключ кэша: ``yt:video_id``, ``ig:shortcode``, не сырой URL."""
    st = (source_type or "").strip().lower()
    if st == "instagram":
        from ..parser import InstagramParser

        shortcode = InstagramParser._extract_shortcode(url)
        if shortcode:
            return f"ig:{shortcode}"
    if st in ("youtube", "tiktok", "vk"):
        from ..parser import TikTokParser, VkVideoParser, YouTubeParser

        if st == "youtube":
            vid = YouTubeParser._extract_youtube_video_id(url)
            if vid:
                return f"yt:{vid}"
        elif st == "vk":
            vid = VkVideoParser._extract_video_id(url)
            if vid:
                return f"vk:{vid}"
        else:
            normalized = url.strip().lower().rstrip("/")
            return f"tt:{normalized}"
    return f"web:{url.strip().lower().rstrip('/')}"


if __name__ == "__main__":
    assert canonical_cache_key(
        "https://www.youtube.com/watch?v=abc123XYZ-_",
        "youtube",
    ) == "yt:abc123XYZ-_"
    assert canonical_cache_key(
        "https://youtu.be/abc123XYZ-_",
        "youtube",
    ) == "yt:abc123XYZ-_"
    print("✅ recipe_vault.keys")
