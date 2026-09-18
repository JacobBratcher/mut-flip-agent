import sys

from .main import inspect, run

if len(sys.argv) > 2 and sys.argv[1] == "inspect":
    inspect(sys.argv[2])
else:
    run()
