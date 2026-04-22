from __future__ import annotations

import torch
from torch import nn


class FirstLimitSequenceModel(nn.Module):
    def __init__(self, input_size: int, hidden_size: int = 48, dropout: float = 0.1) -> None:
        super().__init__()
        self.encoder = nn.GRU(
            input_size=input_size,
            hidden_size=hidden_size,
            num_layers=1,
            batch_first=True,
        )
        self.dropout = nn.Dropout(dropout)
        self.head_cont = nn.Linear(hidden_size, 1)
        self.head_strong = nn.Linear(hidden_size, 1)
        self.head_risk = nn.Linear(hidden_size, 1)

    def forward(self, x: torch.Tensor) -> dict[str, torch.Tensor]:
        out, _ = self.encoder(x)
        last = self.dropout(out[:, -1, :])
        return {
            "continuation": self.head_cont(last).squeeze(-1),
            "strong_3d": self.head_strong(last).squeeze(-1),
            "break_risk": self.head_risk(last).squeeze(-1),
        }
