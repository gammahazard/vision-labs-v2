"""Re-export shim — actual source lives in ../vehicle-attributes/classifier.py."""
from classifier import *  # noqa: F401,F403  (pulls from sys.path inserted in __init__)
from classifier import _preprocess, _vote  # noqa: F401  (underscore names excluded from *)
