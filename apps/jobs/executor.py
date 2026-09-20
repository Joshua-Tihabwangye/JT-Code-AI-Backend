"""Internal Celery executor for AI task types.

The executor superseded the n8n-only dispatch path for ``GENERAL_QUESTION``
and ``RAG_QUERY`` jobs. Non-AI task types are left for external workflow
orchestration (status stays ``QUEUED``).
"""

from __future__ import annotations

from decimal import Decimal

import sentry_sdk
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.ai_gateway.adapters import AIGatewayError, ChatMessage
from apps.ai_gateway.service import generate_completion
from apps.events.outbox import enqueue_outbox_event
from apps.jobs.models import Job, JobStep

_RAG_SYSTEM_PROMPT = (
    "You are JT-Code's grounded research assistant. Answer using ONLY the "
    'provided Knowledge Base context. If the context does not contain the '
    'answer, say so explicitly and do not invent facts. When you use context, '
    'cite the source document title for each claim.'
)


def _user_messages(payload: dict) -> list[ChatMessage]:
    messages = payload.get('messages') or []
    result: list[ChatMessage] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = (msg.get('role') or 'user').strip().lower()
        if role not in {'system', 'user', 'assistant'}:
            role = 'user'
        result.append(ChatMessage(role=role, content=str(msg.get('content') or '')))
    return result


def _last_user_text(payload: dict) -> str:
    for msg in reversed(payload.get('messages') or []):
        if isinstance(msg, dict) and (msg.get('role') or 'user') == 'user':
            return str(msg.get('content') or '')
    return ''


def _run_completion(job: Job, messages: list[ChatMessage]) -> dict:
    payload = job.input_payload
    outcome = generate_completion(
        messages=messages,
        task_type=job.task_type,
        model_id=payload.get('model_id'),
        policy_slug=payload.get('policy_slug'),
        temperature=payload.get('temperature', 0.7),
        max_tokens=payload.get('max_tokens'),
        tools=payload.get('tools'),
        request_id=str(job.request_id),
        trace_id=job.trace_id or '',
        job_id=str(job.id),
    )
    usage = {
        'input_tokens': outcome.usage.input_tokens,
        'output_tokens': outcome.usage.output_tokens,
        'cached_tokens': outcome.usage.cached_tokens,
        'cost_usd': str(outcome.run.provider_cost_usd),
        'model': outcome.model.name,
        'provider': outcome.provider.type,
    }
    return {'answer': outcome.content, 'usage': usage}


def _general_question(job: Job) -> dict:
    payload = job.input_payload
    messages = _user_messages(payload)
    if not messages:
        raise AIGatewayError('messages required in job payload', code='INVALID_INPUT')
    return _run_completion(job, messages)


def _search_research(job: Job) -> dict:
    payload = job.input_payload
    query = payload.get('query') or _last_user_text(payload) or ''
    if not query:
        raise AIGatewayError('No query provided in job payload', code='INVALID_INPUT')

    from langchain_core.messages import HumanMessage

    from apps.agents.runtime import run_agent
    from apps.agents.tools import default_agent_tools

    requested = tuple(payload.get('tools') or default_agent_tools())
    allowed = set(default_agent_tools())
    tools = tuple(t for t in requested if t in allowed) or default_agent_tools()

    run = run_agent(
        user=job.owner,
        organization_id=job.organization_id,
        initial_messages=[HumanMessage(content=query)],
        task_type=job.task_type,
        temperature=payload.get('temperature', 0.7),
        max_tokens=payload.get('max_tokens'),
        tools=tools,
        max_model_calls=settings.AGENT_MAX_ITERATIONS,
        request_id=str(job.request_id),
        trace_id=job.trace_id or '',
        job_id=str(job.id),
    )
    return {
        'answer': run.final_answer,
        'tools': run.invoked_tools,
        'messages': _serialize_agent_messages(run.messages),
        'model_runs': run.model_runs,
        'grounded': 'knowledge.search' in run.invoked_tools,
        'usage': {
            'input_tokens': run.input_tokens,
            'output_tokens': run.output_tokens,
            'model_calls': len(run.model_runs),
        },
    }


def _serialize_agent_messages(messages) -> list[dict]:
    from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

    out = []
    for message in messages:
        if not isinstance(message, BaseMessage):
            continue
        if isinstance(message, AIMessage):
            role, content = 'assistant', str(message.content or '')
        elif isinstance(message, ToolMessage):
            role, content = 'tool', str(message.content or '')
        else:
            role, content = 'user', str(message.content or '')
        out.append({'role': role, 'content': content})
    return out


def _rag_query(job: Job) -> dict:
    payload = job.input_payload
    query = payload.get('query') or _last_user_text(payload) or ''
    if not query:
        raise AIGatewayError('No query provided in job payload', code='INVALID_INPUT')
    sources = _retrieve_sources(query, job)
    grounded = bool(sources)
    context_block = (
        '\n'.join(
            f'[{i + 1}] {s.get("document_title", "Document")} '
            f'(chunk {s.get("chunk_index", "?")}): {s["content"]}'
            for i, s in enumerate(sources)
        )
        or 'No context available.'
    )
    messages = [
        ChatMessage('system', _RAG_SYSTEM_PROMPT),
        ChatMessage('user', f'Question: {query}\n\nKnowledge base context:\n{context_block}'),
    ]
    result = _run_completion(job, messages)
    result['sources'] = sources
    result['grounded'] = grounded
    return result


