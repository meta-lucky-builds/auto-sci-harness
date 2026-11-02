# Candidate image: the image AGENT-WRITTEN code runs in (every candidate/dev/held-out/replay run).
# Build (after the full image; no downloads, derives from the local image only):
#   docker build -f docker/sandbox-candidate.Dockerfile -t custom-harness-sandbox:py312-candidate docker
#
# Why (STATUS red-team ledger, BUNDLED-DATA): scikit-learn ships the iris/wine/breast_cancer/digits/diabetes datasets
# inside the package, and other packages' test fixtures hold copies too (scipy/io/arff/tests/data/iris.arff,
# scipy/spatial/tests/data/iris.txt, sklearn's OpenML fixture id_61 = iris). The real suite tasks are exported from
# those very files, so a program in the full image could load the complete labelled dataset, held-out rows included.
# Only trusted task building (`chh build-suite --docker`) uses the full image `custom-harness-sandbox:py312`.
#
# Removed: the contents of sklearn/datasets/{data,descr,images} (their __init__.py kept, so `import sklearn.datasets`
# works and the loaders fail with FileNotFoundError), every `tests`/`test` package directory in site-packages
# (sample/fixture data of sklearn, scipy, numpy, joblib, networkx, pandas, torch, ...), and torch's bundled test
# model. Kept (not datasets of examples; see CANDIDATE_ALLOWLIST in tests/test_bundled_data.py): scipy's Sobol'
# direction numbers, networkx's graph atlas, dateutil's time-zone database, sympy's MathML stylesheets, C/C++ headers.
# Not installed in the image (checked): torchvision, seaborn, statsmodels, pooch (scipy.datasets downloads only, and
# the sandbox has no network). sklearn's fetch_* loaders need the network and an empty cache (HOME=/tmp, tmpfs).
FROM custom-harness-sandbox:py312

USER root
RUN set -eu; SP=/usr/local/lib/python3.12/site-packages; \
    for d in data descr images; do \
        find "$SP/sklearn/datasets/$d" -mindepth 1 ! -name __init__.py ! -path '*/__pycache__*' -exec rm -rf {} +; \
    done; \
    find "$SP" -depth -type d \( -name tests -o -name test \) ! -path '*/include/*' ! -path '*.dist-info/*' \
        -exec rm -rf {} +; \
    rm -f "$SP/torch/bin/test_interpreter_async.pt"; \
    python -c "import sklearn.datasets, sklearn.utils; sklearn.utils.all_estimators(); import scipy.stats, pandas, numpy, torch"

LABEL org.custom-harness.bundled-data="removed"
USER 1000:1000
WORKDIR /work
