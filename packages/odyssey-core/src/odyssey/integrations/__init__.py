"""Provider integrations.

Each module here wraps one provider SDK. **Nothing in this package is imported
by ``import odyssey``**, and no module imports its provider at module level —
``core`` declares ``dependencies = []`` and that stays true. The provider import
happens inside the wrapper's ``__init__``, so installing
``odyssey[anthropic]`` is what makes ``odyssey.integrations.anthropic`` usable
and nothing else pays for it.

That constraint is also why the wrappers *wrap* rather than subclass: subclassing
``anthropic.Anthropic`` would require the import at class-definition time.
"""

from __future__ import annotations

__all__: list = []
