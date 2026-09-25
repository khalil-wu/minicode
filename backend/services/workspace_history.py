"""Keep workspace conversation snapshots when removing its navigation entry."""

import json
from pathlib import Path

from backend.atomic_io import atomic_write_text, canonical_file_path_key
from backend.conversations.repository import ConversationRepository
from backend.services.workspace_service import conversation_workspace_path


def workspace_conversation_ids(repository: ConversationRepository, path: str) -> list[str]:
    identity = canonical_file_path_key(Path(path).resolve())
    return [item.id for item in repository.list_conversations()
            if conversation_workspace_path(item)
            and canonical_file_path_key(conversation_workspace_path(item)) == identity]


def preserve_workspace_history(repository: ConversationRepository, path: str, ids: list[str]) -> str:
    root = Path(path).resolve()
    directory = root / ".minicode" / "conversations"
    if not ids:
        return str(directory)
    if not root.is_dir():
        raise FileNotFoundError("工作区目录不可用，无法保存历史会话；工作区尚未移除。")
    directory.mkdir(parents=True, exist_ok=True)
    for conversation_id in ids:
        record = repository.get_conversation(conversation_id)
        if record is not None:
            # The repository can read this native legacy record format directly.
            # Keep transcript, context snapshot and metadata together; the live
            # repository and its attachments remain in place.
            atomic_write_text(directory / f"{record.id}.json", json.dumps(
                record.to_dict(), ensure_ascii=False, indent=2,
            ) + "\n")
    return str(directory)
