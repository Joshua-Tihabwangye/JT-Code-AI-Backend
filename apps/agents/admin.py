from django.contrib import admin

from apps.agents.models import AgentDefinition, AgentEvaluation, AgentRun, AgentStep


@admin.register(AgentDefinition)
class AgentDefinitionAdmin(admin.ModelAdmin):
    list_display = ("name", "organization", "graph", "model_alias", "is_active", "updated_at")
    list_filter = ("graph", "is_active")
    search_fields = ("name", "slug", "organization__name")
    readonly_fields = ("id", "created_at", "updated_at")


class AgentStepInline(admin.TabularInline):
    model = AgentStep
    extra = 0
    can_delete = False
    fields = ("sequence", "node", "kind", "outcome", "summary", "latency_ms")
    readonly_fields = fields


@admin.register(AgentRun)
class AgentRunAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "organization",
        "user",
        "graph",
        "status",
        "model_calls",
        "tool_calls",
        "created_at",
    )
    list_filter = ("status", "graph", "intent_source")
    search_fields = ("id", "trace_id", "user__email")
    readonly_fields = [field.name for field in AgentRun._meta.fields]
    inlines = (AgentStepInline,)


@admin.register(AgentEvaluation)
class AgentEvaluationAdmin(admin.ModelAdmin):
    list_display = ("run", "evaluator", "passed", "score", "created_at")
    list_filter = ("evaluator", "passed")
    readonly_fields = ("id", "run", "evaluator", "passed", "score", "metrics", "created_at")