def _retrieve_sources(query: str, job: Job) -> list[dict]:
    org_id = job.organization_id
    if not org_id:
        return []
    try:
        from apps.knowledge.embeddings import embed_texts
        from apps.knowledge.vectorstore import vector_store_enabled

        if not vector_store_enabled():
            return []
        vectors = embed_texts([query])
        return _search_knowledge(
            query_vector=vectors[0],
            organization_id=org_id,
            top_k=int(job.input_payload.get('top_k', 5)),
        )
    except Exception as exc:  # noqa: BLE001 - retrieval must not fail the whole job
        sentry_sdk.capture_exception(exc)
        return []


def _search_knowledge(*, query_vector: list[float], organization_id, top_k: int = 5) -> list[dict]:
    from apps.knowledge.models import Collection
    from apps.knowledge.vectorstore import VectorStoreUnavailable, semantic_search

    collection_ids = list(
        Collection.objects.filter(organization_id=organization_id).values_list('id', flat=True)
    )
    if not collection_ids:
        return []
    try:
        return semantic_search(
            query_vector=query_vector,
            collection_ids=collection_ids,
            organization_id=organization_id,
            top_k=top_k,
        )
    except VectorStoreUnavailable:
        return []


HANDLERS = {
    Job.TaskType.GENERAL_QUESTION: _general_question,
    Job.TaskType.RAG_QUERY: _rag_query,
    Job.TaskType.SEARCH_RESEARCH: _search_research,
}


def _mark_started(job: Job) -> JobStep:
    job.status = Job.Status.RUNNING
    job.started_at = timezone.now()
    job.save(update_fields=['status', 'started_at', 'updated_at'])
    return JobStep.objects.create(
        job=job,
        name=job.task_type,
        step_order=0,
        status=JobStep.Status.RUNNING,
        started_at=timezone.now(),
    )


def _finalize_success(job: Job, step: JobStep, result: dict) -> None:
    usage = result.get('usage', {})
    with transaction.atomic():
        job.status = Job.Status.COMPLETED
        job.result = result
        job.completed_at = timezone.now()
        job.save(update_fields=['status', 'result', 'completed_at', 'updated_at'])
        step.status = JobStep.Status.COMPLETED
        step.output_payload = result
        step.provider = usage.get('provider', '')
        step.model = usage.get('model', '')
        step.input_tokens = usage.get('input_tokens', 0)
        step.output_tokens = usage.get('output_tokens', 0)
        step.actual_cost_usd = Decimal(str(usage.get('cost_usd', 0) or 0))
        step.completed_at = timezone.now()
        step.save(
            update_fields=[
                'status',
                'output_payload',
                'provider',
                'model',
                'input_tokens',
                'output_tokens',
                'actual_cost_usd',
                'completed_at',
                'updated_at',
            ]
        )
    enqueue_outbox_event(
        topic='jobs.job.completed',
        event_key=str(job.request_id),
        payload={
            'job_id': str(job.id),
            'request_id': str(job.request_id),
            'status': 'completed',
            'task_type': job.task_type,
        },
        headers={'trace_id': job.trace_id},
    )


def _finalize_failure(job: Job, step: JobStep, code: str, message: str) -> None:
    with transaction.atomic():
        job.status = Job.Status.FAILED
        job.error_code = code
        job.error_message = message
        job.completed_at = timezone.now()
        job.save(update_fields=['status', 'error_code', 'error_message', 'completed_at', 'updated_at'])
        step.status = JobStep.Status.FAILED
        step.error_message = message
        step.completed_at = timezone.now()
        step.save(update_fields=['status', 'error_message', 'completed_at', 'updated_at'])
    enqueue_outbox_event(
        topic='jobs.job.failed',
        event_key=str(job.request_id),
        payload={
            'job_id': str(job.id),
            'request_id': str(job.request_id),
            'status': 'failed',
            'error_code': code,
            'error_message': message,
        },
        headers={'trace_id': job.trace_id},
    )


def execute_job(job: Job) -> dict:
    """Run ``job`` with the internal executor and update all lifecycle state."""
    handler = HANDLERS.get(job.task_type)
    if handler is None:
        return {'skipped': True, 'task_type': job.task_type, 'reason': 'no_native_handler'}
    step = _mark_started(job)
    try:
        result = handler(job)
    except AIGatewayError as exc:
        _finalize_failure(job, step, code=exc.code, message=str(exc))
        return {'status': 'failed', 'task_type': job.task_type, 'error_code': exc.code}
    except Exception as exc:  # noqa: BLE001 - job isolation
        sentry_sdk.capture_exception(exc)
        _finalize_failure(job, step, code='JOB_EXECUTION_FAILED', message=str(exc))
        return {'status': 'failed', 'task_type': job.task_type, 'error_code': 'JOB_EXECUTION_FAILED'}
    _finalize_success(job, step, result)
    return {'status': 'completed', 'task_type': job.task_type}
