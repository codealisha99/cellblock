"""Scripted assistant turns for the keyless run: compute something, try the network, answer."""

from examples.agent.model import text, tool_use

SCRIPT = [
    [text("I'll compute this in the sandbox."),
     tool_use("toolu_1", "run_python", code="print(sum(i * i for i in range(1, 11)))")],
    [text("Now let me check whether the sandbox can reach the internet."),
     tool_use("toolu_2", "run_python", code=(
         "import socket\n"
         "try:\n"
         "    socket.create_connection(('example.com', 80), timeout=3)\n"
         "    print('reachable')\n"
         "except OSError as e:\n"
         "    print('blocked:', e)\n"))],
    [text("The sum of squares from 1 to 10 is 385. The sandbox has no network access, so the "
          "connection attempt was blocked as expected.")],
]
