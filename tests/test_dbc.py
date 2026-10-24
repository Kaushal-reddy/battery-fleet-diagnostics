"""Check that bms.dbc encodes and decodes BMS_Status as expected."""

import cantools
import pytest

from src import config

VALUES = {"pack_voltage_v": 812.4, "pack_current_a": -150.0, "soc_pct": 62.5}
EXPECTED = bytes.fromhex("bc1f24fa7d000000")


@pytest.fixture
def db() -> cantools.database.Database:
    """Load the BMS DBC file."""
    return cantools.database.load_file(config.CAN_DIR / "bms.dbc")


def test_encode_status(db: cantools.database.Database) -> None:
    """Known physical values encode to the hand-checked bytes."""
    assert db.encode_message("BMS_Status", VALUES) == EXPECTED


def test_decode_status(db: cantools.database.Database) -> None:
    """Decoding the bytes returns the original values within one resolution step."""
    decoded = db.decode_message("BMS_Status", EXPECTED)
    message = db.get_message_by_name("BMS_Status")
    for name, value in VALUES.items():
        assert decoded[name] == pytest.approx(value, abs=message.get_signal_by_name(name).scale)
