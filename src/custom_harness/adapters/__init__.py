"""Domain adapters that live outside the Kernel package (GOAL §5 "Domain adapters", §15).

Each subpackage is one adapter's code. It becomes usable only through `custom_harness.kernel.adapters.REGISTRY` and a
human approval of its content hash (`chh approve-adapter`, invariant 13). This file is part of every such adapter's
identity (listed in its AdapterSpec.modules), so keep it free of behaviour.
"""
