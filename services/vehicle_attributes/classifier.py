"""Re-export shim — actual source lives in ../vehicle-attributes/classifier.py."""
from classifier import *  # noqa: F401,F403  (pulls from sys.path inserted in __init__)
from classifier import _preprocess, _vote, _enforce_make_model_consistency  # noqa: F401  (underscore names excluded from *)
from classifier import run_classifier_and_vote  # noqa: F401  (re-exported)
