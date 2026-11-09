"""The four agents and their roles, declared once (2026-10-15).

Until today TaskStore inferred an agent's role from its folder name ("librarian" gets two workers and the
research_requests table, "private" cannot delegate), and the agent list was typed out in eight files. A fifth agent or a
rename would have to find every copy. Now the roster is the one table; in-process code passes a Role explicitly, and the
process entry points (worker, dashboard, supervisor) look a name up here and fail loudly on an unknown one.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Role:
    name: str
    delegates: bool = True        # owns Cal/Vit requests: parking, delivery gate, scorecard
    owner_budget: bool = True     # own project allowance; Cal and Vit run under the caller's cap
    request_table: str = ""       # the caller-side table that maps a delegate request to a local task
    workers: int = 1              # parallel background jobs
    entry: str = ""               # the role module under tabularium.agents that builds this agent
    learns: bool = True           # Alex's corrections start a diagnosis and lessons go through the lesson tool

    @property
    def env_var(self) -> str:
        return f"{self.name.upper()}_BACKGROUND_TASK"


ROLES = {
    "coordinator": Role("coordinator", entry="coordinator"),
    "private": Role("private", entry="private", delegates=False, learns=False),   # private; in private conversation "you're wrong" is the conversation
    # 2026-10-08: Cato's two Cal briefs ran one after the other, so Cal takes two; Cato's and Vit's tasks share
    # workbooks and code paths and stay at one.
    "librarian": Role("librarian", entry="librarian", delegates=False, owner_budget=False, request_table="research_requests", workers=2),
    "engineer": Role("engineer", entry="engineer", delegates=False, owner_budget=False, request_table="engineering_requests"),
}
AGENTS = tuple(ROLES)
