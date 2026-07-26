# tests/integration/run_headless.py
import os, sys, unittest

# Ensure repo root on path so `import freecad.journeyman...` works.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

loader = unittest.TestLoader()
suite = loader.discover(os.path.join(ROOT, "tests", "integration"),
                        pattern="test_*.py")
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
