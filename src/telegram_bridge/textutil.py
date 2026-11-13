def split_message(text, limit=4096):
    """Split text into chunks <= limit, preferring newline/space boundaries."""
    text = text or ""
    if not text:
        return [""]
    out = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        out.append(text[:cut])
        text = text[cut:].lstrip("\n ")
    if text:
        out.append(text)
    return out
