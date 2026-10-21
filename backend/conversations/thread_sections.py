"""User sidebar sections and their ordered, current resource membership."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from backend.atomic_io import atomic_write_text, canonical_file_path_key, file_mutation_locks
from backend.conversations.sidebar_order import replace_visible_slots
from backend.ids import new_uuid7


class ThreadSectionStore:
    def __init__(self, repository):
        self.repository = repository
        self.path = repository.storage_root / ".thread-sections.json"

    def _read(self):
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else []

    def _write(self, sections):
        atomic_write_text(self.path, json.dumps(sections, ensure_ascii=False), encoding="utf-8")

    def list(self):
        with file_mutation_locks([self.path]):
            return self._read()

    def create(self, name, *, appearance=None, after_section_id=None):
        name = name.strip()
        if not name:
            raise ValueError("Section name must not be empty")
        section = {"id": new_uuid7(), "name": name, "appearance": appearance, "item_keys": []}
        with file_mutation_locks([self.path]):
            sections = self._read()
            position = next((index + 1 for index, item in enumerate(sections) if item["id"] == after_section_id), len(sections))
            sections.insert(position, section)
            self._write(sections)
        return section

    def update(self, section_id, name, **updates):
        name = name.strip()
        if not name:
            raise ValueError("Section name must not be empty")
        with file_mutation_locks([self.path]):
            sections = self._read()
            section = next((item for item in sections if item["id"] == section_id), None)
            if section is None:
                raise ValueError("Section not found")
            section.update(name=name, **updates)
            self._write(sections)
            return deepcopy(section)

    def delete(self, section_id):
        with file_mutation_locks([self.path]):
            sections = self._read()
            if not any(item["id"] == section_id for item in sections):
                raise ValueError("Section not found")
            self._write([item for item in sections if item["id"] != section_id])

    def reorder(self, moved_id, before_id, visible_ids):
        with file_mutation_locks([self.path]):
            sections = self._read()
            order = replace_visible_slots([section["id"] for section in sections], visible_ids, moved_id, before_id)
            by_id = {section["id"]: section for section in sections}
            self._write([by_id[identifier] for identifier in order])

    def move(self, item_key, section_id, *, before_item_key=None):
        with file_mutation_locks([self.path]):
            sections = self._read()
            destination = next((section for section in sections if section["id"] == section_id), None)
            if section_id is not None and destination is None:
                raise ValueError("Section not found")
            if before_item_key == item_key:
                return
            if before_item_key is not None and (destination is None or before_item_key not in destination["item_keys"]):
                raise ValueError("The destination item changed before this move")
            for section in sections:
                section["item_keys"] = [key for key in section["item_keys"] if key != item_key]
            if destination is not None:
                position = 0 if before_item_key is None else destination["item_keys"].index(before_item_key)
                destination["item_keys"].insert(position, item_key)
            self._write(sections)

    def move_thread(self, conversation_id, section_id, *, before_item_key=None):
        repository = self.repository
        with repository._store_lock(conversation_id):
            if repository.get_conversation_summary(conversation_id) is None:
                raise ValueError("Conversation not found")
            if section_id == "pinned":
                repository.set_conversation_pinned(conversation_id, True)
                if before_item_key is not None:
                    pinned = repository.get_pinned_order(repository.list_conversations())["pinned_conversation_ids"]
                    repository.reorder_pinned_conversations(conversation_id, before_item_key.removeprefix("thread:"), pinned)
                return
            self.move("thread:" + conversation_id, section_id, before_item_key=before_item_key)
            repository.set_conversation_pinned(conversation_id, False)

    def move_project(self, path, section_id, *, before_item_key=None):
        self.move("project:" + canonical_file_path_key(path), section_id, before_item_key=before_item_key)

    def reorder_items(self, section_id, moved_key, before_key, visible_keys):
        with file_mutation_locks([self.path]):
            sections = self._read()
            section = next((item for item in sections if item["id"] == section_id), None)
            if section is None:
                raise ValueError("Section not found")
            section["item_keys"] = replace_visible_slots(section["item_keys"], visible_keys, moved_key, before_key)
            self._write(sections)

    def copy_thread_placement(self, source_id, destination_id):
        source_key = "thread:" + source_id
        with file_mutation_locks([self.path]):
            sections = self._read()
            section = next((item for item in sections if source_key in item["item_keys"]), None)
            if section is not None:
                section["item_keys"].insert(section["item_keys"].index(source_key) + 1, "thread:" + destination_id)
                self._write(sections)

    def remove_thread(self, conversation_id):
        key = "thread:" + conversation_id
        with file_mutation_locks([self.path]):
            sections = self._read()
            if not any(key in section["item_keys"] for section in sections):
                return
            for section in sections:
                section["item_keys"] = [item for item in section["item_keys"] if item != key]
            self._write(sections)
