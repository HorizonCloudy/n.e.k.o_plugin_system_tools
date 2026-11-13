from pathlib import Path


def test_plugin_manifest_exists() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = root / "plugin.toml"
    assert manifest.is_file()
    text = manifest.read_text(encoding="utf-8")
    assert 'id = "system_tools"' in text
    assert 'entry = "plugin.plugins.system_tools:SystemToolsPlugin"' in text
    assert 'entry = "ui/panel.tsx"' in text
    assert 'context = "system_tools_panel"' in text