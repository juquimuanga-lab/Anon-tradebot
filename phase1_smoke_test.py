from pathlib import Path
import ast

p = Path("backend/app/connectors/solana_tracker.py")
tree = ast.parse(p.read_text(encoding="utf-8"))
assert any(
    isinstance(node, ast.ClassDef) and node.name == "SolanaTrackerClient"
    for node in tree.body
)
assert any(
    isinstance(node, ast.ClassDef) and node.name == "SmartMoneySignal"
    for node in tree.body
)
print("Phase 1 connector syntax smoke test passed.")
