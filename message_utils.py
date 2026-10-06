def split_message(text, limit=4000):
    """Preserve all characters, counting Telegram's UTF-16 code units."""
    parts = []
    while text:
        units = 0
        end = 0
        for char in text:
            width = 2 if ord(char) > 0xFFFF else 1
            if units + width > limit:
                break
            units += width
            end += 1
        if end == len(text):
            parts.append(text)
            break
        if not end:
            raise ValueError("Message limit is too small")
        boundary = text.rfind('\n', 0, end)
        if boundary >= end // 2:
            end = boundary + 1
        parts.append(text[:end])
        text = text[end:]
    return parts
