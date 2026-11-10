import numpy as np
from scipy import sparse


def sanitize_matrix(X):
    """
    Replace NaN/Inf that might appear after preprocessing.
    Safe for sparse matrices (touches X.data only).
    """
    if sparse.issparse(X):
        X = X.tocsr(copy=True)
        X.data = np.nan_to_num(X.data, nan=0.0, posinf=0.0, neginf=0.0)
        return X
    return np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)


def expected_input_columns(model) -> list:
    """
    Returns the exact feature order the fitted pipeline expects.
    Works with CalibratedClassifierCV + FrozenEstimator(pipe).
    """
    if hasattr(model, "calibrated_classifiers_") and model.calibrated_classifiers_:
        est = model.calibrated_classifiers_[0].estimator
        if hasattr(est, "feature_names_in_"):
            return list(est.feature_names_in_)
    return []


def align_X_to_model(model, X):
    """
    Ensures columns exist and match order expected by the model.
    """
    expected = expected_input_columns(model)
    if not expected:
        return X

    for c in expected:
        if c not in X.columns:
            X[c] = np.nan

    return X.reindex(columns=expected)
