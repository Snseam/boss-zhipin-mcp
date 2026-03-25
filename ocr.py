"""OCR via Claude Vision API — extract text from resume screenshots."""

import base64
from anthropic import Anthropic
from config import ANTHROPIC_API_KEY

_client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


def ocr_image(image_bytes: bytes) -> str:
    """Extract text from an image using Claude Vision.

    Args:
        image_bytes: PNG image bytes

    Returns:
        Extracted text content
    """
    if not _client:
        return "[OCR 不可用：未设置 ANTHROPIC_API_KEY]"

    b64 = base64.b64encode(image_bytes).decode("utf-8")

    response = _client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": b64,
                    },
                },
                {
                    "type": "text",
                    "text": "请提取这张简历截图中的所有文字内容，保持原始结构和层级。直接输出文字，不要添加任何解释。",
                },
            ],
        }],
    )

    return response.content[0].text
