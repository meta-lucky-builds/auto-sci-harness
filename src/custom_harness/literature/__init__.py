"""Literature module (GOAL §5 L4 stage 3), smaller since GOAL 18.6.

The product keeps two jobs: checking citations in Court reports (`cite.py`, resolved offline from a dated snapshot)
and a small dated fetch for the time-split tests of GOAL 6.4 (`snapshot.py`, `client.py`). Lab agents search for
themselves.
The rest (`retrieval.py`, `fulltext.py`, `alphaxiv.py`, `lab.py`) stays for the older research loop and is no longer
developed as a product. tests/test_v1_features.py keeps the Court and the Experience API to those two jobs.
"""
