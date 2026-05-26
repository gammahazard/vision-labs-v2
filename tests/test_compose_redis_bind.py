"""Regression guard: Redis must stay bound to loopback (security audit L2).

Redis runs with AUTH optional (empty REDIS_PASSWORD on a bare compose-up).
The ONLY thing keeping a passwordless Redis off the untrusted LAN is the
`127.0.0.1:6379` port bind. If that ever drifts to `0.0.0.0` / a bare
`6379:6379`, an unauthenticated LAN attacker could reach the bus and drive
the orchestrator. This test fails loudly if the bind regresses.
"""

import re
from pathlib import Path

_COMPOSE = Path(__file__).parent.parent / "docker-compose.yml"


def _redis_service_block(text: str) -> str:
    """Return the `redis:` service block (up to the next top-level service)."""
    lines = text.splitlines()
    out, in_block = [], False
    for line in lines:
        if re.match(r"^  redis:\s*$", line):
            in_block = True
            continue
        if in_block:
            # Next sibling service (two-space indent + name:) ends the block.
            if re.match(r"^  \S", line):
                break
            out.append(line)
    return "\n".join(out)


def test_redis_port_is_loopback_bound():
    block = _redis_service_block(_COMPOSE.read_text())
    assert block, "could not locate the redis service block in docker-compose.yml"
    # The published port must be explicitly bound to 127.0.0.1.
    assert '"127.0.0.1:6379:6379"' in block, (
        "redis port is not 127.0.0.1-bound — a passwordless Redis would be "
        "LAN-reachable (audit L2)"
    )
    # And must NOT be published on all interfaces.
    assert not re.search(r'-\s*"0\.0\.0\.0:6379', block), "redis bound to 0.0.0.0"
    assert not re.search(r'-\s*"6379:6379"', block), (
        "redis published on a bare 6379:6379 (binds all interfaces)"
    )
