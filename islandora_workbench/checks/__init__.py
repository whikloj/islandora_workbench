
from islandora_workbench.checks.common import common_checks_before_task, common_checks_after_task
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
    return task_class(ctx)

def run_checks(ctx: dict, task: Task):
    """Run checks
    :param ctx: The context to use
    :param task: The task to run
    """
    common_checks_before_task(ctx)
    task.check()
    common_checks_after_task(ctx)



