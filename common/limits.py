"""单进程模拟护栏。以UTF-8字节数作保守token上界，不冒充模型实测token。"""
from dataclasses import dataclass, field
from decimal import Decimal
from common.config import get_settings


class BudgetExceeded(ValueError):
    pass


@dataclass
class TaskBudget:
    task_id: str
    tokens: int = 0
    iterations: int = 0
    cost_cny: Decimal = Decimal("0")
    token_limit: int | None = None
    iteration_limit: int | None = None
    cost_limit: Decimal | None = None
    _s: object = field(default_factory=get_settings, repr=False)

    def __post_init__(self):
        self.token_limit = min(self.token_limit or self._s.max_tokens_per_task, self._s.max_tokens_per_task)
        self.iteration_limit = min(self.iteration_limit or self._s.max_iterations_per_task, self._s.max_iterations_per_task)
        self.cost_limit = min(self.cost_limit if self.cost_limit is not None else self._s.max_cost_cny_per_task,
                              self._s.max_cost_cny_per_task)

    def add(self, *, tokens: int = 0, cost_cny: Decimal = Decimal("0")) -> None:
        cost = Decimal(str(cost_cny))
        if tokens < 0 or not cost.is_finite() or cost < 0:
            raise ValueError("计量必须为非负有限值")
        if self.tokens + tokens > self.token_limit or self.cost_cny + cost > self.cost_limit:
            raise BudgetExceeded("任务额度超限")
        self.tokens += tokens
        self.cost_cny += cost

    def consume_text(self, text: str) -> None:
        self.add(tokens=len(text.encode("utf-8")))

    def next_iteration(self) -> None:
        if self.iterations >= self.iteration_limit:
            raise BudgetExceeded("任务迭代上限")
        self.iterations += 1

    def over_tokens(self):
        return self.tokens > self.token_limit

    def over_iterations(self):
        return self.iterations > self.iteration_limit

    def over_cost(self):
        return self.cost_cny > self.cost_limit

    def breached(self):
        return self.over_tokens() or self.over_iterations() or self.over_cost()
