
from islandora_workbench.tasks import task_registry
from islandora_workbench.tasks.base import Task

def get_tasks(ctx) -> Task:
    """Get the correct task class from the registry
    :param ctx: The context to use
    :return The task class
    """
    task_name = ctx["task"]
    try:
        task_class = task_registry[task_name]
    except KeyError as e:
        tasks = sorted(task_registry)
        ctx.log.error(f'"task" in your configuration file must be one of {", ".join(tasks)}. Got {task_name}.')
        raise e
    return task_class(ctx)





