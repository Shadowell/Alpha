from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from .modeling import FirstLimitBaselineTrainer
from .schema import TrainingConfig


def main() -> None:
    parser = argparse.ArgumentParser(description="Train FirstLimit Alpha baseline model")
    parser.add_argument("--features", required=True, help="Path to features.csv")
    parser.add_argument("--output-dir", required=True, help="Artifact output dir")
    args = parser.parse_args()

    frame = pd.read_csv(args.features)
    trainer = FirstLimitBaselineTrainer()
    result = trainer.train(frame, Path(args.output_dir), config=TrainingConfig())
    print(result)


if __name__ == "__main__":
    main()
