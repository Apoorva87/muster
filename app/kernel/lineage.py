"""Walking a task back to the work a human actually asked for.

Most tasks in a running project are reactions to internal events, and their
objective reads "React to critique.complete". That is accurate and useless:
anything reasoning about *what the work is about* — a memory subject, a recall
query, a summary for a human — needs the objective at the root of the lineage,
which is the sentence someone actually wrote.

One definition, because two call sites getting this subtly different is how a
memory ends up filed under the wrong subject.
"""

from __future__ import annotations

from app.kernel.models import Task

#: Objectives the kernel generates for event fan-out. Never the real subject.
GENERATED_PREFIXES = ("react to ", "scheduled wakeup for ")

#: A cycle here would mean corrupt data; the cap makes it a bounded bug.
MAX_DEPTH = 32


def is_generated(objective: str | None) -> bool:
    text = (objective or "").strip().lower()
    return not text or text.startswith(GENERATED_PREFIXES)


def root_task(repository, task: Task | None) -> Task | None:
    """Walk ``parent_task_id`` to the origin of this lineage."""
    current = task
    seen: set[str] = set()
    for _ in range(MAX_DEPTH):
        if current is None or not current.parent_task_id or current.id in seen:
            break
        seen.add(current.id)
        parent = repository.get_task(current.parent_task_id)
        if parent is None:
            break
        current = parent
    return current


def meaningful_objective(repository, task: Task | None,
                         fallback: str = "") -> str:
    """The objective a human wrote, walking up from ``task`` if needed.

    Prefers the root; falls back to the task's own objective, then to
    ``fallback``. Never returns a generated "React to …" string when a real
    objective exists anywhere in the lineage.
    """
    for candidate in (root_task(repository, task), task):
        if candidate is not None and not is_generated(candidate.objective):
            return candidate.objective.strip()
    if task is not None and task.objective:
        return task.objective.strip()
    return fallback
