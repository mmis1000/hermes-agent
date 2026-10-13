"""Bundled task-context registration through the generic plugin interfaces."""


def register(ctx):
    from .task_context.engine import TaskContextEngine
    from .task_context.migration import contract_provider

    ctx.register_context_engine(TaskContextEngine())
    ctx.register_hook("read_task_contract", contract_provider)
