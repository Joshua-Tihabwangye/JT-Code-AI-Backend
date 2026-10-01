"""A PostgreSQL (Django ORM) LangGraph checkpointer.

Checkpoints let a run resume after a worker crash or a human-approval pause.
State is serialized with LangGraph's own serializer (its msgpack allowlist
restricts which types may be revived) and stored against the run id, so every
checkpoint inherits the run's tenant. Rows are deleted once a run is terminal.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Sequence
from typing import Any, TypeVar

from django.db import IntegrityError, connections, transaction
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)

from apps.agents.models import AgentCheckpoint, AgentCheckpointWrite

T = TypeVar("T")


def _configurable(config: RunnableConfig) -> dict[str, Any]:
    return dict(config.get("configurable") or {})


class DjangoCheckpointSaver(BaseCheckpointSaver[str]):
    """Synchronous checkpointer backed by ``AgentCheckpoint``/``AgentCheckpointWrite``.

    LangGraph submits checkpoint writes to a background thread pool. Django opens
    one database connection per thread, so any work done off the owning thread
    closes that thread's connection afterwards; otherwise every pool thread would
    leak a PostgreSQL connection (exhausting Supabase's connection slots).
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._owner_thread = threading.get_ident()

    def _db(self, operation: Callable[[], T]) -> T:
        try:
            return operation()
        finally:
            if threading.get_ident() != self._owner_thread:
                connections.close_all()

    def _tuple(self, row: AgentCheckpoint) -> CheckpointTuple:
        checkpoint: Checkpoint = self.serde.loads_typed((row.type, bytes(row.checkpoint)))
        metadata: CheckpointMetadata = self.serde.loads_typed((row.metadata_type, bytes(row.metadata)))
        writes = AgentCheckpointWrite.objects.filter(
            thread_id=row.thread_id, checkpoint_ns=row.checkpoint_ns, checkpoint_id=row.checkpoint_id
        ).order_by("task_id", "idx")
        config: RunnableConfig = {
            "configurable": {
                "thread_id": row.thread_id,
                "checkpoint_ns": row.checkpoint_ns,
                "checkpoint_id": row.checkpoint_id,
            }
        }
        parent: RunnableConfig | None = None
        if row.parent_checkpoint_id:
            parent = {
                "configurable": {
                    "thread_id": row.thread_id,
                    "checkpoint_ns": row.checkpoint_ns,
                    "checkpoint_id": row.parent_checkpoint_id,
                }
            }
        return CheckpointTuple(
            config=config,
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=parent,
            pending_writes=[
                (write.task_id, write.channel, self.serde.loads_typed((write.type, bytes(write.value))))
                for write in writes
            ],
        )

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return self._db(lambda: self._get_tuple(config))

    def _get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        configurable = _configurable(config)
        rows = AgentCheckpoint.objects.filter(
            thread_id=str(configurable["thread_id"]),
            checkpoint_ns=configurable.get("checkpoint_ns", ""),
        )
        if checkpoint_id := get_checkpoint_id(config):
            rows = rows.filter(checkpoint_id=checkpoint_id)
        row = rows.order_by("-checkpoint_id").first()
        return self._tuple(row) if row is not None else None

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,  # noqa: A002 - LangGraph API name
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        return iter(self._db(lambda: list(self._list(config, filter=filter, before=before, limit=limit))))

    def _list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,  # noqa: A002 - LangGraph API name
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        rows = AgentCheckpoint.objects.all()
        if config is not None:
            configurable = _configurable(config)
            rows = rows.filter(thread_id=str(configurable["thread_id"]))
            if "checkpoint_ns" in configurable:
                rows = rows.filter(checkpoint_ns=configurable["checkpoint_ns"])
            if checkpoint_id := get_checkpoint_id(config):
                rows = rows.filter(checkpoint_id=checkpoint_id)
        if before is not None and (before_id := get_checkpoint_id(before)):
            rows = rows.filter(checkpoint_id__lt=before_id)
        yielded = 0
        for row in rows.order_by("-checkpoint_id"):
            item = self._tuple(row)
            if filter and not all(item.metadata.get(key) == value for key, value in filter.items()):
                continue
            yield item
            yielded += 1
            if limit is not None and yielded >= limit:
                return

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return self._db(lambda: self._put(config, checkpoint, metadata))

    def _put(
        self, config: RunnableConfig, checkpoint: Checkpoint, metadata: CheckpointMetadata
    ) -> RunnableConfig:
        configurable = _configurable(config)
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = configurable.get("checkpoint_ns", "")
        checkpoint_type, checkpoint_blob = self.serde.dumps_typed(checkpoint)
        metadata_type, metadata_blob = self.serde.dumps_typed(get_checkpoint_metadata(config, metadata))
        AgentCheckpoint.objects.update_or_create(
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            checkpoint_id=checkpoint["id"],
            defaults={
                "parent_checkpoint_id": configurable.get("checkpoint_id") or "",
                "type": checkpoint_type,
                "checkpoint": checkpoint_blob,
                "metadata_type": metadata_type,
                "metadata": metadata_blob,
            },
        )
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint["id"],
            }
        }

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self._db(lambda: self._put_writes(config, writes, task_id, task_path))

    def _put_writes(
        self, config: RunnableConfig, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str
    ) -> None:
        configurable = _configurable(config)
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = configurable.get("checkpoint_ns", "")
        checkpoint_id = configurable["checkpoint_id"]
        for index, (channel, value) in enumerate(writes):
            idx = WRITES_IDX_MAP.get(channel, index)
            value_type, value_blob = self.serde.dumps_typed(value)
            key = {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
                "task_id": task_id,
                "idx": idx,
            }
            payload = {"channel": channel, "type": value_type, "value": value_blob, "task_path": task_path}
            if idx >= 0:
                # Regular writes are first-writer-wins (idempotent replays).
                try:
                    with transaction.atomic():
                        AgentCheckpointWrite.objects.create(**key, **payload)
                except IntegrityError:
                    continue
            else:
                # Special writes (errors, interrupts) replace any previous value.
                AgentCheckpointWrite.objects.update_or_create(**key, defaults=payload)

    def delete_thread(self, thread_id: str) -> None:
        def delete() -> None:
            AgentCheckpointWrite.objects.filter(thread_id=str(thread_id)).delete()
            AgentCheckpoint.objects.filter(thread_id=str(thread_id)).delete()

        self._db(delete)
