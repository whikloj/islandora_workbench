
from . import create, create_from_files, update, delete
from .base import Task

# Task registry to hold all the tasks registered by decorators
task_registry = {}

def register_task(*names):
    """Decorator to register a task class with one or more names."""
    def wrap(cls):
        for name in names:
            task_registry[name] = cls
        return cls
    return wrap

def get_task(ctx: dict) -> Task:
    """Get the correct task class from the registry
    :param ctx: The context to use
    :return The task class
    """
    task_name = ctx["config"]["task"]
    try:
        task_class = task_registry[task_name]
    except KeyError as e:
        tasks = sorted(task_registry)
        ctx["log"].error(f'"task" in your configuration file must be one of {", ".join(tasks)}. Got {task_name}.')
        raise e
    return task_class(ctx)
