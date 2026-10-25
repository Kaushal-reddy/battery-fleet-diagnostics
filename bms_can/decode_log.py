"""Read the CAN log and decode it into one DataFrame per message."""

from pathlib import Path

import can
import cantools
import pandas as pd

from src import config


def read_frames(path: Path) -> list[can.Message]:
    """Read every frame from a candump-format log."""
    return list(can.LogReader(path))


def decode(frames: list[can.Message], db: cantools.database.Database) -> dict[str, pd.DataFrame]:
    """Decode frames into one time-sorted DataFrame per message, counting unknown IDs."""
    rows = {m.name: [] for m in db.messages}
    unknown = 0
    for frame in frames:
        try:
            msg = db.get_message_by_frame_id(frame.arbitration_id)
        except KeyError:
            unknown += 1
            continue
        rows[msg.name].append({"t": frame.timestamp, **msg.decode(frame.data)})
    print(f"  unknown IDs: {unknown}")
    # Columns are listed so a message with no frames still gives an empty table with the right shape.
    return {m.name: pd.DataFrame(rows[m.name], columns=["t", *(s.name for s in m.signals)])
            .sort_values("t").reset_index(drop=True)
            for m in db.messages}


def message_stats(dfs: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Per message: frame count and the spacing between frames, against the configured period."""
    rows = []
    for name, df in dfs.items():
        gaps = df["t"].diff().dropna()
        rows.append({
            "message": name,
            "count": len(df),
            "expected_period_s": config.PERIODS[name],
            "mean_period_s": gaps.mean(),
            "median_period_s": gaps.median(),  # the mean is pulled up by any dropout
            "max_gap_s": gaps.max(),
            "std_period_s": gaps.std(),
        })
    return pd.DataFrame(rows)


def save_decoded(dfs: dict[str, pd.DataFrame]) -> None:
    """Write each decoded message table to bms_can/output for the next steps."""
    for name, df in dfs.items():
        df.to_csv(config.CAN_OUT / f"decoded_{name}.csv", index=False)


def load_decoded() -> dict[str, pd.DataFrame]:
    """Read the decoded message tables written by save_decoded."""
    return {name: pd.read_csv(config.CAN_OUT / f"decoded_{name}.csv") for name in config.PERIODS}


def main() -> None:
    """Decode the simulated log, print message timing, and save the tables."""
    db = cantools.database.load_file(config.CAN_DIR / "bms.dbc")
    frames = read_frames(config.CAN_OUT / "bms_run.log")
    print(f"  SYNTHETIC DATA: read {len(frames)} frames")
    dfs = decode(frames, db)
    print(message_stats(dfs).round(4).to_string(index=False))
    save_decoded(dfs)


if __name__ == "__main__":
    main()
