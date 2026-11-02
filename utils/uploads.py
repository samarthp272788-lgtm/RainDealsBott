from io import BytesIO
from pathlib import PurePosixPath


async def text_from_message(message, max_bytes: int = 2_000_000) -> str | None:
    """Read plain text sent directly or uploaded as a small .txt/.csv file."""
    if message.text:
        return message.text
    document = message.document
    if not document:
        return None
    filename = PurePosixPath(document.file_name or "").name.lower()
    if PurePosixPath(filename).suffix not in {".txt", ".csv", ".tsv"}:
        return None
    if document.file_size and document.file_size > max_bytes:
        raise ValueError("File is too large; send a text file up to 2 MB.")
    remote = await message.bot.get_file(document.file_id)
    buffer = BytesIO()
    await message.bot.download_file(remote.file_path, destination=buffer)
    try:
        return buffer.getvalue().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("The uploaded list must be UTF-8 text.") from exc
