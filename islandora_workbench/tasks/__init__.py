
from . import create

# Task registry to hold all the tasks registered by decorators
task_registry = {}

def register_task(*names):
    """Decorator to register a task class with one or more names."""
    def wrap(cls):
        for name in names:
            task_registry[name] = cls
        return cls
    return wrap
