"""序列 / 多模态模型增量验证脚手架 (GitHub issue #36).

Public surface:

* :class:`~strategy.model_validation.contracts.Panel` - the input contract
  (labels from #23, gross returns + ``can_buy`` / ``can_sell`` from #26).
* :func:`~strategy.model_validation.splitting.build_walk_forward_folds` -
  walk-forward + embargo + purging.
* :func:`~strategy.model_validation.metrics.evaluate_scores` /
  :func:`~strategy.model_validation.portfolio.run_portfolio` - identical scoring
  code path for baseline and candidate (IC / rank IC / PR-AUC / Precision@K /
  分层收益 / 换手 / 成本敏感性), pure numpy + pandas.
* :func:`~strategy.model_validation.gate.evaluate_gate` - the hard "没有增量就不
  保留" decision.
* :func:`~strategy.model_validation.runner.run_validation` - orchestration +
  report / model-card output.

Heavy models (torch GRU / Transformer, K-line image CNN, Kronos) are only ever
reachable through an adapter that imports its dependency lazily; a missing
dependency marks that candidate as ``skipped`` instead of failing the run, so CI
(Python 3.11, CPU only, no new requirements) stays green.
"""

from .contracts import (
    ColumnNames,
    ContractError,
    IncrementValidator,
    ModelInput,
    Panel,
    SequenceBundle,
    validate_panel,
)
from .gate import (
    DEFAULT_RULES,
    GateCheck,
    GateConfig,
    GateRule,
    GateVerdict,
    evaluate_gate,
)
from .portfolio import PortfolioConfig, run_portfolio
from .runner import (
    ROLE_BASELINE,
    ROLE_CANDIDATE,
    ModelSpec,
    RunResult,
    ValidationConfig,
    default_models,
    run_validation,
)
from .splitting import (
    EmbargoViolation,
    FoldSpec,
    InsufficientDataError,
    WalkForwardConfig,
    build_walk_forward_folds,
    split_panel_rows,
)

__all__ = [
    "DEFAULT_RULES",
    "ROLE_BASELINE",
    "ROLE_CANDIDATE",
    "ColumnNames",
    "ContractError",
    "EmbargoViolation",
    "FoldSpec",
    "GateCheck",
    "GateConfig",
    "GateRule",
    "GateVerdict",
    "IncrementValidator",
    "InsufficientDataError",
    "ModelInput",
    "ModelSpec",
    "Panel",
    "PortfolioConfig",
    "ROLE_BASELINE",
    "ROLE_CANDIDATE",
    "RunResult",
    "SequenceBundle",
    "ValidationConfig",
    "WalkForwardConfig",
    "build_walk_forward_folds",
    "default_models",
    "evaluate_gate",
    "run_portfolio",
    "run_validation",
    "split_panel_rows",
    "validate_panel",
]
