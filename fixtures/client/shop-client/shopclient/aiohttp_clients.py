"""aiohttp ClientSession의 base_url(RFC 3986)."""

import aiohttp


async def recommendations():
    """상대 경로는 base 경로 뒤."""
    async with aiohttp.ClientSession(base_url="http://recs.example.test/api/") as session:
        async with session.get("recommendations") as response:
            return response.status


async def popular():
    """/로 시작하는 경로는 base 경로를 바꾼다(RFC 3986)."""
    async with aiohttp.ClientSession(base_url="http://recs.example.test/api/") as session:
        async with session.get("/popular") as response:
            return response.status


async def send_feedback(item_id):
    """base 없는 세션의 전체 URL과 request(동사, url)."""
    async with aiohttp.ClientSession() as session:
        async with session.request("post", f"http://recs.example.test/api/items/{item_id}/feedback") as response:
            return response.status
