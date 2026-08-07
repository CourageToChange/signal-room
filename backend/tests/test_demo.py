from __future__ import annotations

from pathlib import Path

from signal_room.demo import PRIVATE_PATTERN, export_pressure_drop
from signal_room.models import RunbookConfig, TopologyConfig


async def test_demo_export_is_reproducible_and_private(
    tmp_path: Path,
    topology: TopologyConfig,
    runbooks: RunbookConfig,
) -> None:
    output = tmp_path / "pressure-drop.json"
    await export_pressure_drop(topology, runbooks, output)
    generated = output.read_text(encoding="utf-8")
    committed = (
        Path(__file__).resolve().parents[2]
        / "frontend"
        / "src"
        / "demo"
        / "generated"
        / "pressure-drop.json"
    ).read_text(encoding="utf-8")
    assert generated == committed
    assert PRIVATE_PATTERN.search(generated) is None
    assert '"root_asset_id": "orchid-guest"' in generated
    assert '"kind": "correlated"' in generated
